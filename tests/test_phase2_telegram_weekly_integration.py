"""Cross-module Phase 2 contracts for Telegram routing and weekly evidence.

All tests use a temporary SQLite database and fake Telegram/Gemini clients.
They intentionally exercise public handler/tool entry points rather than
duplicating implementation logic.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
from unittest.mock import patch
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import conversation_contract as C
import conversation_store
import conversation_tools
import daily_log
import morning_context
import phase2_store
import telegram_bot
import weekly_analysis_v2
import weekly_report
from conversation_fakes import FakeBot, TempDBCase, message


KYIV = ZoneInfo("Europe/Kyiv")
MONDAY_NOON = dt.datetime(2026, 7, 13, 12, tzinfo=KYIV)


class TelegramPriorityContracts(TempDBCase):
    def setUp(self):
        super().setUp()
        self._real_bot = telegram_bot.bot
        self.bot = FakeBot()
        telegram_bot.bot = self.bot

    def tearDown(self):
        telegram_bot.bot = self._real_bot
        super().tearDown()

    def _open_morning_question(self, question_id=700):
        morning_context.ensure_request("2026-07-13")
        self.assertIsNotNone(morning_context.claim_question("2026-07-13"))
        self.assertTrue(morning_context.mark_question_sent("2026-07-13", question_id))
        return question_id

    def _open_fact_clarification(self, question_id=800):
        ctx = conversation_store.ActionContext(
            source="telegram", chat_id="1", user_id="42",
            message_id="origin", input_text="жим 80 4х8",
        )
        origin = conversation_store.reserve(ctx).action_id
        pending_id = conversation_store.create_pending(
            origin, ctx, C.INTENT_LOG_STRENGTH,
            {
                "date_ref": {"kind": "today", "value": None},
                "entries": [{
                    "exercise_name": "жим", "weight_kg": 80,
                    "sets": 4, "reps": 8,
                }],
            },
            ["fact_status"],
        )
        conversation_store.set_clarification_message_id(pending_id, question_id)
        return pending_id, question_id

    def _morning_status(self):
        conn = daily_log.connect()
        try:
            return conn.execute(
                "SELECT status FROM morning_context WHERE recovery_date='2026-07-13'"
            ).fetchone()[0]
        finally:
            conn.close()

    def test_exact_morning_reply_wins_over_clarification_and_conversation(self):
        morning_id = self._open_morning_question()
        pending_id, _ = self._open_fact_clarification()

        with patch.dict(os.environ, {"CONVERSATIONAL_ROUTER_ENABLED": "true"}), \
             patch.object(telegram_bot, "route_via_conversation") as route, \
             patch.object(telegram_bot, "deliver_morning_analysis"), \
             patch.object(telegram_bot, "_capture_daily_factors"):
            telegram_bot.handle_free_text(
                message(900, "спал спокойно", reply_to_message_id=morning_id)
            )

        route.assert_not_called()
        self.assertEqual(self._morning_status(), morning_context.STATUS_ANSWERED)
        active = conversation_store.active_pending("telegram", "1", "42")
        self.assertEqual(active["pending_id"], pending_id)

    def test_exact_clarification_reply_wins_over_session_followup(self):
        pending_id, clarification_id = self._open_fact_clarification()
        conn = conversation_store.connect()
        try:
            phase2_store.migrate(conn)
            phase2_store.save_session(
                conn, "telegram", "1", "42",
                {
                    "active_topic": "hrv_rmssd",
                    "last_read_intent": C.INTENT_GET_METRIC_TREND,
                    "last_query": {"metric": "hrv_rmssd", "window_days": 7},
                    "last_evidence_sha256": "a" * 64,
                    "turn_count": 1,
                },
                expected_version=None,
            )
            conn.commit()
        finally:
            conn.close()

        class NoGemini:
            def generate(self, *_args, **_kwargs):
                raise AssertionError("exact clarification must not call Gemini")

        with patch.dict(os.environ, {"CONVERSATION_MEMORY_ENABLED": "true"}), \
             patch.object(telegram_bot, "_build_gemini_client", return_value=NoGemini()), \
             patch.object(telegram_bot, "get_kiev_time", return_value=MONDAY_NOON):
            telegram_bot.route_via_conversation(
                message(901, "да", reply_to_message_id=clarification_id), "да"
            )

        self.assertIsNone(conversation_store.active_pending("telegram", "1", "42"))
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM workout_exercises").fetchone()[0], 1
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM pending_actions WHERE pending_id=?", (pending_id,)
                ).fetchone()[0],
                C.PENDING_RESOLVED,
            )
        finally:
            conn.close()

    def test_wrong_reply_consumes_neither_pending_request(self):
        self._open_morning_question()
        pending_id, _ = self._open_fact_clarification()

        with patch.dict(os.environ, {"CONVERSATIONAL_ROUTER_ENABLED": "true"}), \
             patch.object(telegram_bot, "route_via_conversation") as route:
            telegram_bot.handle_free_text(
                message(902, "обычный вопрос", reply_to_message_id=999999)
            )

        route.assert_called_once()
        self.assertEqual(self._morning_status(), morning_context.STATUS_PENDING)
        active = conversation_store.active_pending("telegram", "1", "42")
        self.assertEqual(active["pending_id"], pending_id)

    def test_help_command_does_not_consume_pending_and_has_phase2_guidance(self):
        self._open_morning_question()
        pending_id, _ = self._open_fact_clarification()

        with patch.object(telegram_bot, "TG_CHAT", "1"):
            telegram_bot.send_welcome(message(903, "/help"))

        self.assertEqual(self._morning_status(), morning_context.STATUS_PENDING)
        self.assertEqual(
            conversation_store.active_pending("telegram", "1", "42")["pending_id"],
            pending_id,
        )
        help_text = self.bot.sent[-1].text
        self.assertIn("HRV за 28 дней", help_text)
        self.assertIn("через *Reply*", help_text)
        self.assertIn("удали последнюю тренировку", help_text)
        self.assertIn("перенеси последнюю тренировку на вчера", help_text)
        self.assertIn("через *Reply*", help_text)

    def test_status_uses_explicit_records_day_cutoff(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript("""
                CREATE TABLE workout_exercises (
                    date TEXT, exercise_name TEXT, weight REAL, reps INTEGER
                );
                CREATE TABLE cardio_exercises (
                    date TEXT, type TEXT, duration REAL, distance REAL,
                    avg_hr INTEGER, hr_zone_0_duration TEXT,
                    hr_zone_1_duration TEXT, hr_zone_2_duration TEXT,
                    hr_zone_3_duration TEXT, hr_zone_4_duration TEXT,
                    hr_zone_5_duration TEXT
                );
                CREATE TABLE supplements_log (
                    date TEXT, name TEXT, dosage TEXT, time TEXT
                );
                CREATE TABLE daily_log (date TEXT PRIMARY KEY, notes TEXT);
            """)
            conn.commit()
        finally:
            conn.close()

        with patch.object(telegram_bot, "TG_CHAT", "1"), \
             patch.object(telegram_bot.workouts_db, "get_accumulated_creatine", return_value=(0, 0)):
            with patch.object(
                telegram_bot, "get_kiev_time",
                return_value=dt.datetime(2026, 7, 13, 4, 30, tzinfo=KYIV),
            ):
                telegram_bot.show_status(message(904, "/status"))
            with patch.object(
                telegram_bot, "get_kiev_time",
                return_value=dt.datetime(2026, 7, 13, 5, 0, tzinfo=KYIV),
            ):
                telegram_bot.show_status(message(905, "/status"))

        self.assertIn("Статус за 2026-07-12", self.bot.sent[-2].text)
        self.assertIn("Статус за 2026-07-13", self.bot.sent[-1].text)


class WeeklyIntegrationContracts(TempDBCase):
    def _create_health_schema(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS recovery (
                    cycle_id INTEGER PRIMARY KEY, created_at TEXT,
                    recovery_score REAL, hrv_rmssd REAL, resting_hr REAL
                );
                CREATE TABLE IF NOT EXISTS sleep (
                    id TEXT PRIMARY KEY, end TEXT,
                    performance_pct REAL, raw_json TEXT
                );
            """)
            for i in range(7):
                action_day = dt.date(2026, 7, 6) + dt.timedelta(days=i)
                outcome = action_day + dt.timedelta(days=1)
                instant = dt.datetime.combine(
                    outcome, dt.time(8), tzinfo=KYIV
                ).astimezone(dt.timezone.utc)
                conn.execute(
                    "INSERT INTO recovery VALUES (?,?,?,?,?)",
                    (i + 1, instant.isoformat(), 70 + i, 50 + i, 60 - i),
                )
                raw = json.dumps({
                    "nap": False,
                    "score": {"stage_summary": {
                        "total_in_bed_time_milli": int((7 + i / 10) * 3_600_000),
                        "total_awake_time_milli": 0,
                    }},
                })
                conn.execute(
                    "INSERT INTO sleep VALUES (?,?,?,?)",
                    (f"sleep-{i}", instant.isoformat(), 80 + i, raw),
                )
            conn.commit()
        finally:
            conn.close()

        conn = telegram_bot.workouts_db.connect()
        try:
            conn.executemany(
                """INSERT INTO workout_exercises
                   (date, exercise_name, weight, sets, reps, volume)
                   VALUES (?, 'test', 10, ?, 10, ?)""",
                [("2026-07-06", 4, 1000), ("2026-07-08", 5, 1200)],
            )
            conn.executemany(
                """INSERT INTO cardio_exercises(date, type, duration)
                   VALUES (?, 'run', ?)""",
                [("2026-07-07", 30), ("2026-07-12", 45)],
            )
            conn.commit()
        finally:
            conn.close()

    def test_weekly_flag_off_keeps_legacy_and_on_selects_v2(self):
        with patch.object(weekly_report.phase2_flags, "weekly_v2_enabled", return_value=False), \
             patch.object(weekly_report.weekly_analysis_v2, "create_report") as v2:
            legacy = weekly_report.create_report(dt.date(2026, 7, 12))
        v2.assert_not_called()
        self.assertIn("Еженедельный отчёт Джарвиса", legacy)

        with patch.object(weekly_report.phase2_flags, "weekly_v2_enabled", return_value=True), \
             patch.object(weekly_report.weekly_analysis_v2, "create_report", return_value="V2") as v2:
            self.assertEqual(weekly_report.create_report(dt.date(2026, 7, 12)), "V2")
        v2.assert_called_once_with(dt.date(2026, 7, 12))

    def test_proactive_and_conversational_weekly_use_identical_numbers(self):
        self._create_health_schema()
        conn = daily_log.connect()
        try:
            proactive = weekly_analysis_v2.build_snapshot(conn, dt.date(2026, 7, 12))
        finally:
            conn.close()

        reservation = conversation_store.reserve(conversation_store.ActionContext(
            source="telegram", chat_id="1", user_id="42",
            message_id="weekly-query", input_text="как прошла неделя?",
        ))
        tool_ctx = conversation_tools.ExecContext(
            action_id=reservation.action_id, source="telegram", chat_id="1",
            message_id="weekly-query", local_now=MONDAY_NOON,
        )
        with patch.dict(os.environ, {"CONVERSATION_ANALYTICS_V2_ENABLED": "true"}):
            conversational = conversation_tools.execute(
                "get_week_summary", {}, tool_ctx
            )["data"]

        self.assertEqual(conversational["snapshot"], "weekly_evidence.v2")
        self.assertEqual(
            conversational["current_week"]["metrics"],
            proactive["current_week"]["metrics"],
        )
        self.assertEqual(
            conversational["current_week"]["activity"],
            proactive["current_week"]["activity"],
        )
        self.assertEqual(
            conversational["mean_delta_current_minus_previous"],
            proactive["mean_delta_current_minus_previous"],
        )


if __name__ == "__main__":
    import unittest
    unittest.main()
