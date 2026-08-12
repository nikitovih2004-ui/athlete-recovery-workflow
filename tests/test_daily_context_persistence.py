import datetime as dt
import sqlite3
from unittest.mock import patch

import conversation_store
import daily_log
import factor_capture
import morning_context
import phase2_store
from tests.conversation_fakes import TempDBCase


UTC = dt.timezone.utc


class DailyContextEntryTests(TempDBCase):
    def test_source_idempotency_projection_replace_and_retract(self):
        conn = daily_log.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            first, inserted, projection = daily_log.append_entry_tx(
                conn, "2026-07-12", "без алкоголя",
                source_key="telegram:1:10:context",
            )
            same, inserted_again, _ = daily_log.append_entry_tx(
                conn, "2026-07-12", "без алкоголя",
                source_key="telegram:1:10:context",
            )
            conn.commit()
            self.assertTrue(inserted)
            self.assertFalse(inserted_again)
            self.assertEqual(first, same)
            self.assertEqual(projection["notes"], "без алкоголя")

            conn.execute("BEGIN IMMEDIATE")
            second, replaced, projection = daily_log.replace_entry_tx(
                conn, first, "был бокал вина",
                source_key="telegram:1:11:correction",
            )
            conn.commit()
            self.assertTrue(replaced)
            self.assertEqual(projection["notes"], "был бокал вина")

            conn.execute("BEGIN IMMEDIATE")
            changed, projection = daily_log.retract_entry_tx(conn, second)
            conn.commit()
            self.assertTrue(changed)
            self.assertIsNone(projection["notes"])
        finally:
            conn.close()

    def test_conflicting_source_key_is_rejected(self):
        conn = daily_log.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            daily_log.append_entry_tx(
                conn, "2026-07-12", "one", source_key="same"
            )
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(daily_log.ContextConflict):
                daily_log.append_entry_tx(
                    conn, "2026-07-12", "different", source_key="same"
                )
            conn.rollback()
        finally:
            conn.close()

    def test_context_content_cannot_be_rewritten_or_hard_deleted(self):
        conn = daily_log.connect()
        try:
            entry_id, _, _ = daily_log.append_entry_tx(
                conn, "2026-07-12", "calm", source_key="telegram:1:12:context",
            )
            conn.commit()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                conn.execute(
                    "UPDATE daily_context_entries SET notes='tampered' WHERE entry_id=?",
                    (entry_id,),
                )
            conn.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "delete blocked"):
                conn.execute(
                    "DELETE FROM daily_context_entries WHERE entry_id=?", (entry_id,),
                )
            conn.rollback()
            changed, _ = daily_log.retract_entry_tx(conn, entry_id)
            conn.commit()
            self.assertTrue(changed)
        finally:
            conn.close()


