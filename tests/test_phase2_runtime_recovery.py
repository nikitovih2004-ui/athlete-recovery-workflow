"""Crash-recovery contracts for Phase 2 conversation runtime.

These tests intentionally exercise persisted state only.  Recovery must never
re-run Gemini, a conversation tool, or a domain mutation after a process crash.
"""
from __future__ import annotations

import datetime as dt
import contextlib
import io
import json
import os
import sqlite3
import sys
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import conversation_contract as C
import conversation_router as router
import conversation_store as store
import conversation_tools as tools
import phase2_store
import telegram_bot
from conversation_fakes import TempDBCase, envelope, single_response


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
KYIV_NOW = NOW.astimezone(ZoneInfo("Europe/Kyiv"))


class _NoGemini:
    def generate(self, *_args, **_kwargs):
        raise AssertionError("recovery/duplicate replay must not call Gemini")


class StalePendingRecoveryTests(TempDBCase):
    def _insert_action(self, conn, action_id, status):
        now = NOW.isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO conversation_actions
               (action_id, source, chat_id, user_id, message_id, received_at,
                input_sha256, input_length, status, attempt_count,
                created_at, updated_at, completed_at)
               VALUES (?, 'telegram', 'chat', 'user', ?, ?, 'hash', 0, ?, 0,
                       ?, ?, ?)""",
            (action_id, f"message-{action_id}", now, status, now, now, now),
        )

    def _insert_pending(self, conn, pending_id, action_id, *, expires_at):
        stale = (NOW - dt.timedelta(seconds=1)).isoformat(timespec="seconds")
        created = (NOW - dt.timedelta(minutes=10)).isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO pending_actions
               (pending_id, origin_action_id, source, chat_id, user_id,
                candidate_intent, partial_arguments_json, missing_fields_json,
                status, attempt_count, expires_at, created_at, updated_at,
                resolved_by_action_id, claimed_by_action_id, claimed_at,
                claim_expires_at)
               VALUES (?, 'origin', 'telegram', 'chat', 'user', 'log_cardio',
                       '{}', '["fact_status"]', 'resolving', 0, ?, ?, ?,
                       ?, ?, ?, ?)""",
            (pending_id, expires_at, created, created,
             action_id, action_id, created, stale),
        )

    def test_recovery_uses_claimed_action_terminal_state_and_fails_closed(self):
        conn = store.connect()
        try:
            conn.execute("CREATE TABLE domain_sentinel(value TEXT NOT NULL)")
            conn.execute("INSERT INTO domain_sentinel VALUES ('unchanged')")
            cases = {
                "succeeded": C.PENDING_RESOLVED,
                "failed": C.PENDING_OPEN,
                "rejected": C.PENDING_OPEN,
                "noop": C.PENDING_OPEN,
                "received": C.PENDING_EXPIRED,
                "needs_clarification": C.PENDING_EXPIRED,
            }
            future = (NOW + dt.timedelta(hours=1)).isoformat(timespec="seconds")
            for status in cases:
                action_id = f"action-{status}"
                self._insert_action(conn, action_id, status)
                self._insert_pending(conn, f"pending-{status}", action_id,
                                     expires_at=future)

            # A missing claimed action is indeterminate and therefore expires;
            # it must never be blindly reopened or replayed.
            self._insert_pending(conn, "pending-missing", "action-missing",
                                 expires_at=future)

            # Even a safely failed action cannot reopen an already expired
            # clarification.
            self._insert_action(conn, "action-expired-failed", C.ACTION_FAILED)
            self._insert_pending(
                conn, "pending-expired-failed", "action-expired-failed",
                expires_at=(NOW - dt.timedelta(minutes=1)).isoformat(timespec="seconds"),
            )
            conn.commit()
        finally:
            conn.close()

        recovered = store.recover_stale_pending_claims(now=NOW, limit=100)
        self.assertEqual(recovered, len(cases) + 2)

        conn = sqlite3.connect(self.db)
        try:
            actual = dict(conn.execute(
                "SELECT pending_id, status FROM pending_actions"
            ).fetchall())
            for action_status, expected_pending_status in cases.items():
                self.assertEqual(
                    actual[f"pending-{action_status}"], expected_pending_status,
                    action_status,
                )
            self.assertEqual(actual["pending-missing"], C.PENDING_EXPIRED)
            self.assertEqual(actual["pending-expired-failed"], C.PENDING_EXPIRED)
            self.assertEqual(
                conn.execute("SELECT value FROM domain_sentinel").fetchone()[0],
                "unchanged",
            )
            uncleared = conn.execute(
                """SELECT COUNT(*) FROM pending_actions
                   WHERE claimed_by_action_id IS NOT NULL
                      OR claimed_at IS NOT NULL OR claim_expires_at IS NOT NULL"""
            ).fetchone()[0]
            self.assertEqual(uncleared, 0)
        finally:
            conn.close()


