import datetime as dt
import os
import sqlite3
import tempfile
import threading
import unittest

import phase2_store as store


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


def create_legacy_pending(conn):
    conn.execute(
        """
        CREATE TABLE pending_actions (
            pending_id TEXT PRIMARY KEY,
            origin_action_id TEXT NOT NULL,
            source TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            user_id TEXT,
            candidate_intent TEXT NOT NULL,
            partial_arguments_json TEXT NOT NULL,
            missing_fields_json TEXT NOT NULL,
            clarification_question_message_id TEXT,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by_action_id TEXT
        )
        """
    )


class Phase2SchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        create_legacy_pending(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_migration_is_idempotent_and_exactly_additive(self):
        store.migrate(self.conn)
        store.migrate(self.conn)

        tables = {
            row[0] for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertEqual(
            tables,
            {"pending_actions", "conversation_sessions", "daily_factor_observations",
             "factor_extraction_jobs"},
        )
        session_columns = [
            row[1] for row in self.conn.execute("PRAGMA table_info(conversation_sessions)")
        ]
        self.assertEqual(
            session_columns,
            ["source", "chat_id", "user_id", "state_json", "version", "expires_at",
             "created_at", "updated_at"],
        )
        factor_columns = [
            row[1] for row in self.conn.execute("PRAGMA table_info(daily_factor_observations)")
        ]
        self.assertEqual(
            factor_columns,
            ["observation_id", "context_date", "factor_key", "state",
             "extractor_version", "confidence", "source_key", "created_at", "updated_at",
             "job_id", "projection_hash", "projection_revision", "is_current"],
        )
        pending_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(pending_actions)")
        }
        self.assertTrue(
            {"claimed_by_action_id", "claimed_at", "claim_expires_at"}
            <= pending_columns
        )
        indexes = {
            row[0] for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_autoindex%'"
            )
        }
        self.assertEqual(
            indexes,
            {"idx_conversation_sessions_expiry", "idx_daily_factor_date_key",
             "idx_pending_actions_claim_expiry", "idx_daily_factor_current",
             "idx_factor_jobs_ready", "idx_factor_jobs_date_revision"},
        )


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        store.migrate(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_create_read_cas_and_principal_isolation(self):
        first = store.save_session(
            self.conn, "telegram", "chat", "user-a",
            {"active_topic": "recovery", "turn_count": 1},
            expected_version=None, now=NOW,
        )
        self.assertEqual(first.version, 1)
        self.assertEqual(
            store.get_session(self.conn, "telegram", "chat", "user-a", now=NOW).state,
            {"active_topic": "recovery", "turn_count": 1},
        )
        self.assertIsNone(store.get_session(self.conn, "telegram", "chat", "user-b", now=NOW))

        second = store.save_session(
            self.conn, "telegram", "chat", "user-a",
            {"active_topic": "hrv", "turn_count": 2},
            expected_version=1, now=NOW + dt.timedelta(minutes=5),
        )
        self.assertEqual(second.version, 2)
        with self.assertRaises(store.SessionConflict):
            store.save_session(
                self.conn, "telegram", "chat", "user-a",
                {"active_topic": "sleep", "turn_count": 3},
                expected_version=1, now=NOW,
            )

    def test_ttl_is_at_most_24_hours_and_expired_read_is_side_effect_free(self):
        record = store.save_session(
            self.conn, "telegram", "chat", None, {"turn_count": 0},
            expected_version=None, now=NOW,
        )
        self.assertEqual(record.expires_at, (NOW + dt.timedelta(hours=24)).isoformat(timespec="seconds"))
        self.assertIsNone(
            store.get_session(
                self.conn, "telegram", "chat", None,
                now=NOW + dt.timedelta(hours=24),
            )
        )
        rows = self.conn.execute("SELECT COUNT(*) FROM conversation_sessions").fetchone()[0]
        self.assertEqual(rows, 1)
        with self.assertRaises(ValueError):
            store.save_session(
                self.conn, "telegram", "another", None, {"turn_count": 0},
                expected_version=None, now=NOW, ttl=dt.timedelta(hours=25),
            )

    def test_privacy_fields_unknown_fields_and_size_cap_are_rejected(self):
        for state in (
            {"transcript": ["secret"]},
            {"last_query": {"notes": "private"}},
            {"unknown": "value"},
        ):
            with self.subTest(state=state), self.assertRaises(store.InvalidSessionState):
                store.encode_session_state(state)
        with self.assertRaises(store.InvalidSessionState):
            store.encode_session_state({"last_query": {"value": "ю" * 5000}})

    def test_cleanup_is_bounded(self):
        for index in range(3):
            store.save_session(
                self.conn, "telegram", f"chat-{index}", None, {"turn_count": 0},
                expected_version=None, now=NOW - dt.timedelta(days=2),
            )
        self.assertEqual(store.cleanup_expired_sessions(self.conn, now=NOW, limit=2), 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM conversation_sessions").fetchone()[0], 1
        )
        with self.assertRaises(ValueError):
            store.cleanup_expired_sessions(self.conn, now=NOW, limit=0)


class FactorTests(unittest.TestCase):
    ALLOWED = {"alcohol", "late_caffeine"}

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        store.migrate(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_factor_retry_is_idempotent_and_conflict_is_rejected(self):
        kwargs = dict(
            context_date="2026-07-12", factor_key="alcohol", state=1,
            extractor_version="factors_v1", confidence=0.95,
            source_key="daily-context:42:alcohol", allowed_factor_keys=self.ALLOWED,
            now=NOW,
        )
        observation_id, created = store.put_factor_observation(self.conn, **kwargs)
        same_id, created_again = store.put_factor_observation(self.conn, **kwargs)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(observation_id, same_id)
        with self.assertRaises(store.FactorConflict):
            store.put_factor_observation(self.conn, **{**kwargs, "state": 0})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM daily_factor_observations").fetchone()[0], 1
        )

    def test_factor_allowlist_state_and_confidence_are_enforced(self):
        base = dict(
            context_date="2026-07-12", factor_key="alcohol", state=1,
            extractor_version="factors_v1", confidence=None, source_key="s1",
            allowed_factor_keys=self.ALLOWED, now=NOW,
        )
        with self.assertRaises(ValueError):
            store.put_factor_observation(self.conn, **{**base, "factor_key": "custom"})
        with self.assertRaises(ValueError):
            store.put_factor_observation(self.conn, **{**base, "state": True})
        with self.assertRaises(ValueError):
            store.put_factor_observation(self.conn, **{**base, "confidence": 1.1})

class PendingClaimTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        create_legacy_pending(conn)
        store.migrate(conn)
        self._insert_pending(conn, "p1")
        conn.commit()
        conn.close()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except FileNotFoundError:
                pass

    def _insert_pending(self, conn, pending_id, *, status="open", expires=None):
        expires = expires or NOW + dt.timedelta(hours=1)
        now_iso = NOW.isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO pending_actions
               (pending_id, origin_action_id, source, chat_id, user_id,
                candidate_intent, partial_arguments_json, missing_fields_json,
                status, attempt_count, expires_at, created_at, updated_at)
               VALUES (?, 'origin', 'telegram', 'chat', 'user', 'log_cardio',
                       '{}', '["fact_status"]', ?, 0, ?, ?, ?)""",
            (pending_id, status, expires.isoformat(timespec="seconds"), now_iso, now_iso),
        )

    def test_concurrent_claim_has_one_winner(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def contender(action_id):
            conn = sqlite3.connect(self.path, timeout=10)
            conn.execute("PRAGMA busy_timeout=10000")
            try:
                barrier.wait()
                conn.execute("BEGIN IMMEDIATE")
                won = store.claim_pending_for_resolution(
                    conn, "p1", action_id, now=NOW
                )
                conn.commit()
                results.append((action_id, won))
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=contender, args=(f"a{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(sorted(won for _action, won in results), [False, True])
        winner = next(action for action, won in results if won)
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT status, claimed_by_action_id FROM pending_actions WHERE pending_id='p1'"
        ).fetchone()
        self.assertEqual(row, ("resolving", winner))
        self.assertTrue(store.finalize_pending_resolution(conn, "p1", winner, now=NOW))
        self.assertFalse(store.finalize_pending_resolution(conn, "p1", "loser", now=NOW))
        conn.close()

    def test_release_and_stale_recovery(self):
        conn = sqlite3.connect(self.path)
        self.assertTrue(store.claim_pending_for_resolution(conn, "p1", "a1", now=NOW))
        self.assertFalse(store.release_pending_claim(conn, "p1", "other", now=NOW))
        self.assertTrue(store.release_pending_claim(conn, "p1", "a1", now=NOW))
        self.assertTrue(store.claim_pending_for_resolution(
            conn, "p1", "a2", now=NOW, lease=dt.timedelta(seconds=1)
        ))
        self.assertEqual(
            store.recover_stale_pending_claims(
                conn, now=NOW + dt.timedelta(seconds=2), limit=10
            ), 1
        )
        status = conn.execute(
            "SELECT status FROM pending_actions WHERE pending_id='p1'"
        ).fetchone()[0]
        self.assertEqual(status, "open")
        conn.close()


if __name__ == "__main__":
    unittest.main()