class AtomicMorningReplyTests(TempDBCase):
    def _question(self):
        morning_context.ensure_request("2026-07-13")
        self.assertIsNotNone(morning_context.claim_question("2026-07-13"))
        self.assertTrue(morning_context.mark_question_sent("2026-07-13", 700))

    def test_entry_projection_state_and_factor_job_commit_together(self):
        self._question()
        result = morning_context.accept_and_record_reply(
            701, 700, "поздний ужин",
            source_key="telegram:1:701:morning-context",
            factor_capture_enabled=False,
        )
        self.assertTrue(result["inserted"])
        conn = daily_log.connect()
        try:
            self.assertEqual(
                conn.execute("SELECT status FROM morning_context").fetchone()[0],
                morning_context.STATUS_ANSWERED,
            )
            self.assertEqual(
                conn.execute("SELECT notes FROM daily_log WHERE date='2026-07-12'").fetchone()[0],
                "поздний ужин",
            )
            job = conn.execute(
                "SELECT status, projection_hash FROM factor_extraction_jobs"
            ).fetchone()
            self.assertEqual(job[0], "disabled")
            self.assertEqual(job[1], result["projection_hash"])
        finally:
            conn.close()

    def test_outbox_failure_rolls_back_entry_and_morning_state(self):
        self._question()
        with patch.object(phase2_store, "enqueue_factor_job", side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(sqlite3.OperationalError):
                morning_context.accept_and_record_reply(
                    702, 700, "private note",
                    source_key="telegram:1:702:morning-context",
                )
        conn = daily_log.connect()
        try:
            self.assertEqual(
                conn.execute("SELECT status FROM morning_context").fetchone()[0],
                morning_context.STATUS_PENDING,
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_context_entries").fetchone()[0], 0)
        finally:
            conn.close()


class FactorOutboxTests(TempDBCase):
    def test_old_projection_completion_is_fenced_and_latest_wins(self):
        conn = daily_log.connect()
        try:
            phase2_store.migrate(conn)
            conn.execute("BEGIN IMMEDIATE")
            _, _, p1 = daily_log.append_entry_tx(
                conn, "2026-07-12", "без алкоголя", source_key="m1"
            )
            j1, _ = phase2_store.enqueue_factor_job(
                conn, context_date="2026-07-12", projection_hash=p1["projection_hash"],
                projection_revision=p1["revision"], extractor_version="v1",
                origin_action_id=None,
            )
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            claim1 = phase2_store.claim_factor_job(conn, job_id=j1)
            conn.commit()

            conn.execute("BEGIN IMMEDIATE")
            _, _, p2 = daily_log.append_entry_tx(
                conn, "2026-07-12", "поздний ужин", source_key="m2"
            )
            j2, _ = phase2_store.enqueue_factor_job(
                conn, context_date="2026-07-12", projection_hash=p2["projection_hash"],
                projection_revision=p2["revision"], extractor_version="v1",
                origin_action_id=None,
            )
            conn.commit()

            conn.execute("BEGIN IMMEDIATE")
            self.assertFalse(phase2_store.complete_factor_job(
                conn, j1, claim1["lease_token"], [], allowed_factor_keys={"late_meal"}
            ))
            conn.commit()
            self.assertEqual(
                conn.execute("SELECT status FROM factor_extraction_jobs WHERE job_id=?", (j1,)).fetchone()[0],
                "superseded",
            )

            conn.execute("BEGIN IMMEDIATE")
            claim2 = phase2_store.claim_factor_job(conn, job_id=j2)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            self.assertTrue(phase2_store.complete_factor_job(
                conn, j2, claim2["lease_token"],
                [{"factor_key": "late_meal", "state": 1, "confidence": 0.95}],
                allowed_factor_keys={"late_meal"},
            ))
            conn.commit()
            row = conn.execute(
                "SELECT job_id, is_current FROM daily_factor_observations"
            ).fetchone()
            self.assertEqual(tuple(row), (j2, 1))
        finally:
            conn.close()


    def test_retract_last_entry_publishes_empty_projection_once(self):
        conn = daily_log.connect()
        try:
            phase2_store.migrate(conn)
            conn.execute("BEGIN IMMEDIATE")
            entry_id, _, projection = daily_log.append_entry_tx(
                conn, "2026-07-12", "late meal", source_key="context:initial",
            )
            first_job, _ = phase2_store.enqueue_factor_job(
                conn, context_date="2026-07-12",
                projection_hash=projection["projection_hash"],
                projection_revision=projection["revision"], extractor_version="v1",
                origin_action_id=None,
            )
            first_claim = phase2_store.claim_factor_job(conn, job_id=first_job)
            self.assertTrue(phase2_store.complete_factor_job(
                conn, first_job, first_claim["lease_token"],
                [{"factor_key": "late_meal", "state": 1, "confidence": 0.95}],
                allowed_factor_keys={"late_meal"},
            ))
            conn.commit()

            conn.execute("BEGIN IMMEDIATE")
            changed, empty_projection = daily_log.retract_entry_tx(conn, entry_id)
            self.assertTrue(changed)
            self.assertIsNone(empty_projection["notes"])
            empty_job, _ = phase2_store.enqueue_factor_job(
                conn, context_date="2026-07-12",
                projection_hash=empty_projection["projection_hash"],
                projection_revision=empty_projection["revision"], extractor_version="v1",
                origin_action_id=None,
            )
            empty_claim = phase2_store.claim_factor_job(conn, job_id=empty_job)
            result = factor_capture.capture_factors_result(
                empty_projection["notes"] or "", enabled=True,
                transport=object(),
            )
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["observations"], [])
            self.assertTrue(phase2_store.complete_factor_job(
                conn, empty_job, empty_claim["lease_token"], [],
                allowed_factor_keys={"late_meal"},
            ))
            conn.commit()

            self.assertEqual(
                conn.execute(
                    "SELECT status FROM factor_extraction_jobs WHERE job_id=?",
                    (empty_job,),
                ).fetchone()[0],
                "succeeded",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM daily_factor_observations WHERE is_current=1"
                ).fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_failed_job_stops_claiming_at_retry_budget(self):
        conn = daily_log.connect()
        try:
            phase2_store.migrate(conn)
            start = dt.datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
            conn.execute("BEGIN IMMEDIATE")
            _, _, projection = daily_log.append_entry_tx(
                conn, "2026-07-12", "late meal", source_key="context:retry",
            )
            job_id, _ = phase2_store.enqueue_factor_job(
                conn, context_date="2026-07-12",
                projection_hash=projection["projection_hash"],
                projection_revision=projection["revision"], extractor_version="v1",
                origin_action_id=None, now=start,
            )
            conn.commit()

            for attempt in range(phase2_store.FACTOR_JOB_MAX_ATTEMPTS):
                now = start + dt.timedelta(minutes=attempt)
                conn.execute("BEGIN IMMEDIATE")
                claimed = phase2_store.claim_factor_job(
                    conn, job_id=job_id, now=now,
                )
                self.assertIsNotNone(claimed)
                self.assertEqual(claimed["attempt_count"], attempt + 1)
                self.assertTrue(phase2_store.fail_factor_job(
                    conn, job_id, claimed["lease_token"], "response_invalid",
                    retry_at=now, now=now,
                ))
                conn.commit()

            conn.execute("BEGIN IMMEDIATE")
            self.assertIsNone(phase2_store.claim_factor_job(
                conn, job_id=job_id,
                now=start + dt.timedelta(hours=1),
            ))
            row = conn.execute(
                """SELECT status, attempt_count, completed_at, last_error_code
                   FROM factor_extraction_jobs WHERE job_id=?""",
                (job_id,),
            ).fetchone()
            conn.commit()
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["attempt_count"], phase2_store.FACTOR_JOB_MAX_ATTEMPTS)
            self.assertIsNotNone(row["completed_at"])
            self.assertEqual(row["last_error_code"], "response_invalid")
        finally:
            conn.close()

    def test_expired_final_lease_is_dead_lettered_not_reclaimed(self):
        conn = daily_log.connect()
        try:
            phase2_store.migrate(conn)
            start = dt.datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
            conn.execute("BEGIN IMMEDIATE")
            _, _, projection = daily_log.append_entry_tx(
                conn, "2026-07-12", "late meal", source_key="context:crash",
            )
            job_id, _ = phase2_store.enqueue_factor_job(
                conn, context_date="2026-07-12",
                projection_hash=projection["projection_hash"],
                projection_revision=projection["revision"], extractor_version="v1",
                origin_action_id=None, now=start,
            )
            conn.execute(
                "UPDATE factor_extraction_jobs SET attempt_count=? WHERE job_id=?",
                (phase2_store.FACTOR_JOB_MAX_ATTEMPTS - 1, job_id),
            )
            final_claim = phase2_store.claim_factor_job(
                conn, job_id=job_id, now=start, lease=dt.timedelta(seconds=1),
            )
            self.assertEqual(
                final_claim["attempt_count"], phase2_store.FACTOR_JOB_MAX_ATTEMPTS,
            )
            conn.commit()

            conn.execute("BEGIN IMMEDIATE")
            self.assertIsNone(phase2_store.claim_factor_job(
                conn, job_id=job_id, now=start + dt.timedelta(seconds=2),
            ))
            row = conn.execute(
                """SELECT status, completed_at, lease_token, last_error_code
                   FROM factor_extraction_jobs WHERE job_id=?""",
                (job_id,),
            ).fetchone()
            conn.commit()
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["completed_at"])
            self.assertIsNone(row["lease_token"])
            self.assertEqual(row["last_error_code"], "max_attempts_exceeded")
        finally:
            conn.close()