class FailedResponseDeliveryTests(TempDBCase):
    def _finalize_without_response(self, message_id, intent, arguments, result, *, read):
        reservation = store.reserve(store.ActionContext(
            source="telegram", chat_id="chat", user_id="user",
            message_id=message_id, input_text="private input",
        ))
        store.record_router(
            reservation.action_id, model="fake", response_sha256="a" * 64,
            intent=intent, confidence=0.99,
        )
        if read:
            store.finalize_read(
                reservation.action_id, tool_name=C.tool_for_intent(intent),
                validated_arguments=arguments, result=result,
            )
        else:
            conn = store.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                store.finalize_success_tx(
                    conn, reservation.action_id,
                    tool_name=C.tool_for_intent(intent),
                    validated_arguments=arguments, result=result,
                )
                conn.commit()
            finally:
                conn.close()
        return reservation.action_id

    def test_crash_before_response_is_reconstructed_for_read_and_mutation(self):
        read_id = self._finalize_without_response(
            "crash-read", C.INTENT_GET_TODAY_STATUS, {},
            {"data": {"date": "2026-07-13", "recovery": None,
                      "baseline_28d": {"recovery_score": 70, "sample_size": 20}}},
            read=True,
        )
        mutation_id = self._finalize_without_response(
            "crash-mutation", C.INTENT_LOG_SUPPLEMENT,
            {"resolved_date": "2026-07-13", "items": [{"name": "magnesium"}]},
            {"resolved_date": "2026-07-13", "created_count": 1},
            read=False,
        )

        class Sent:
            def __init__(self, message_id):
                self.message_id = message_id

        sent = []
        def send(_chat_id, text):
            sent.append(text)
            return Sent(len(sent))

        with mock.patch.object(telegram_bot, "TG_CHAT", "chat"), \
             mock.patch.object(telegram_bot.bot, "send_message", side_effect=send):
            self.assertEqual(telegram_bot.retry_pending_conversation_responses(), 2)

        self.assertEqual(len(sent), 2)
        for action_id in (read_id, mutation_id):
            row = store.get_action(action_id)
            self.assertTrue(row["response_text"])
            self.assertEqual(row["reply_delivery_status"], "delivered")

    def test_failed_persisted_response_is_claimed_once_and_marked_delivered(self):
        reservation = store.reserve(store.ActionContext(
            source="telegram", chat_id="chat", user_id="user",
            message_id="read-1", input_text="status",
        ))
        persisted_result = {
            "data": {"snapshot": "today_status.v1", "date": "2026-07-13"}
        }
        store.finalize_read(
            reservation.action_id, tool_name="get_today_status",
            validated_arguments={}, result=persisted_result,
        )
        response_text = "Детерминированный сохранённый ответ"
        store.record_response(reservation.action_id, "confirmation", response_text)
        store.mark_response_delivery(reservation.action_id, delivered=False)

        conn = sqlite3.connect(self.db)
        try:
            conn.execute("CREATE TABLE domain_sentinel(value TEXT NOT NULL)")
            conn.execute("INSERT INTO domain_sentinel VALUES ('unchanged')")
            conn.commit()
        finally:
            conn.close()

        retryable = store.claim_retryable_responses(
            source="telegram", chat_id="chat", limit=10, now=NOW,
        )
        self.assertEqual(len(retryable), 1)
        self.assertEqual(retryable[0]["action_id"], reservation.action_id)
        self.assertEqual(retryable[0]["response_text"], response_text)

        # claim_retryable_responses already acquired the lease atomically;
        # another worker cannot claim the same response.
        self.assertFalse(store.claim_response_delivery(reservation.action_id))

        store.mark_response_delivery(
            reservation.action_id, delivered=True, message_id="telegram-9001"
        )
        row = store.get_action(reservation.action_id)
        self.assertEqual(row["reply_delivery_status"], "delivered")
        self.assertEqual(row["reply_message_id"], "telegram-9001")
        self.assertEqual(json.loads(row["result_json"]), persisted_result)
        self.assertEqual(row["tool_name"], "get_today_status")

        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(
                conn.execute("SELECT value FROM domain_sentinel").fetchone()[0],
                "unchanged",
            )
        finally:
            conn.close()


