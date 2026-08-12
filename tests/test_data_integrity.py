import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

import conversation_store
import conversation_tools
import daily_log
import data_integrity
import morning_context
import phase2_store
import workouts_db


NOW = dt.datetime(2026, 7, 13, 12, 0, tzinfo=dt.timezone.utc)


class DataIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "whoop.db")
        self.old_workouts = workouts_db.DB_PATH
        self.old_daily = daily_log.DB_PATH
        workouts_db.DB_PATH = self.db
        daily_log.DB_PATH = self.db
        conn = conversation_store.connect()
        phase2_store.migrate(conn)
        workouts_db.ensure_tables(conn)
        morning_context.ensure_table(conn)
        conn.close()

    def tearDown(self):
        workouts_db.DB_PATH = self.old_workouts
        daily_log.DB_PATH = self.old_daily
        self.temp.cleanup()

    def _action_ctx(self, message_id="m1"):
        reservation = conversation_store.reserve(conversation_store.ActionContext(
            source="telegram", chat_id="1", user_id="42",
            message_id=message_id, input_text="fact",
        ))
        return conversation_tools.ExecContext(
            action_id=reservation.action_id, source="telegram", chat_id="1",
            message_id=message_id, local_now=NOW,
            processing_token=reservation.processing_token,
            processing_fence=reservation.processing_fence,
        )

    def _write_strength(self, message_id="m1"):
        ctx = self._action_ctx(message_id)
        result = conversation_tools.execute("log_strength_workout", {
            "entries": [{
                "exercise_name": "жим", "weight_kg": 80,
                "sets": 4, "reps": 8,
            }],
            "resolved_date": "2026-07-13",
        }, ctx)
        return ctx, result

    def test_database_identity_is_stable_and_legacy_rows_are_allowed(self):
        conn = workouts_db.connect()
        first = conn.execute(
            "SELECT database_uuid, generation FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        conn.execute(
            """INSERT INTO workout_exercises
               (date, exercise_name, weight, sets, reps, volume, raw_text, source_key)
               VALUES ('2026-07-12', 'legacy', 10, 1, 5, 50, '', 'legacy:1')"""
        )
        conn.commit()
        conn.close()

        conn = workouts_db.connect()
        second = conn.execute(
            "SELECT database_uuid, generation FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        conn.close()
        self.assertEqual(tuple(first), tuple(second))
        report = data_integrity.audit_database(self.db)
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["legacy_rows"]["strength"], 1)

    def test_conversation_mutation_commits_row_link_event_and_hash(self):
        ctx, result = self._write_strength()
        self.assertEqual(result["created_count"], 1)
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            """SELECT origin_action_id, created_at, updated_at, deleted_at
               FROM workout_exercises WHERE id = ?""",
            (result["record_ids"][0],),
        ).fetchone()
        link = conn.execute(
            """SELECT action_id, entity_type, entity_id, row_hash
               FROM action_domain_links"""
        ).fetchone()
        event = conn.execute(
            "SELECT event_type, action_id FROM domain_events"
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], ctx.action_id)
        self.assertIsNotNone(row[1])
        self.assertEqual(row[1], row[2])
        self.assertIsNone(row[3])
        self.assertEqual(link[:3], (ctx.action_id, "strength", result["record_ids"][0]))
        self.assertEqual(len(link[3]), 64)
        self.assertEqual(event, ("created", ctx.action_id))
        self.assertTrue(data_integrity.audit_database(self.db)["ok"])

    def test_hash_mismatch_is_detected(self):
        _, result = self._write_strength()
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE workout_exercises SET weight = 81 WHERE id = ?",
            (result["record_ids"][0],),
        )
        conn.commit()
        conn.close()
        report = data_integrity.audit_database(self.db)
        self.assertFalse(report["ok"])
        self.assertIn("row_hash_mismatch", {item["code"] for item in report["issues"]})

    def test_missing_link_for_succeeded_action_is_detected(self):
        _, _result = self._write_strength()
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TRIGGER trg_action_domain_links_immutable_delete")
        conn.execute("DELETE FROM action_domain_links")
        conn.commit()
        conn.close()
        report = data_integrity.audit_database(self.db)
        codes = {item["code"] for item in report["issues"]}
        self.assertIn("domain_row_without_link", codes)
        self.assertIn("succeeded_action_link_count_mismatch", codes)

    def test_replace_cannot_bypass_hard_delete_guard(self):
        _, result = self._write_strength()
        row_id = result["record_ids"][0]
        conn = workouts_db.connect()
        self.assertEqual(conn.execute("PRAGMA recursive_triggers").fetchone()[0], 1)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "hard delete blocked"):
            conn.execute(
                """INSERT OR REPLACE INTO workout_exercises
                   (id, date, exercise_name, weight, sets, reps, volume, source_key)
                   VALUES (?, '2026-07-13', 'tamper', 1, 1, 1, 1, 'replace:1')""",
                (row_id,),
            )
        conn.rollback()
        row = conn.execute(
            "SELECT exercise_name FROM workout_exercises WHERE id=?", (row_id,)
        ).fetchone()
        conn.close()
        self.assertNotEqual(row[0], "tamper")

    def test_provenance_ledgers_are_immutable(self):
        self._write_strength()
        conn = workouts_db.connect()
        for sql in (
            "UPDATE action_domain_links SET row_hash='x'",
            "DELETE FROM action_domain_links",
            "UPDATE domain_events SET reason='x'",
            "DELETE FROM domain_events",
        ):
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    conn.execute(sql)
                conn.rollback()
        conn.close()

    def test_missing_created_event_is_detected(self):
        self._write_strength()
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TRIGGER trg_domain_events_immutable_delete")
        conn.execute("DELETE FROM domain_events WHERE event_type='created'")
        conn.commit()
        conn.close()
        report = data_integrity.audit_database(self.db)
        self.assertIn(
            "invalid_created_event_count", {item["code"] for item in report["issues"]}
        )

    def test_created_event_tamper_is_detected(self):
        self._write_strength()
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TRIGGER trg_domain_events_immutable_update")
        conn.execute(
            """UPDATE domain_events
               SET action_id='wrong', source_key='wrong', after_hash='wrong', reason='wrong'
               WHERE event_type='created'"""
        )
        conn.commit()
        conn.close()
        codes = {item["code"] for item in data_integrity.audit_database(self.db)["issues"]}
        self.assertTrue({
            "created_event_actor_mismatch", "created_event_source_mismatch",
            "created_event_hash_mismatch", "created_event_reason_invalid",
        }.issubset(codes))

    def test_event_order_and_orphan_are_detected(self):
        ctx, result = self._write_strength()
        conn = workouts_db.connect()
        conn.execute("BEGIN IMMEDIATE")
        workouts_db.soft_delete_activity(
            conn, "strength", result["record_ids"][0],
            deleted_by_action_id=f"delete:{ctx.action_id}", reason="correction",
        )
        conn.commit()
        conn.execute("DROP TRIGGER trg_domain_events_immutable_update")
        conn.execute("UPDATE domain_events SET event_id=99 WHERE event_type='created'")
        conn.execute(
            """INSERT INTO domain_events
               (entity_type, entity_id, event_type, action_id, source_key,
                before_hash, after_hash, reason, created_at)
               VALUES ('strength', 999999, 'created', 'ghost', 'ghost', NULL,
                       'hash', 'conversation mutation', '2026-07-13T00:00:00+00:00')"""
        )
        conn.commit()
        conn.close()
        codes = {item["code"] for item in data_integrity.audit_database(self.db)["issues"]}
        self.assertIn("domain_event_order_invalid", codes)
        self.assertIn("orphan_domain_event", codes)

    def test_historical_succeeded_record_ids_are_always_checked(self):
        workouts_db.connect().close()
        ctx = self._action_ctx("historical")
        conn = sqlite3.connect(self.db)
        conn.execute(
            """UPDATE conversation_actions
               SET status='succeeded', tool_name='log_strength_workout',
                   result_json=?, completed_at='2000-01-01T00:00:00+00:00'
               WHERE action_id=?""",
            ('{"created_count":1,"record_ids":[999999],"data":{"entries":1}}',
             ctx.action_id),
        )
        conn.commit()
        conn.close()
        codes = {item["code"] for item in data_integrity.audit_database(self.db)["issues"]}
        self.assertIn("succeeded_action_missing_domain_row", codes)

    def _historical_action_with_reconciliation_setup(self, message_id):
        """A historical action whose original row (id 999999) is gone, exactly
        like the always-checked test above, plus a real recovered row linked
        to the same action - as a controlled recovery would leave it."""
        ctx = self._action_ctx(message_id)
        conn = sqlite3.connect(self.db)
        conn.execute(
            """UPDATE conversation_actions
               SET status='succeeded', tool_name='log_strength_workout',
                   result_json=?, completed_at='2000-01-01T00:00:00+00:00'
               WHERE action_id=?""",
            ('{"created_count":1,"record_ids":[999999],"data":{"entries":1}}',
             ctx.action_id),
        )
        conn.commit()
        conn.close()

        conn = workouts_db.connect()
        conn.execute("BEGIN IMMEDIATE")
        recovered_id = workouts_db.insert_workout_row(
            conn, "2026-07-12", "recovered exercise", 10, 1, 5,
            source_key=f"recovered:{message_id}", origin_action_id=ctx.action_id,
        )
        workouts_db.link_action_domain(
            conn, ctx.action_id, "strength", recovered_id, f"recovered:{message_id}",
        )
        conn.commit()
        conn.close()
        return ctx, recovered_id

    def test_documented_reconciliation_clears_missing_domain_row(self):
        ctx, recovered_id = self._historical_action_with_reconciliation_setup("recon-1")

        conn = workouts_db.connect()
        conn.execute("BEGIN IMMEDIATE")
        workouts_db.record_reconciliation(
            conn, ctx.action_id, "strength",
            original_record_ids=[999999], recovered_record_ids=[recovered_id],
            reason="test: recovered from backup after historical loss",
        )
        conn.commit()
        conn.close()

        report = data_integrity.audit_database(self.db)
        codes = {item["code"] for item in report["issues"]}
        self.assertNotIn("succeeded_action_missing_domain_row", codes)
        self.assertEqual(len(report.get("reconciliations", [])), 1)
        self.assertEqual(report["reconciliations"][0]["action_id"], ctx.action_id)
        self.assertEqual(report["reconciliations"][0]["recovered_record_ids"], [recovered_id])

    def test_reconciliation_is_ignored_when_original_ids_do_not_match_ledger(self):
        ctx, recovered_id = self._historical_action_with_reconciliation_setup("recon-2")

        conn = workouts_db.connect()
        conn.execute("BEGIN IMMEDIATE")
        # Reconciliation recorded for the wrong original id - must not paper
        # over a mismatch it doesn't actually account for.
        workouts_db.record_reconciliation(
            conn, ctx.action_id, "strength",
            original_record_ids=[123456], recovered_record_ids=[recovered_id],
            reason="test: wrong original id",
        )
        conn.commit()
        conn.close()

        report = data_integrity.audit_database(self.db)
        codes = {item["code"] for item in report["issues"]}
        self.assertIn("succeeded_action_missing_domain_row", codes)
        self.assertEqual(report.get("reconciliations", []), [])

    def test_record_reconciliation_rejects_nonexistent_recovered_ids(self):
        ctx, _ = self._write_strength()
        conn = workouts_db.connect()
        conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "must all exist"):
            workouts_db.record_reconciliation(
                conn, ctx.action_id, "strength",
                original_record_ids=[999999], recovered_record_ids=[8675309],
                reason="test",
            )
        conn.rollback()
        conn.close()

    def test_record_reconciliation_rejects_ids_not_linked_to_action(self):
        ctx, result = self._write_strength("m1")
        recovered_id = result["record_ids"][0]
        other_ctx, other_result = self._write_strength("m2")
        conn = workouts_db.connect()
        conn.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "must exactly match"):
            # recovered_id is a real row, but it belongs to a different action.
            workouts_db.record_reconciliation(
                conn, other_ctx.action_id, "strength",
                original_record_ids=[999999], recovered_record_ids=[recovered_id],
                reason="test",
            )
        conn.rollback()
        conn.close()

    def test_recovery_reconciliations_table_is_immutable(self):
        ctx, result = self._write_strength()
        recovered_id = result["record_ids"][0]
        conn = workouts_db.connect()
        conn.execute("BEGIN IMMEDIATE")
        workouts_db.record_reconciliation(
            conn, ctx.action_id, "strength",
            original_record_ids=[999999], recovered_record_ids=[recovered_id],
            reason="test",
        )
        conn.commit()
        for sql in (
            "UPDATE recovery_reconciliations SET reason='x'",
            "DELETE FROM recovery_reconciliations",
        ):
            with self.subTest(sql=sql):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                    conn.execute(sql)
                conn.rollback()
        conn.close()

    def test_post_install_row_without_provenance_is_detected(self):
        conn = workouts_db.connect()
        installed_at = conn.execute(
            "SELECT created_at FROM database_meta WHERE singleton_id=1"
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO workout_exercises
               (date, exercise_name, weight, sets, reps, volume, source_key,
                created_at, updated_at)
               VALUES ('2026-07-13', 'untracked', 10, 1, 5, 50, 'untracked:1', ?, ?)""",
            (installed_at, installed_at),
        )
        conn.commit()
        conn.close()
        codes = {item["code"] for item in data_integrity.audit_database(self.db)["issues"]}
        self.assertIn("post_install_row_without_provenance", codes)

    def test_hard_delete_is_blocked_and_soft_delete_is_audited(self):
        ctx, result = self._write_strength()
        row_id = result["record_ids"][0]
        conn = workouts_db.connect()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "hard delete blocked"):
            conn.execute("DELETE FROM workout_exercises WHERE id = ?", (row_id,))
        conn.rollback()
        conn.execute("BEGIN IMMEDIATE")
        self.assertTrue(workouts_db.soft_delete_activity(
            conn, "strength", row_id,
            deleted_by_action_id=f"delete:{ctx.action_id}", reason="user correction",
        ))
        conn.commit()
        deleted = conn.execute(
            "SELECT deleted_at, deleted_by_action_id FROM workout_exercises WHERE id = ?",
            (row_id,),
        ).fetchone()
        events = conn.execute(
            "SELECT event_type FROM domain_events WHERE entity_type='strength' AND entity_id=?",
            (row_id,),
        ).fetchall()
        conn.close()
        self.assertIsNotNone(deleted[0])
        self.assertEqual(deleted[1], f"delete:{ctx.action_id}")
        self.assertEqual([row[0] for row in events], ["created", "soft_deleted"])
        self.assertTrue(data_integrity.audit_database(self.db)["ok"])

    def test_creatine_total_is_taken_only_and_unit_aware(self):
        rows = [
            ("5g", 1), ("5000 мг", 1), ("1000000mcg", 1),
            ("3 capsules", 1), ("10g", 0), (None, 1),
        ]
        for index, (dose, taken) in enumerate(rows):
            workouts_db.log_supplement(
                "2026-07-13", "09:00", "Creatine", dose, taken=taken,
                source_key=f"legacy:supp:{index}",
            )
        summary = workouts_db.get_creatine_summary()
        self.assertAlmostEqual(summary["total_grams"], 11.0)
        self.assertEqual(summary["parsed_count"], 3)
        self.assertEqual(summary["excluded_count"], 2)
        self.assertEqual(summary["taken_creatine_rows"], 5)
        self.assertEqual(workouts_db.get_accumulated_creatine(), (11.0, 3))

    def test_lost_action_lease_rolls_back_domain_row_and_link(self):
        ctx = self._action_ctx()
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE conversation_actions SET processing_fence=processing_fence+1 "
            "WHERE action_id=?", (ctx.action_id,),
        )
        conn.commit()
        conn.close()
        with self.assertRaises(conversation_store.ActionLeaseLost):
            conversation_tools.execute("log_strength_workout", {
                "entries": [{
                    "exercise_name": "жим", "weight_kg": 80,
                    "sets": 4, "reps": 8,
                }],
                "resolved_date": "2026-07-13",
            }, ctx)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM workout_exercises").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM action_domain_links").fetchone()[0], 0)
        conn.close()

    def test_daily_context_commit_contains_entry_and_factor_outbox_job(self):
        workouts_db.connect().close()
        ctx = self._action_ctx()
        result = conversation_tools.execute("save_daily_context", {
            "notes": "поздно лёг", "resolved_date": "2026-07-13",
        }, ctx)
        conn = sqlite3.connect(self.db)
        entry = conn.execute(
            "SELECT entry_id, origin_action_id FROM daily_context_entries"
        ).fetchone()
        job = conn.execute(
            "SELECT origin_action_id, status, projection_revision FROM factor_extraction_jobs"
        ).fetchone()
        conn.close()
        self.assertEqual(result["record_ids"], [entry[0]])
        self.assertEqual(entry[1], ctx.action_id)
        self.assertEqual(job[0], ctx.action_id)
        self.assertIn(job[1], {"pending", "disabled"})
        self.assertEqual(job[2], 1)
        self.assertTrue(data_integrity.audit_database(self.db)["ok"])

    def test_missing_action_ledger_is_never_accepted(self):
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TABLE conversation_actions")
        conn.commit()
        conn.close()
        report = data_integrity.audit_database(self.db)
        self.assertFalse(report["ok"])
        self.assertIn("missing_table", {item["code"] for item in report["issues"]})

    def test_answered_morning_context_requires_entry_projection_and_outbox(self):
        morning_context.ensure_request("2026-07-13")
        conn = sqlite3.connect(self.db)
        conn.execute(
            """UPDATE morning_context
               SET status='answered', source_message_id='900',
                   updated_at='2099-01-01T00:00:00+00:00'"""
        )
        conn.commit()
        conn.close()
        report = data_integrity.audit_database(self.db)
        self.assertIn(
            "morning_context_entry_missing",
            {item["code"] for item in report["issues"]},
        )

    def test_answered_morning_context_detects_projection_hash_tampering(self):
        morning_context.ensure_request("2026-07-13")
        self.assertIsNotNone(morning_context.claim_question("2026-07-13"))
        self.assertTrue(morning_context.mark_question_sent("2026-07-13", 700))
        result = morning_context.accept_and_record_reply(
            900, 700, "late meal", factor_capture_enabled=True,
            source_key="telegram:1:900:morning-context",
        )
        self.assertIsNotNone(result)
        self.assertTrue(data_integrity.audit_database(self.db)["ok"])

        conn = sqlite3.connect(self.db)
        conn.execute(
            """UPDATE daily_context_projection_state
               SET projection_hash='tampered' WHERE context_date='2026-07-12'"""
        )
        conn.commit()
        conn.close()

        report = data_integrity.audit_database(self.db)
        self.assertFalse(report["ok"])
        self.assertIn(
            "morning_context_projection_hash_mismatch",
            {item["code"] for item in report["issues"]},
        )

    def test_phase1_daily_action_without_record_ids_uses_legacy_projection(self):
        reservation = conversation_store.reserve(conversation_store.ActionContext(
            source="telegram", chat_id="1", message_id="legacy-context",
            input_text="legacy",
        ))
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO daily_log(date, notes, updated_at) VALUES (?,?,?)",
            ("2020-01-01", "legacy note", "2020-01-02T00:00:00+00:00"),
        )
        conn.execute(
            """UPDATE conversation_actions
               SET status='succeeded', tool_name='save_daily_context',
                   result_json=?, completed_at='2020-01-02T00:00:00+00:00'
               WHERE action_id=?""",
            (__import__("json").dumps({
                "created_count": 1, "record_ids": [],
                "resolved_date": "2020-01-01", "data": {},
            }), reservation.action_id),
        )
        conn.commit()
        conn.close()
        self.assertTrue(data_integrity.audit_database(self.db)["ok"])

    def test_daily_context_provenance_hash_and_outbox_are_audited(self):
        workouts_db.connect().close()
        ctx = self._action_ctx("context-audit")
        result = conversation_tools.execute("save_daily_context", {
            "notes": "late meal", "resolved_date": "2026-07-13",
        }, ctx)
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TRIGGER trg_daily_context_entries_content_immutable")
        conn.execute(
            "UPDATE daily_context_entries SET content_sha256='tampered' WHERE entry_id=?",
            (result["record_ids"][0],),
        )
        conn.execute("DELETE FROM factor_extraction_jobs")
        conn.commit()
        conn.close()
        codes = {item["code"] for item in data_integrity.audit_database(self.db)["issues"]}
        self.assertIn("daily_context_content_hash_mismatch", codes)
        self.assertIn("daily_context_outbox_revision_missing", codes)

    def test_historical_daily_context_record_id_is_checked(self):
        workouts_db.connect().close()
        ctx = self._action_ctx("context-history")
        conversation_tools.execute("save_daily_context", {
            "notes": "historical", "resolved_date": "2026-07-13",
        }, ctx)
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TRIGGER trg_conversation_actions_success_immutable")
        conn.execute(
            """UPDATE conversation_actions
               SET status='succeeded', tool_name='save_daily_context',
                   result_json=?, completed_at='2000-01-01T00:00:00+00:00'
               WHERE action_id=?""",
            ('{"created_count":1,"record_ids":["missing-entry"],"data":{}}',
             ctx.action_id),
        )
        conn.commit()
        conn.close()
        codes = {item["code"] for item in data_integrity.audit_database(self.db)["issues"]}
        self.assertIn("succeeded_action_missing_domain_row", codes)


if __name__ == "__main__":
    unittest.main()
