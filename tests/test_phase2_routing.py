import datetime as dt
import json
import os
import sqlite3
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import conversation_contract as C
import conversation_router
import conversation_store
import conversation_tools
import conversation_validation
import phase2_store
import telegram_bot
import factor_capture
import daily_log
from conversation_fakes import FakeBot, TempDBCase, envelope, message, single_response

KYIV = ZoneInfo("Europe/Kyiv")


class CapturingGemini:
    def __init__(self, payload):
        self.payload = payload
        self.user_text = None

    def generate(self, system, user_text):
        self.user_text = user_text
        return SimpleNamespace(
            text=json.dumps(self.payload, ensure_ascii=False), model="fake",
            latency_ms=1, attempt_count=1,
        )


class Phase2RoutingTests(TempDBCase):
    def setUp(self):
        super().setUp()
        conn = conversation_store.connect()
        phase2_store.migrate(conn)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS recovery (
                cycle_id INTEGER PRIMARY KEY, created_at TEXT,
                recovery_score REAL, hrv_rmssd REAL, resting_hr REAL
            );
            CREATE TABLE IF NOT EXISTS sleep (
                id TEXT PRIMARY KEY, end TEXT, performance_pct REAL, raw_json TEXT
            );
            CREATE TABLE IF NOT EXISTS supplements_log (
                id INTEGER PRIMARY KEY, date TEXT, name TEXT, taken INTEGER
            );
        """)
        instant = dt.datetime(2026, 7, 13, 8, tzinfo=KYIV).astimezone(dt.timezone.utc)
        conn.execute("INSERT INTO recovery VALUES (1,?,?,?,?)",
                     (instant.isoformat(), 70, 55, 52))
        conn.commit()
        conn.close()
        self.now = dt.datetime(2026, 7, 13, 12, tzinfo=KYIV)

    def _route(self, msg_id, text, payload, session=None):
        ctx = conversation_store.ActionContext(
            source="telegram", chat_id="1", user_id="42",
            message_id=str(msg_id), input_text=text,
        )
        exec_ctx = conversation_tools.ExecContext(
            action_id="", source="telegram", chat_id="1",
            message_id=str(msg_id), local_now=self.now,
        )
        gemini = CapturingGemini(payload)
        with patch.dict(os.environ, {"CONVERSATION_ANALYTICS_V2_ENABLED": "true"}):
            outcome = conversation_router.route(
                ctx, exec_ctx, local_now=self.now, gemini=gemini,
                session_state=session,
            )
        return outcome, gemini

    def test_metric_trend_is_grounded_and_bounded(self):
        payload = envelope(C.INTENT_GET_METRIC_TREND, {
            "fact_status": "not_applicable", "metric": "hrv_rmssd",
            "window_days": 7,
        })
        outcome, _ = self._route(1, "как HRV за неделю?", payload)
        self.assertEqual(outcome.result["data"]["snapshot"], "metric_trend.v1")
        self.assertIn("Данные: 1/7", outcome.message)
        self.assertIn("не медицинский вывод", outcome.message)
        self.assertEqual(
            conversation_store.get_action(outcome.action_id)["prompt_version"],
            C.PHASE2_PROMPT_VERSION,
        )

    def test_factor_insufficient_data_is_explicit(self):
        payload = envelope(C.INTENT_GET_FACTOR_OBSERVATION, {
            "fact_status": "not_applicable", "factor_type": "supplement",
            "factor_key": "магний", "window_days": 28,
        })
        outcome, _ = self._route(2, "магний помогает?", payload)
        self.assertFalse(outcome.result["data"]["eligible"])
        self.assertIn("недостаточно", outcome.message)
        self.assertIn("Выводов не делаю", outcome.message)

    def test_validated_session_is_separate_bounded_planner_context(self):
        session = {
            "active_topic": "hrv_rmssd", "last_read_intent": "get_metric_trend",
            "last_query": {"metric": "hrv_rmssd", "window_days": 7},
            "last_evidence_sha256": "a" * 64, "turn_count": 1,
        }
        payload = envelope(C.INTENT_GET_METRIC_TREND, {
            "fact_status": "not_applicable", "metric": "hrv_rmssd",
            "window_days": 28,
        })
        _, gemini = self._route(3, "а за 28 дней?", payload, session=session)
        planner = json.loads(gemini.user_text)
        self.assertEqual(planner["current_message"], "а за 28 дней?")
        self.assertEqual(planner["validated_read_context"], session)

    def test_phase2_read_remains_available_when_legacy_flag_off(self):
        payload = envelope(C.INTENT_GET_METRIC_TREND, {
            "fact_status": "not_applicable", "metric": "hrv_rmssd",
            "window_days": 7,
        })
        ctx = conversation_store.ActionContext(
            source="telegram", chat_id="1", user_id="42",
            message_id="4", input_text="trend",
        )
        exec_ctx = conversation_tools.ExecContext(
            action_id="", source="telegram", chat_id="1", message_id="4",
            local_now=self.now,
        )
        with patch.dict(os.environ, {"CONVERSATION_ANALYTICS_V2_ENABLED": "false"}):
            out = conversation_router.route(
                ctx, exec_ctx, local_now=self.now, gemini=CapturingGemini(payload)
            )
        self.assertEqual(out.kind, "confirmation")
        self.assertEqual(out.result["data"]["snapshot"], "metric_trend.v1")
        self.assertEqual(
            conversation_store.get_action(out.action_id)["prompt_version"],
            C.PHASE2_PROMPT_VERSION,
        )

    def test_unsupported_model_prose_is_never_surfaced(self):
        payload = envelope(
            C.INTENT_UNSUPPORTED_REQUEST,
            {"reason": "medical"},
            reply_text="Magnesium definitely causes better recovery.",
        )
        outcome, _ = self._route(40, "магний точно помогает?", payload)
        self.assertEqual(outcome.kind, "unsupported")
        self.assertEqual(outcome.message, C.MSG_UNSUPPORTED_REQUEST)
        self.assertNotIn("causes", outcome.message.lower())

    def test_query_validation_rejects_model_selected_identifiers(self):
        result = conversation_validation.validate_envelope(envelope(
            C.INTENT_GET_METRIC_TREND,
            {"fact_status": "not_applicable", "metric": "raw_json", "window_days": 7},
        ))
        verdict = conversation_validation.validate(result, local_now=self.now)
        self.assertFalse(verdict.ok)

    def test_telegram_persists_only_typed_read_session(self):
        old_bot = telegram_bot.bot
        telegram_bot.bot = FakeBot()
        payload = envelope(C.INTENT_GET_METRIC_TREND, {
            "fact_status": "not_applicable", "metric": "hrv_rmssd",
            "window_days": 7,
        })
        try:
            with patch.dict(os.environ, {
                "CONVERSATION_ANALYTICS_V2_ENABLED": "true",
                "CONVERSATION_MEMORY_ENABLED": "true",
            }), patch.object(telegram_bot, "get_kiev_time", return_value=self.now), \
                 patch.object(telegram_bot, "_build_gemini_client",
                              return_value=single_response(payload)):
                telegram_bot.route_via_conversation(message(10, "private raw question"),
                                                    "private raw question")
            conn = conversation_store.connect()
            record = phase2_store.get_session(conn, "telegram", "1", "42")
            conn.close()
            encoded = json.dumps(record.state, ensure_ascii=False)
            self.assertNotIn("private raw question", encoded)
            self.assertEqual(record.state["last_query"]["metric"], "hrv_rmssd")
        finally:
            telegram_bot.bot = old_bot

    def test_factor_capture_stores_only_sanitized_observations(self):
        response = json.dumps({
            "version": factor_capture.SCHEMA_VERSION,
            "observations": [
                {"factor_key": key,
                 "state": "present" if key == "late_meal" else "unknown",
                 "confidence": 0.9}
                for key in factor_capture.FACTOR_KEYS
            ],
        })

        class FactorClient:
            def generate(self, *args, **kwargs):
                return response

        sentinel = "RAW_PRIVATE_DAILY_NOTE"
        conn = daily_log.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            daily_log.append_entry_tx(
                conn, "2026-07-12", sentinel,
                source_key="telegram:1:99:context",
            )
            conn.commit()
        finally:
            conn.close()
        with patch.dict(os.environ, {"DAILY_FACTOR_CAPTURE_ENABLED": "true"}), \
             patch.object(telegram_bot, "_build_factor_client", return_value=FactorClient()):
            created = telegram_bot._capture_daily_factors(
                "2026-07-12", sentinel, "telegram:1:99:context"
            )
        self.assertEqual(created, 1)
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT factor_key, state, source_key FROM daily_factor_observations"
        ).fetchone()
        serialized = " ".join(str(value) for value in row)
        conn.close()
        self.assertEqual(row[:2], ("late_meal", 1))
        self.assertNotIn(sentinel, serialized)


if __name__ == "__main__":
    unittest.main()