class DuplicateReadRecoveryTests(TempDBCase):
    def _route(self, message_id, gemini):
        ctx = store.ActionContext(
            source="telegram", chat_id="chat", user_id="user",
            message_id=message_id, input_text="мой статус",
        )
        exec_ctx = tools.ExecContext(
            action_id="", source="telegram", chat_id="chat",
            message_id=message_id, local_now=KYIV_NOW,
        )
        return router.route(
            ctx, exec_ctx, local_now=KYIV_NOW, gemini=gemini,
        )

    def test_duplicate_read_reconstructs_read_answer_when_response_text_missing(self):
        first = self._route(
            "duplicate-read",
            single_response(envelope(C.INTENT_GET_TODAY_STATUS, {})),
        )
        self.assertEqual(first.kind, "confirmation")

        # Simulate the crash window after the read result was committed but
        # before send_conversation_outcome persisted response_text.
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                """UPDATE conversation_actions
                   SET response_kind = NULL, response_text = NULL,
                       reply_delivery_status = NULL
                   WHERE action_id = ?""",
                (first.action_id,),
            )
            conn.commit()
        finally:
            conn.close()

        duplicate = self._route("duplicate-read", _NoGemini())
        expected = router._read_message(C.INTENT_GET_TODAY_STATUS, first.result)
        self.assertEqual(duplicate.kind, "duplicate")
        self.assertEqual(duplicate.message, expected)
        self.assertTrue(duplicate.message)
        self.assertFalse(duplicate.message.startswith("Готово"))


