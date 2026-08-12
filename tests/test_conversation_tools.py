"""Integration tests for the atomic tools + read models (temp SQLite)."""
import datetime as dt
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import conversation_contract as C
import conversation_store as store
import conversation_tools as tools
import workouts_db
from conversation_fakes import TempDBCase

KYIV = ZoneInfo("Europe/Kyiv")
NOW = dt.datetime(2026, 7, 12, 15, 0, tzinfo=KYIV)


class ToolTests(TempDBCase):
    def _ctx(self, message_id="m1"):
        ctx = store.ActionContext(source="telegram", chat_id="1", message_id=message_id,
                                  user_id="42", input_text="text")
        res = store.reserve(ctx)
        exec_ctx = tools.ExecContext(action_id=res.action_id, source="telegram",
                                     chat_id="1", message_id=message_id, local_now=NOW)
        return exec_ctx

    def _count(self, table):
        conn = sqlite3.connect(self.db)
        try:
            try:
                return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                return 0
        finally:
            conn.close()

    def _status(self, action_id):
        return store.get_action(action_id)["status"]

    def test_strength_writes_rows_and_audit(self):
        ctx = self._ctx()
        args = {"entries": [
            {"exercise_name": "жим", "weight_kg": 80, "sets": 4, "reps": 8},
            {"exercise_name": "тяга", "weight_kg": 100, "sets": 3, "reps": 5},
        ], "resolved_date": "2026-07-12"}
        result = tools.execute("log_strength_workout", args, ctx)
        self.assertEqual(result["created_count"], 2)
        self.assertEqual(self._count("workout_exercises"), 2)
        self.assertEqual(self._status(ctx.action_id), C.ACTION_SUCCEEDED)

    def test_supplement_taken_false_stored_as_zero(self):
        ctx = self._ctx()
        args = {"time": "22:00", "items": [
            {"name": "магний", "dose_text": "400мг", "taken": False}],
            "resolved_date": "2026-07-12"}
        tools.execute("log_supplement", args, ctx)
        conn = sqlite3.connect(self.db)
        taken = conn.execute("SELECT taken FROM supplements_log").fetchone()[0]
        conn.close()
        self.assertEqual(taken, 0)

    def test_idempotent_rerun_inserts_nothing(self):
        ctx = self._ctx()
        args = {"entries": [{"exercise_name": "жим", "weight_kg": 80, "sets": 1, "reps": 5}],
                "resolved_date": "2026-07-12"}
        tools.execute("log_strength_workout", args, ctx)
        second = tools.execute("log_strength_workout", args, ctx)
        self.assertEqual(second["created_count"], 0)
        self.assertEqual(self._count("workout_exercises"), 1)

    def test_atomic_batch_rollback_on_mid_failure(self):
        ctx = self._ctx()
        args = {"time": "09:00", "items": [
            {"name": "a", "dose_text": None, "taken": True},
            {"name": "b", "dose_text": None, "taken": True},
        ], "resolved_date": "2026-07-12"}

        real = workouts_db.insert_supplement_row
        calls = {"n": 0}

        def flaky(conn, *a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected fault")
            return real(conn, *a, **k)

        with patch.object(tools.workouts_db, "insert_supplement_row", flaky):
            with self.assertRaises(RuntimeError):
                tools.execute("log_supplement", args, ctx)

        # Whole batch rolled back; audit not flipped to succeeded.
        self.assertEqual(self._count("supplements_log"), 0)
        self.assertNotEqual(self._status(ctx.action_id), C.ACTION_SUCCEEDED)

    def test_daily_context_appends(self):
        ctx = self._ctx()
        args = {"notes": "поздно лёг", "resolved_date": "2026-07-11"}
        tools.execute("save_daily_context", args, ctx)
        conn = sqlite3.connect(self.db)
        notes = conn.execute("SELECT notes FROM daily_log WHERE date='2026-07-11'").fetchone()[0]
        conn.close()
        self.assertIn("поздно лёг", notes)

    def test_daily_context_notes_never_persisted_in_audit_ledger(self):
        ctx = self._ctx()
        secret_notes = "выпил, поругался с женой, не спал"
        args = {"notes": secret_notes, "resolved_date": "2026-07-11"}
        tools.execute("save_daily_context", args, ctx)
        row = store.get_action(ctx.action_id)
        self.assertNotIn(secret_notes, row["validated_arguments_json"] or "")
        self.assertNotIn(secret_notes, row["result_json"] or "")

    def test_two_scripted_sets_are_stored_as_two_distinct_rows(self):
        # "30x9, 30x8" resolved to two entries upstream; confirm storage keeps
        # them distinct with the exact values given, no fabrication/merging.
        ctx = self._ctx()
        args = {"entries": [
            {"exercise_name": "жим", "weight_kg": 30, "sets": 1, "reps": 9},
            {"exercise_name": "жим", "weight_kg": 30, "sets": 1, "reps": 8},
        ], "resolved_date": "2026-07-12"}
        result = tools.execute("log_strength_workout", args, ctx)
        self.assertEqual(result["created_count"], 2)
        conn = sqlite3.connect(self.db)
        rows = conn.execute(
            "SELECT weight, reps FROM workout_exercises ORDER BY reps DESC"
        ).fetchall()
        conn.close()
        self.assertEqual(rows, [(30, 9), (30, 8)])

    def test_read_today_status_is_side_effect_free(self):
        ctx = self._ctx()
        result = tools.execute("get_today_status", {}, ctx)
        self.assertEqual(result["data"]["snapshot"], "today_status.v1")
        self.assertEqual(self._status(ctx.action_id), C.ACTION_SUCCEEDED)
        # No domain tables were created by a read.
        self.assertEqual(self._count("workout_exercises"), 0)


if __name__ == "__main__":
    unittest.main()
