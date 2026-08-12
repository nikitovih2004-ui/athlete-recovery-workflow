"""Router orchestration tests with fake Gemini + temp SQLite."""
import datetime as dt
import json
import os
import sqlite3
import sys
import unittest
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import conversation_contract as C
import conversation_router as router
import conversation_store as store
import conversation_tools as tools
from conversation_fakes import (
    TempDBCase, envelope, single_response, make_client, HttpResponse, ok_body,
)

KYIV = ZoneInfo("Europe/Kyiv")
NOW = dt.datetime(2026, 7, 12, 15, 0, tzinfo=KYIV)


class _RaisingClient:
    def generate(self, *a, **k):
        raise AssertionError("Gemini must not be called")


class RoutingTests(TempDBCase):
    def _route(self, message_id, gemini, *, text="msg", reply_evening_date=None,
               reply_to=None):
        ctx = store.ActionContext(source="telegram", chat_id="1", message_id=message_id,
                                  user_id="42", reply_to_message_id=reply_to, input_text=text)
        exec_ctx = tools.ExecContext(action_id="", source="telegram", chat_id="1",
                                     message_id=message_id, local_now=NOW,
                                     reply_to_message_id=reply_to)
        return router.route(ctx, exec_ctx, local_now=NOW, gemini=gemini,
                            reply_evening_date=reply_evening_date)

    def _count(self, table):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()

    def test_supplement_fact_writes(self):
        payload = envelope(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": "09:00",
            "items": [{"name": "креатин", "dose_text": "5г", "taken": True}]})
        outcome = self._route("m1", single_response(payload))
        self.assertEqual(outcome.kind, "confirmation")
        self.assertEqual(self._count("supplements_log"), 1)
        self.assertEqual(store.get_action(outcome.action_id)["status"], C.ACTION_SUCCEEDED)

    def test_plan_is_noop(self):
        payload = envelope(C.INTENT_GENERAL_CONVERSATION, {"topic": "plan"},
                           reply_text="Понял, не записываю.")
        outcome = self._route("m2", single_response(payload))
        self.assertEqual(outcome.kind, "general")
        self.assertEqual(self._count("supplements_log"), 0)
        self.assertEqual(store.get_action(outcome.action_id)["status"], C.ACTION_NOOP)

    def test_unknown_supplement_status_clarifies(self):
        payload = envelope(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": None,
            "items": [{"name": "магний", "dose_text": "400мг", "taken": None}]})
        outcome = self._route("m3", single_response(payload))
        self.assertEqual(outcome.kind, "clarification")
        self.assertIsNotNone(outcome.pending_id)
        self.assertEqual(self._count("supplements_log"), 0)
        active = store.active_pending("telegram", "1", "42")
        self.assertEqual(active["pending_id"], outcome.pending_id)

    def test_outage_writes_nothing(self):
        client = make_client([HttpResponse(503, None), HttpResponse(500, None)])
        outcome = self._route("m4", client)
        self.assertEqual(outcome.kind, "outage")
        self.assertEqual(store.get_action(outcome.action_id)["status"], C.ACTION_FAILED)
        self.assertEqual(self._count("supplements_log"), 0)

    def test_malformed_output_rejected(self):
        client = make_client([HttpResponse(200, ok_body("not json at all"))])
        outcome = self._route("m5", client)
        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(store.get_action(outcome.action_id)["status"], C.ACTION_REJECTED)

    def test_unknown_intent_rejected(self):
        client = make_client([HttpResponse(200, ok_body(envelope("drop_db", {})))])
        outcome = self._route("m6", client)
        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(self._count("supplements_log"), 0)

    def test_duplicate_message_does_not_recall_gemini(self):
        payload = envelope(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": "09:00",
            "items": [{"name": "креатин", "dose_text": "5г", "taken": True}]})
        first = self._route("dup", single_response(payload))
        self.assertEqual(first.kind, "confirmation")
        # Second time the same message id must short-circuit before Gemini.
        second = self._route("dup", _RaisingClient())
        self.assertEqual(second.kind, "duplicate")
        self.assertEqual(self._count("supplements_log"), 1)

    def test_today_status_read(self):
        payload = envelope(C.INTENT_GET_TODAY_STATUS, {})
        outcome = self._route("m7", single_response(payload))
        self.assertEqual(outcome.kind, "confirmation")
        self.assertEqual(outcome.result["data"]["snapshot"], "today_status.v1")

    def test_low_confidence_mutation_blocked(self):
        payload = envelope(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": None,
            "items": [{"name": "креатин", "dose_text": "5г", "taken": True}]},
            confidence=0.80)
        outcome = self._route("m8", single_response(payload))
        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(self._count("supplements_log"), 0)


