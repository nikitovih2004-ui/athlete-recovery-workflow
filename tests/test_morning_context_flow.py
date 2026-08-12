"""Daily-context flow tests using only temporary SQLite and mocked Telegram I/O."""

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import daily_log
import generate_insights
import morning_context
import morning_flow
import morning_observability
import send_reminder
import telegram_bot
import workouts_db
import conversation_contract as C
from tests.conversation_fakes import envelope, single_response


class _TelegramResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


def _message(message_id, text, reply_to_message_id=None):
    reply = (
        SimpleNamespace(message_id=reply_to_message_id)
        if reply_to_message_id is not None
        else None
    )
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        chat=SimpleNamespace(id=1),
        reply_to_message=reply,
    )


class MorningContextFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "whoop.db")
        self.old_daily_log_path = daily_log.DB_PATH
        self.old_workouts_path = workouts_db.DB_PATH
        daily_log.DB_PATH = self.db_path
        workouts_db.DB_PATH = self.db_path

    def tearDown(self):
        daily_log.DB_PATH = self.old_daily_log_path
        workouts_db.DB_PATH = self.old_workouts_path
        self.temp.cleanup()

    def _ask(self, recovery_date="2026-07-10", question_message_id=700):
        morning_context.ensure_request(recovery_date)
        self.assertIsNotNone(morning_context.claim_question(recovery_date))
        self.assertTrue(
            morning_context.mark_question_sent(recovery_date, question_message_id)
        )
        return question_message_id

    def _count(self, table):
        if not Path(self.db_path).exists():
            return 0
        conn = sqlite3.connect(self.db_path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] if exists else 0
        finally:
            conn.close()

    def _observed_script(self, readiness):
        def run(name, *_args, **kwargs):
            if name == "fetch_data.py":
                run_id = kwargs["pipeline_run_id"]
                pipeline_date = kwargs["pipeline_date"]
                morning_observability.record_stage(
                    pipeline_date, run_id, "whoop_refresh_attempted",
                    "skipped", "access_token_current_no_refresh_required",
                )
                morning_observability.record_stage(
                    pipeline_date, run_id, "whoop_refresh_result",
                    "success", "access_token_current",
                )
                morning_observability.record_stage(
                    pipeline_date, run_id, "recovery_imported",
                    "success" if readiness["ready"] else "waiting",
                    "current_morning_recovery_present"
                    if readiness["ready"] else "current_morning_recovery_absent",
                )
                morning_observability.record_stage(
                    pipeline_date, run_id, "sleep_imported",
                    "success" if readiness["ready"] else "waiting",
                    "current_morning_sleep_present"
                    if readiness["ready"] else "current_morning_sleep_absent",
                )
            return morning_flow.ScriptResult(
                True, f"subprocess_completed:{name}", 1, 0
            )
        return run

    def test_one_recovery_date_has_one_active_request(self):
        first, _ = morning_context.ensure_request("2026-07-10")
        second, _ = morning_context.ensure_request("2026-07-10")
        self.assertEqual(first["recovery_date"], second["recovery_date"])
        self.assertEqual(self._count("morning_context"), 1)

    def test_concurrent_claims_have_one_winner(self):
        morning_context.ensure_request("2026-07-10")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _: morning_context.claim_question("2026-07-10"),
                    range(2),
                )
            )
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_question_message_id_is_saved_after_send(self):
        morning_context.ensure_request("2026-07-10")
        self.assertIsNotNone(morning_context.claim_question("2026-07-10"))
        self.assertTrue(morning_context.mark_question_sent("2026-07-10", 701))
        conn = daily_log.connect()
        try:
            row = conn.execute(
                "SELECT question_message_id, question_claimed_at "
                "FROM morning_context WHERE recovery_date='2026-07-10'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "701")
        self.assertIsNone(row[1])

    def test_failed_send_claim_is_retryable(self):
        morning_context.ensure_request("2026-07-10")
        self.assertIsNotNone(morning_context.claim_question("2026-07-10"))
        self.assertTrue(morning_context.release_question_claim("2026-07-10"))
        self.assertIsNotNone(morning_context.claim_question("2026-07-10"))

    def test_direct_reply_to_exact_question_is_accepted(self):
        question_id = self._ask()
        accepted = morning_context.accept_pending_reply(800, question_id)
        self.assertEqual(accepted["evening_date"], "2026-07-09")

    def test_reply_to_another_message_is_not_context(self):
        self._ask()
        self.assertIsNone(morning_context.accept_pending_reply(800, 999))
        conn = daily_log.connect()
        try:
            status = conn.execute(
                "SELECT status FROM morning_context WHERE recovery_date='2026-07-10'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(status, morning_context.STATUS_PENDING)

    def test_workout_reaches_parser_while_context_is_pending(self):
        self._ask()
        payload = envelope(C.INTENT_LOG_STRENGTH, {
            "date_ref": {"kind": "absolute", "value": "2026-07-10"},
            "fact_status": "completed",
            "entries": [{"exercise_name": "test exercise", "weight_kg": 10,
                         "sets": 1, "reps": 5}],
        })
        with (
            patch.object(telegram_bot, "_build_gemini_client",
                         return_value=single_response(payload)),
            patch.object(telegram_bot.bot, "send_message"),
        ):
            telegram_bot.handle_free_text(_message(801, "structured workout"))
        self.assertEqual(self._count("workout_exercises"), 1)
        self.assertEqual(self._count("daily_log"), 0)

    def test_supplement_reaches_parser_while_context_is_pending(self):
        self._ask()
        payload = envelope(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "absolute", "value": "2026-07-10"},
            "time": "09:00",
            "items": [{"name": "test supplement", "dose_text": "5 mg",
                       "taken": True}],
        })
        with (
            patch.object(telegram_bot, "_build_gemini_client",
                         return_value=single_response(payload)),
            patch.object(telegram_bot.bot, "send_message"),
        ):
            telegram_bot.handle_free_text(_message(802, "structured supplement"))
        self.assertEqual(self._count("supplements_log"), 1)
        self.assertEqual(self._count("daily_log"), 0)

    def test_unrecognized_message_is_not_written_to_daily_log(self):
        # route_via_conversation() is the live path (router is on by default);
        # parse_with_gemini() is unreachable legacy code, so the router's own
        # Gemini call must be scripted here too - otherwise this silently
        # falls through to a real, nondeterministic network call.
        with (
            patch.object(telegram_bot, "_build_gemini_client",
                         return_value=single_response("not a valid router envelope")),
            patch.object(telegram_bot.bot, "send_message") as send,
        ):
            telegram_bot.handle_free_text(_message(803, "unrecognized input"))
        self.assertEqual(self._count("daily_log"), 0)
        self.assertIn("Не смог завершить обработку", send.call_args.args[1])

    def test_delete_command_soft_deletes_and_closes_connection(self):
        today_str = telegram_bot.get_kiev_time().date().isoformat()
        workouts_db.log_exercise(today_str, "test exercise", 10, 1, 5)
        with (
            patch.object(telegram_bot, "legacy_destructive_text_enabled", return_value=True),
            patch.object(telegram_bot.bot, "send_message"),
        ):
            self.assertTrue(
                telegram_bot.handle_delete_command("удали тренировку за сегодня", 1)
            )
        conn = sqlite3.connect(self.db_path)
        try:
            deleted_at = conn.execute(
                "SELECT deleted_at FROM workout_exercises WHERE date=?", (today_str,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNotNone(deleted_at)
        # The connection must not be leaked: a fresh writer can still open the db.
        probe = sqlite3.connect(self.db_path)
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()
        probe.close()

    def test_delete_command_rolls_back_and_closes_on_failure(self):
        today_str = telegram_bot.get_kiev_time().date().isoformat()
        workouts_db.log_exercise(today_str, "test exercise", 10, 1, 5)
        with (
            patch.object(telegram_bot, "legacy_destructive_text_enabled", return_value=True),
            patch.object(workouts_db, "soft_delete_activity", side_effect=RuntimeError("boom")),
            patch.object(telegram_bot.bot, "send_message"),
        ):
            with self.assertRaises(RuntimeError):
                telegram_bot.handle_delete_command("удали тренировку за сегодня", 1)
        conn = sqlite3.connect(self.db_path)
        try:
            deleted_at = conn.execute(
                "SELECT deleted_at FROM workout_exercises WHERE date=?", (today_str,)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(deleted_at)
        # The connection must not be leaked or left mid-transaction after the failure.
        probe = sqlite3.connect(self.db_path)
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()
        probe.close()

    def test_duplicate_reply_message_id_is_not_accepted_twice(self):
        question_id = self._ask()
        self.assertIsNotNone(morning_context.accept_pending_reply(804, question_id))
        self.assertIsNone(morning_context.accept_pending_reply(804, question_id))

    def test_second_answer_does_not_overwrite_first_context(self):
        question_id = self._ask()
        parsed = {"workouts": [], "supplements": [], "date": None}
        with (
            patch.object(telegram_bot, "deliver_morning_analysis"),
            patch.object(telegram_bot, "parse_with_gemini", return_value=parsed),
            patch.object(telegram_bot.bot, "send_message"),
        ):
            telegram_bot.handle_free_text(_message(805, "context-one", question_id))
            telegram_bot.handle_free_text(_message(806, "context-two", question_id))
        conn = daily_log.connect()
        try:
            notes = conn.execute(
                "SELECT notes FROM daily_log WHERE date='2026-07-09'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(notes, "context-one")

    def test_late_reply_uses_request_evening_date(self):
        question_id = self._ask("2026-07-10", 702)
        with (
            patch.object(telegram_bot, "deliver_morning_analysis"),
            patch.object(telegram_bot.bot, "send_message"),
        ):
            telegram_bot.handle_free_text(_message(807, "late-context", question_id))
        conn = daily_log.connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM daily_log WHERE date='2026-07-09' "
                "AND notes IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_evening_reminder_does_not_write_activity_or_context(self):
        with (
            patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "1"},
            ),
            patch.object(sys, "argv", ["send_reminder.py"]),
            patch.object(
                send_reminder.requests,
                "post",
                return_value=_TelegramResponse(),
            ),
        ):
            send_reminder.main()
        self.assertEqual(self._count("daily_log"), 0)
        self.assertEqual(self._count("supplements_log"), 0)
        self.assertEqual(self._count("morning_context"), 0)

    def test_google_form_importer_is_not_in_morning_cycle(self):
        calls = []
        readiness = {"ready": True}

        def record(name, *args, **kwargs):
            calls.append(name)
            return self._observed_script(readiness)(name, *args, **kwargs)

        with (
            patch.object(sys, "argv", ["morning_flow.py", "--dry-run"]),
            patch.object(morning_flow, "run_script", side_effect=record),
            patch.object(
                morning_flow.morning_readiness,
                "morning_data_status",
                return_value={
                    "recovery": True, "sleep": True,
                    "ready": True, "error": None,
                },
            ),
            patch.object(morning_flow, "log"),
        ):
            morning_flow.main()
        self.assertNotIn("import_evening_log.py", calls)
        self.assertNotIn("daily_summary.py", calls)

    def test_prompt_waits_for_whoop_then_retries_telegram_without_duplicate(self):
        now = dt.datetime(2026, 7, 10, 8, 0, tzinfo=morning_flow.TZ)
        sent_ids = [None, 1702]
        readiness = {"ready": False}

        def telegram_result(_text):
            return sent_ids.pop(0)

        def status(*_args, **_kwargs):
            ready = readiness["ready"]
            return {
                "recovery": ready, "sleep": ready,
                "ready": ready, "error": None,
            }

        common = (
            patch.object(sys, "argv", ["morning_flow.py"]),
            patch.object(morning_flow, "current_time", return_value=now),
            patch.object(
                morning_flow, "run_script",
                side_effect=self._observed_script(readiness),
            ),
            patch.object(
                morning_flow.morning_readiness,
                "morning_data_status",
                side_effect=status,
            ),
            patch.object(morning_flow, "send_telegram", side_effect=telegram_result),
            patch.object(morning_flow, "log"),
        )
        with common[0], common[1] as mocked_time, common[2], common[3], common[4] as send, common[5]:
            morning_flow.main()
            self.assertEqual(self._count("morning_context"), 0)
            send.assert_not_called()

            # WHOOP appears late; the first Telegram attempt fails and is released.
            mocked_time.return_value = now + dt.timedelta(minutes=15)
            readiness["ready"] = True
            morning_flow.main()
            row = morning_context.ensure_request(now.date())[0]
            self.assertIsNone(row["question_message_id"])

            # The next cron run retries and persists exactly one prompt.
            mocked_time.return_value = now + dt.timedelta(minutes=30)
            morning_flow.main()
            row = morning_context.ensure_request(now.date())[0]
            self.assertEqual(row["question_message_id"], "1702")

            # Further cron retries are idempotent: no third delivery attempt.
            mocked_time.return_value = now + dt.timedelta(minutes=45)
            morning_flow.main()
            self.assertEqual(send.call_count, 2)

    def test_missing_whoop_sends_one_context_only_prompt_at_cutoff(self):
        late = dt.datetime(2026, 7, 10, 23, 0, tzinfo=morning_flow.TZ)
        readiness = {"ready": False}
        with patch.object(sys, "argv", ["morning_flow.py"]), \
             patch.object(morning_flow, "current_time", return_value=late), \
             patch.object(
                 morning_flow, "run_script",
                 side_effect=self._observed_script(readiness),
             ), \
             patch.object(
                 morning_flow.morning_readiness, "morning_data_status",
                 return_value={
                     "recovery": False, "sleep": False,
                     "ready": False, "error": None,
                 },
             ), \
             patch.object(morning_flow, "send_telegram", return_value=1800) as send, \
             patch.object(morning_flow, "log"):
            morning_flow.main()
        send.assert_called_once()
        self.assertIn("без вымышленных метрик", send.call_args.args[0])

    def test_legacy_schema_migrates_without_losing_row(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """
            CREATE TABLE morning_context (
                recovery_date TEXT PRIMARY KEY,
                evening_date TEXT NOT NULL,
                status TEXT NOT NULL,
                asked_at TEXT,
                reminded_at TEXT,
                replied_at TEXT,
                analyzed_at TEXT,
                source_message_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO morning_context "
            "(recovery_date, evening_date, status, created_at, updated_at) "
            "VALUES ('2026-07-10', '2026-07-09', 'analyzed', 't0', 't0')"
        )
        conn.commit()
        conn.close()

        conn = daily_log.connect()
        try:
            morning_context.ensure_table(conn)
            morning_context.ensure_table(conn)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(morning_context)")
            }
            row = conn.execute(
                "SELECT question_message_id FROM morning_context"
            ).fetchone()
        finally:
            conn.close()
        self.assertIn("question_message_id", columns)
        self.assertIn("question_claimed_at", columns)
        self.assertIsNone(row[0])
        self.assertEqual(self._count("morning_context"), 1)

    def test_context_date_maps_to_next_local_morning(self):
        self.assertEqual(
            generate_insights.outcome_date_for_context("2026-07-09"),
            "2026-07-10",
        )

    def test_analysis_prompt_deduplicates_narrative_activity_mentions(self):
        prompt = generate_insights.build_llm_prompt("synthetic-report")
        self.assertIn("канонические факты", prompt)
        self.assertIn("не считай её второй отдельной записью", prompt)
        self.assertIn("D+1", prompt)
        self.assertIn("недостаточно данных для вывода", prompt)


if __name__ == "__main__":
    unittest.main()