class ActionLeaseTests(TempDBCase):
    def test_live_duplicate_waits_and_stale_duplicate_reclaims(self):
        ctx = conversation_store.ActionContext(
            source="telegram", chat_id="1", message_id="9", input_text="same"
        )
        start = dt.datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
        first = conversation_store.reserve(ctx, now=start, lease=dt.timedelta(seconds=30))
        live = conversation_store.reserve(ctx, now=start + dt.timedelta(seconds=5))
        stale = conversation_store.reserve(ctx, now=start + dt.timedelta(seconds=31))
        self.assertFalse(live.is_new)
        self.assertTrue(live.in_progress)
        self.assertTrue(stale.is_new)
        self.assertTrue(stale.reclaimed)
        self.assertEqual(stale.processing_fence, first.processing_fence + 1)

    def test_stale_fence_cannot_finalize_success(self):
        ctx = conversation_store.ActionContext(
            source="telegram", chat_id="1", message_id="10", input_text="same"
        )
        start = dt.datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
        old = conversation_store.reserve(ctx, now=start, lease=dt.timedelta(seconds=1))
        conversation_store.reserve(ctx, now=start + dt.timedelta(seconds=2))
        conn = conversation_store.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(conversation_store.ActionLeaseLost):
                conversation_store.finalize_success_tx(
                    conn, old.action_id, tool_name="save_daily_context",
                    validated_arguments={}, result={},
                    processing_token=old.processing_token,
                    processing_fence=old.processing_fence,
                )
            conn.rollback()
        finally:
            conn.close()


if __name__ == "__main__":
    import unittest
    unittest.main()