class RetentionTests(TempDBCase):
    def test_model_controlled_error_detail_is_never_persisted_and_legacy_is_redacted(self):
        reservation = store.reserve(store.ActionContext(
            source="telegram", chat_id="chat", user_id="user",
            message_id="private-error", input_text="private",
        ))
        store.mark_rejected(
            reservation.action_id, C.ERR_UNKNOWN_INTENT,
            "PRIVATE_SENTINEL_FROM_USER",
        )
        self.assertIsNone(store.get_action(reservation.action_id)["error_detail"])

        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "UPDATE conversation_actions SET error_detail=?, completed_at=?",
                ("LEGACY_PRIVATE_SENTINEL", "2025-01-01T00:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(store.redact_old_action_payloads(before=NOW, limit=1), 1)
        self.assertIsNone(store.get_action(reservation.action_id)["error_detail"])

    def test_maintenance_failure_emits_only_stable_diagnostic(self):
        output = io.StringIO()
        with mock.patch.object(
            store, "recover_stale_pending_claims",
            side_effect=sqlite3.OperationalError("PRIVATE_DB_DETAIL"),
        ), contextlib.redirect_stdout(output):
            self.assertFalse(telegram_bot.run_phase2_maintenance(now=NOW))
        diagnostic = output.getvalue()
        self.assertIn("phase2_maintenance_failed error=OperationalError", diagnostic)
        self.assertNotIn("PRIVATE_DB_DETAIL", diagnostic)

    def test_reconstructed_response_persistence_is_compare_and_set(self):
        reservation = store.reserve(store.ActionContext(
            source="telegram", chat_id="chat", user_id="user",
            message_id="cas-response", input_text="status",
        ))
        store.record_router(
            reservation.action_id, model="fake", response_sha256="a" * 64,
            intent=C.INTENT_GET_TODAY_STATUS, confidence=0.99,
        )
        store.finalize_read(
            reservation.action_id,
            tool_name=C.tool_for_intent(C.INTENT_GET_TODAY_STATUS),
            validated_arguments={}, result={"data": {"date": "2026-07-13"}},
        )
        self.assertEqual(len(store.missing_success_responses("telegram", "chat")), 1)
        self.assertEqual(len(store.missing_success_responses("telegram", "chat")), 1)

        self.assertTrue(store.record_response(reservation.action_id, "confirmation", "saved"))
        self.assertTrue(store.claim_response_delivery(reservation.action_id))
        store.mark_response_delivery(reservation.action_id, delivered=True, message_id="1")
        self.assertFalse(store.record_response(reservation.action_id, "confirmation", "saved"))
        self.assertFalse(store.claim_response_delivery(reservation.action_id))
        self.assertEqual(
            store.get_action(reservation.action_id)["reply_delivery_status"], "delivered"
        )

    def test_phase1_historical_success_is_not_recovered_after_column_migration(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript("""
                CREATE TABLE conversation_actions (
                    action_id TEXT PRIMARY KEY, source TEXT NOT NULL,
                    chat_id TEXT NOT NULL, user_id TEXT, message_id TEXT NOT NULL,
                    reply_to_message_id TEXT, received_at TEXT NOT NULL,
                    input_sha256 TEXT NOT NULL, input_length INTEGER NOT NULL,
                    router_model TEXT, router_schema_version TEXT,
                    prompt_version TEXT, router_response_sha256 TEXT,
                    intent TEXT, confidence REAL, tool_name TEXT,
                    validated_arguments_json TEXT, status TEXT NOT NULL,
                    error_code TEXT, error_detail TEXT, result_json TEXT,
                    latency_ms INTEGER, attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    completed_at TEXT, UNIQUE(source, chat_id, message_id)
                );
                CREATE TABLE pending_actions (
                    pending_id TEXT PRIMARY KEY, origin_action_id TEXT NOT NULL,
                    source TEXT NOT NULL, chat_id TEXT NOT NULL, user_id TEXT,
                    candidate_intent TEXT NOT NULL,
                    partial_arguments_json TEXT NOT NULL,
                    missing_fields_json TEXT NOT NULL,
                    clarification_question_message_id TEXT, status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, resolved_at TEXT,
                    resolved_by_action_id TEXT
                );
                INSERT INTO conversation_actions (
                    action_id, source, chat_id, message_id, received_at,
                    input_sha256, input_length, intent, status, result_json,
                    attempt_count, created_at, updated_at, completed_at
                ) VALUES (
                    'legacy', 'telegram', 'chat', 'old-message',
                    '2026-01-01T00:00:00+00:00', 'hash', 1,
                    'get_today_status', 'succeeded', '{"data":{"date":"2026-01-01"}}',
                    1, '2026-01-01T00:00:00+00:00',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
                );
            """)
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(store.missing_success_responses("telegram", "chat"), [])
        self.assertIsNone(store.get_action("legacy")["reply_delivery_status"])

    def test_idle_open_pending_expires_and_is_redacted_by_maintenance(self):
        migration_conn = store.connect()
        try:
            phase2_store.migrate(migration_conn)
        finally:
            migration_conn.close()
        ctx = store.ActionContext(
            source="telegram", chat_id="chat", user_id="user",
            message_id="idle-origin", input_text="supplement",
        )
        origin = store.reserve(ctx).action_id
        pending_id = store.create_pending(
            origin, ctx, C.INTENT_LOG_SUPPLEMENT,
            {"items": [{"name": "private supplement", "dosage": "secret"}]},
            ["taken"], ttl_minutes=1,
        )
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "UPDATE pending_actions SET expires_at=? WHERE pending_id=?",
                ((NOW - dt.timedelta(minutes=1)).isoformat(timespec="seconds"), pending_id),
            )
            conn.commit()
        finally:
            conn.close()

        self.assertTrue(telegram_bot.run_phase2_maintenance(now=NOW))
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT status, partial_arguments_json, missing_fields_json "
                "FROM pending_actions WHERE pending_id=?", (pending_id,),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (C.PENDING_EXPIRED, "{}", "[]"))

    def test_retry_bookkeeping_error_does_not_escape(self):
        row = {"action_id": "a", "chat_id": "chat", "response_text": "saved"}

        class Sent:
            message_id = 12

        with mock.patch.object(
            store, "claim_retryable_responses", return_value=[row]
        ), mock.patch.object(
            telegram_bot.bot, "send_message", return_value=Sent()
        ), mock.patch.object(
            store, "mark_response_delivery", side_effect=sqlite3.OperationalError("locked")
        ):
            self.assertEqual(telegram_bot.retry_pending_conversation_responses(), 0)

    def test_metric_series_is_not_duplicated_in_audit(self):
        reservation = store.reserve(store.ActionContext(
            source="telegram", chat_id="chat", user_id="user",
            message_id="trend-audit", input_text="trend",
        ))
        result = {
            "status": "success", "action_id": reservation.action_id,
            "data": {
                "snapshot": "metric_trend.v1", "metric": "hrv_rmssd",
                "series": [{"date": "2026-07-12", "value": 50}],
                "coverage": {"observed_days": 1, "expected_days": 7},
                "summary": {"mean": 50},
            },
        }
        store.finalize_read(
            reservation.action_id, tool_name="get_metric_trend",
            validated_arguments={"metric": "hrv_rmssd", "window_days": 7},
            result=result,
        )
        persisted = json.loads(store.get_action(reservation.action_id)["result_json"])
        self.assertNotIn("series", persisted["data"])
        self.assertEqual(persisted["data"]["summary"]["mean"], 50)

    def test_old_payload_redaction_is_bounded_and_idempotent(self):
        reservation = store.reserve(store.ActionContext(
            source="telegram", chat_id="chat", user_id="user",
            message_id="old-audit", input_text="status",
        ))
        store.finalize_read(
            reservation.action_id, tool_name="get_today_status",
            validated_arguments={}, result={"data": {"snapshot": "today_status.v1"}},
        )
        conn = sqlite3.connect(self.db)
        conn.execute(
            "UPDATE conversation_actions SET completed_at='2025-01-01T00:00:00+00:00'"
        )
        conn.commit()
        conn.close()
        self.assertEqual(store.redact_old_action_payloads(before=NOW, limit=1), 1)
        self.assertEqual(store.redact_old_action_payloads(before=NOW, limit=1), 0)
        row = store.get_action(reservation.action_id)
        self.assertIsNone(row["validated_arguments_json"])
        self.assertIsNone(row["result_json"])


if __name__ == "__main__":
    unittest.main()
