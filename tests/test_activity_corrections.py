import datetime as dt
import json
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import canonical_read_model
import conversation_contract as C
import conversation_store as store
import conversation_tools
import data_integrity
import telegram_bot
import activity_corrections
from conversation_fakes import FakeBot, TempDBCase, envelope, message, single_response


NOW = dt.datetime(2026, 7, 16, 18, 30, tzinfo=dt.timezone(
    dt.timedelta(hours=3)
))


class NoGemini:
    def generate(self, *args, **kwargs):
        raise AssertionError("deterministic correction must not call provider")


class ActivityCorrectionE2E(TempDBCase):
    def setUp(self):
        super().setUp()
        conn = store.connect()
        telegram_bot.phase2_store.migrate(conn)
        telegram_bot.morning_context.ensure_table(conn)
        conn.commit()
        conn.close()
        self.real_bot = telegram_bot.bot
        self.bot = FakeBot()
        telegram_bot.bot = self.bot
        self.now_patch = patch.object(telegram_bot, "get_kiev_time", return_value=NOW)
        self.now_patch.start()

    def tearDown(self):
        self.now_patch.stop()
        telegram_bot.bot = self.real_bot
        super().tearDown()

    def send(self, msg, gemini=NoGemini()):
        # These cases exercise the retained feature-flagged rollback path.
        with patch.object(telegram_bot.phase2_flags, "bounded_agent_enabled", return_value=False), \
                patch.object(telegram_bot, "_build_gemini_client", return_value=gemini):
            telegram_bot.route_via_conversation(msg, msg.text)

    def log_strength(self, message_id=1):
        entries = []
        for name, weight, reps in (
            ("жим", 100, (10, 7)), ("молотки", 17.5, (8, 6)),
            ("махи", 12.5, (15, 13)), ("трицепс", 43, (10, 8)),
            ("бицепс", 41, (10, 6)), ("тяга", 52, (9, 7)),
        ):
            entries.extend({
                "exercise_name": name, "weight_kg": weight, "sets": 1, "reps": rep,
            } for rep in reps)
        payload = envelope(C.INTENT_LOG_STRENGTH, {
            "date_ref": {"kind": "today", "value": None},
            "fact_status": "completed", "entries": entries,
        })
        self.send(message(message_id, "explicit strength"), single_response(payload))

    def test_move_latest_strength_requires_confirmation_and_is_atomic(self):
        self.log_strength()
        self.send(message(2, "перенеси последнюю тренировку на вчера"))
        self.assertIn("да, перенести", self.bot.sent[-1].text.casefold())
        question_id = self.bot.sent[-1].message_id
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM workout_exercises WHERE deleted_at IS NULL"
        ).fetchone()[0], 12)
        conn.close()

        reply = message(3, "да, перенести", reply_to_message_id=question_id)
        self.send(reply)
        self.send(reply)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM workout_exercises "
                "WHERE date='2026-07-16' AND deleted_at IS NULL"
            ).fetchone()[0], 0)
            rows = conn.execute(
                "SELECT exercise_name,weight,reps FROM workout_exercises "
                "WHERE date='2026-07-15' AND deleted_at IS NULL ORDER BY id"
            ).fetchall()
            self.assertEqual(len(rows), 12)
            correction = conn.execute(
                "SELECT action_id FROM conversation_actions "
                "WHERE message_id='3' AND tool_name='correct_activity'"
            ).fetchone()[0]
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM action_domain_links WHERE action_id=?",
                (correction,),
            ).fetchone()[0], 12)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM domain_events WHERE action_id=?",
                (correction,),
            ).fetchone()[0], 24)
            self.assertEqual(conn.execute(
                "SELECT status FROM pending_actions "
                "WHERE claimed_by_action_id=?",
                (correction,),
            ).fetchone()[0], C.PENDING_RESOLVED)
            snapshot = canonical_read_model.range_snapshot(
                conn, "2026-07-15", "2026-07-16"
            )
            self.assertEqual(
                len(snapshot["days"][0]["activities"]["manual_strength"]), 12
            )
            self.assertEqual(
                len(snapshot["days"][1]["activities"]["manual_strength"]), 0
            )
        finally:
            conn.close()
        report = data_integrity.audit_database(self.db)
        self.assertTrue(report["ok"], report["issues"])
        reopened = sqlite3.connect(self.db)
        try:
            self.assertEqual(reopened.execute(
                "SELECT COUNT(*) FROM workout_exercises "
                "WHERE date='2026-07-15' AND deleted_at IS NULL"
            ).fetchone()[0], 12)
            self.assertEqual(reopened.execute(
                "SELECT COUNT(*) FROM pending_actions WHERE status='resolving'"
            ).fetchone()[0], 0)
        finally:
            reopened.close()

    def test_bare_delete_targets_latest_and_requires_exact_reply(self):
        self.log_strength()
        self.send(message(10, "удали"))
        question_id = self.bot.sent[-1].message_id
        self.send(message(11, "да", reply_to_message_id=question_id))
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM workout_exercises WHERE deleted_at IS NULL"
        ).fetchone()[0], 12)
        conn.close()
        self.send(message(12, "да, удалить", reply_to_message_id=question_id))
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM workout_exercises WHERE deleted_at IS NULL"
            ).fetchone()[0], 0)
            correction = conn.execute(
                "SELECT action_id FROM conversation_actions WHERE message_id='12'"
            ).fetchone()[0]
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM domain_events "
                "WHERE action_id=? AND event_type='soft_deleted'",
                (correction,),
            ).fetchone()[0], 12)
        finally:
            conn.close()
        self.assertTrue(data_integrity.audit_database(self.db)["ok"])

    def test_incident_repair_targets_exact_origin_and_preserves_other_workout(self):
        self.log_strength(message_id=50)
        self.log_strength(message_id=51)
        conn = sqlite3.connect(self.db)
        first_origin = conn.execute(
            "SELECT action_id FROM conversation_actions WHERE message_id='50'"
        ).fetchone()[0]
        second_origin = conn.execute(
            "SELECT action_id FROM conversation_actions WHERE message_id='51'"
        ).fetchone()[0]
        conn.close()
        preview_ctx = store.ActionContext(
            source="maintenance", chat_id="1", user_id="42", message_id="repair-preview",
            input_text="verified exact-origin repair preview",
        )
        preview = activity_corrections.propose(
            preview_ctx, NOW,
            activity_corrections.exact_origin_delete_request(first_origin),
        )
        self.assertEqual(preview["kind"], "clarification")
        store.set_clarification_message_id(preview["pending_id"], "repair-question")
        pending = store.get_pending(preview["pending_id"])
        confirm_ctx = store.ActionContext(
            source="maintenance", chat_id="1", user_id="42", message_id="repair-confirm",
            reply_to_message_id="repair-question", input_text="да, удалить",
        )
        exec_ctx = conversation_tools.ExecContext(
            action_id="", source="maintenance", chat_id="1", message_id="repair-confirm",
            reply_to_message_id="repair-question", local_now=NOW,
        )
        result = activity_corrections.resolve(pending, confirm_ctx, exec_ctx)
        self.assertEqual(result["kind"], "confirmation")
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM workout_exercises WHERE origin_action_id=? "
                "AND deleted_at IS NULL", (first_origin,),
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM workout_exercises WHERE origin_action_id=? "
                "AND deleted_at IS NULL", (second_origin,),
            ).fetchone()[0], 12)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM domain_events WHERE action_id=? "
                "AND event_type='soft_deleted'", (result["action_id"],),
            ).fetchone()[0], 12)
            reasons = [row[0] for row in conn.execute(
                "SELECT reason FROM domain_events WHERE action_id=? "
                "AND event_type='soft_deleted'", (result["action_id"],),
            )]
            self.assertEqual(set(reasons), {"verified_data_repair"})
        finally:
            conn.close()
        self.assertTrue(data_integrity.audit_database(self.db)["ok"])

    def test_cancel_and_wrong_user_do_not_mutate(self):
        self.log_strength()
        self.send(message(20, "удали"))
        question_id = self.bot.sent[-1].message_id
        self.send(message(
            21, "да, удалить", user_id=99, reply_to_message_id=question_id
        ))
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM workout_exercises WHERE deleted_at IS NULL"
        ).fetchone()[0], 12)
        conn.close()
        self.send(message(22, "нет", reply_to_message_id=question_id))
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM workout_exercises WHERE deleted_at IS NULL"
        ).fetchone()[0], 12)
        conn.close()

    def test_move_failure_rolls_back_copies_and_deletes(self):
        self.log_strength()
        self.send(message(30, "перенеси последнюю тренировку на вчера"))
        question_id = self.bot.sent[-1].message_id
        real_link = activity_corrections.workouts_db.link_action_domain
        calls = {"count": 0}

        def fail_second(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise sqlite3.IntegrityError("injected")
            return real_link(*args, **kwargs)

        with patch.object(
            activity_corrections.workouts_db, "link_action_domain",
            side_effect=fail_second,
        ):
            self.send(message(
                31, "да, перенести", reply_to_message_id=question_id
            ))
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM workout_exercises "
                "WHERE date='2026-07-16' AND deleted_at IS NULL"
            ).fetchone()[0], 12)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM workout_exercises WHERE date='2026-07-15'"
            ).fetchone()[0], 0)
        finally:
            conn.close()
        self.assertTrue(data_integrity.audit_database(self.db)["ok"])

    def test_invalid_or_out_of_policy_target_date_never_reaches_domain(self):
        self.log_strength()
        for message_id, text in (
            (40, "перенеси последнюю силовую на 2026-99-99"),
            (41, "перенеси последнюю силовую на 2099-01-01"),
        ):
            self.send(message(message_id, text))
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM workout_exercises "
                "WHERE date='2026-07-16' AND deleted_at IS NULL"
            ).fetchone()[0], 12)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM workout_exercises "
                "WHERE date!='2026-07-16'"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM pending_actions "
                "WHERE candidate_intent='correct_logged_activity'"
            ).fetchone()[0], 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