class FactStatusClarificationTests(TempDBCase):
    def _ctx(self, message_id, text, reply_to=None):
        ctx = store.ActionContext(source="telegram", chat_id="1", message_id=message_id,
                                  user_id="42", reply_to_message_id=reply_to, input_text=text)
        exec_ctx = tools.ExecContext(action_id="", source="telegram", chat_id="1",
                                     message_id=message_id, local_now=NOW,
                                     reply_to_message_id=reply_to)
        return ctx, exec_ctx

    def _count(self, table):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()

    def _open_pending(self, message_id, entries=None):
        payload = envelope(C.INTENT_LOG_STRENGTH, {
            "date_ref": {"kind": "today", "value": None},
            "entries": entries or [{"exercise_name": "жим", "weight_kg": 80, "sets": 4, "reps": 8}],
        })  # no fact_status key at all - the fallback-omitted-it scenario
        ctx, exec_ctx = self._ctx(message_id, "жим 80 4х8")
        outcome = router.route(ctx, exec_ctx, local_now=NOW, gemini=single_response(payload))
        self.assertEqual(outcome.kind, "clarification")
        return store.active_pending("telegram", "1", "42")

    def test_missing_fact_status_opens_clarification_with_zero_writes(self):
        pending = self._open_pending("f1")
        self.assertIsNotNone(pending)
        self.assertIn("fact_status", json.loads(pending["missing_fields_json"]))
        self.assertEqual(self._count("workout_exercises"), 0)

    def test_affirmative_reply_writes_the_original_entries(self):
        pending = self._open_pending("f2")
        ctx, exec_ctx = self._ctx("f3", "да, выполнил",
                                  reply_to=pending["clarification_question_message_id"])
        outcome = router.resolve_fact_status_pending(pending, ctx, exec_ctx)
        self.assertEqual(outcome.kind, "confirmation")
        self.assertEqual(self._count("workout_exercises"), 1)
        self.assertIsNone(store.active_pending("telegram", "1", "42"))

    def test_negative_reply_is_noop_no_write(self):
        pending = self._open_pending("f4")
        ctx, exec_ctx = self._ctx("f5", "нет, только планирую",
                                  reply_to=pending["clarification_question_message_id"])
        outcome = router.resolve_fact_status_pending(pending, ctx, exec_ctx)
        self.assertEqual(outcome.kind, "general")
        self.assertEqual(self._count("workout_exercises"), 0)
        self.assertIsNone(store.active_pending("telegram", "1", "42"))

    def test_unclear_reply_rejects_without_second_clarification(self):
        pending = self._open_pending("f6")
        ctx, exec_ctx = self._ctx("f7", "может быть",
                                  reply_to=pending["clarification_question_message_id"])
        outcome = router.resolve_fact_status_pending(pending, ctx, exec_ctx)
        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(self._count("workout_exercises"), 0)
        self.assertIsNone(store.active_pending("telegram", "1", "42"))

    def test_duplicate_resolving_reply_does_not_double_write(self):
        pending = self._open_pending("f8")
        ctx, exec_ctx = self._ctx("f9", "да",
                                  reply_to=pending["clarification_question_message_id"])
        first = router.resolve_fact_status_pending(pending, ctx, exec_ctx)
        self.assertEqual(first.kind, "confirmation")
        self.assertEqual(self._count("workout_exercises"), 1)

        second_ctx, second_exec_ctx = self._ctx(
            "f9", "да", reply_to=pending["clarification_question_message_id"])
        second = router.resolve_fact_status_pending(pending, second_ctx, second_exec_ctx)
        self.assertEqual(second.kind, "duplicate")
        self.assertEqual(self._count("workout_exercises"), 1)


class StrengthGuardTests(TempDBCase):
    """Deterministic guards against the two observed Gemini parse failures:
    dropped reps (volume-0 garbage) and a mis-parsed absolute date."""

    def _route(self, message_id, gemini, *, text="msg"):
        ctx = store.ActionContext(source="telegram", chat_id="1", message_id=message_id,
                                  user_id="42", input_text=text)
        exec_ctx = tools.ExecContext(action_id="", source="telegram", chat_id="1",
                                     message_id=message_id, local_now=NOW)
        return router.route(ctx, exec_ctx, local_now=NOW, gemini=gemini)

    def _completed_strength(self, entries, *, date_ref=None):
        return envelope(C.INTENT_LOG_STRENGTH, {
            "date_ref": date_ref or {"kind": "today", "value": None},
            "fact_status": C.FACT_STATUS_COMPLETED,
            "entries": entries,
        })

    def _count(self, table):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()

    def test_weight_but_no_reps_is_rejected_without_write(self):
        payload = self._completed_strength(
            [{"exercise_name": "жим", "weight_kg": 30, "sets": None, "reps": None}])
        outcome = self._route("g1", single_response(payload))
        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(outcome.message, C.MSG_STRENGTH_MISSING_REPS)
        self.assertEqual(self._count("workout_exercises"), 0)

    def test_one_entry_missing_reps_rejects_the_whole_log(self):
        payload = self._completed_strength([
            {"exercise_name": "жим", "weight_kg": 30, "sets": 1, "reps": 9},
            {"exercise_name": "тяга", "weight_kg": 40, "sets": 1, "reps": None},
        ])
        outcome = self._route("g2", single_response(payload))
        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(self._count("workout_exercises"), 0)

    def test_bodyweight_entry_without_weight_is_allowed(self):
        # No weight -> volume 0 is legitimate (bodyweight), reps present.
        payload = self._completed_strength(
            [{"exercise_name": "подтягивания", "weight_kg": None, "sets": 1, "reps": 10}])
        outcome = self._route("g3", single_response(payload))
        self.assertEqual(outcome.kind, "confirmation")
        self.assertEqual(self._count("workout_exercises"), 1)

    def test_weight_float_artifact_is_rounded_on_write(self):
        payload = self._completed_strength(
            [{"exercise_name": "жим", "weight_kg": 30.000000000000004,
              "sets": 1, "reps": 9}])
        outcome = self._route("g4", single_response(payload))
        self.assertEqual(outcome.kind, "confirmation")
        conn = sqlite3.connect(self.db)
        try:
            w, vol = conn.execute(
                "SELECT weight, volume FROM workout_exercises "
                "WHERE deleted_at IS NULL").fetchone()
        finally:
            conn.close()
        self.assertEqual(w, 30.0)
        self.assertEqual(vol, 270.0)

    def test_old_absolute_date_appends_warning(self):
        payload = self._completed_strength(
            [{"exercise_name": "жим", "weight_kg": 30, "sets": 1, "reps": 9}],
            date_ref={"kind": "absolute", "value": "2026-07-01"})
        outcome = self._route("g5", single_response(payload))
        self.assertEqual(outcome.kind, "confirmation")
        self.assertIn("⚠️", outcome.message)
        self.assertIn("2026-07-01", outcome.message)
        self.assertIn("удали силовую", outcome.message)

    def test_today_log_has_no_date_warning(self):
        payload = self._completed_strength(
            [{"exercise_name": "жим", "weight_kg": 30, "sets": 1, "reps": 9}])
        outcome = self._route("g6", single_response(payload))
        self.assertEqual(outcome.kind, "confirmation")
        self.assertNotIn("⚠️", outcome.message)


if __name__ == "__main__":
    unittest.main()
