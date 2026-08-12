import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import requests

import daily_log
import fetch_data
import whoop_auth


class Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


class WhoopAuthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tokens = str(Path(self.temp.name) / "tokens.json")
        self.lock = self.tokens + ".lock"
        self.ambiguous = self.tokens + ".refresh-ambiguous"
        self.marker = self.tokens + ".transferred"
        self.marker_env = patch.dict(
            os.environ, {"WHOOP_TRANSFER_MARKER": self.marker}, clear=False,
        )
        self.marker_env.start()
        self.paths = patch.multiple(
            whoop_auth, TOKENS_FILE=self.tokens, LOCK_FILE=self.lock,
            AMBIGUOUS_FILE=self.ambiguous,
        )
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.marker_env.stop()
        self.temp.cleanup()

    def _expired(self, refresh="refresh-old"):
        Path(self.tokens).write_text(json.dumps({
            "access_token": "expired", "refresh_token": refresh,
            "obtained_at": 1, "expires_in": 1,
        }), encoding="utf-8")

    def test_concurrent_rotation_uses_refresh_token_once(self):
        self._expired()
        calls = 0
        guard = threading.Lock()

        def refresh(*_args, **_kwargs):
            nonlocal calls
            with guard:
                calls += 1
            time.sleep(.05)
            return Response(200, {
                "access_token": "fresh", "refresh_token": "refresh-new",
                "expires_in": 3600, "scope": "offline read:recovery",
            })

        with patch.object(whoop_auth.requests, "post", side_effect=refresh):
            with ThreadPoolExecutor(max_workers=2) as pool:
                values = list(pool.map(
                    lambda _: whoop_auth.get_valid_access_token("client", "secret"),
                    range(2),
                ))
        self.assertEqual(values, ["fresh", "fresh"])
        self.assertEqual(calls, 1)
        persisted = json.loads(Path(self.tokens).read_text(encoding="utf-8"))
        self.assertEqual(persisted["refresh_token"], "refresh-new")

    def test_permanent_oauth_error_is_safe_and_not_retried(self):
        self._expired(refresh="must-not-leak")
        with patch.object(whoop_auth.requests, "post",
                          return_value=Response(400, {"error": "invalid_grant"})) as post:
            with self.assertRaises(whoop_auth.WhoopAuthError) as raised:
                whoop_auth.get_valid_access_token("client", "secret")
        self.assertEqual(raised.exception.category, "invalid_grant")
        self.assertNotIn("must-not-leak", str(raised.exception))
        self.assertEqual(post.call_count, 1)

    def test_ambiguous_refresh_server_error_is_not_replayed(self):
        self._expired()
        before = Path(self.tokens).read_bytes()
        with patch.object(whoop_auth.requests, "post", return_value=Response(503, {
            "error": "temporarily_unavailable",
        })) as post:
            with self.assertRaises(whoop_auth.WhoopAuthError) as raised:
                whoop_auth.get_valid_access_token("client", "secret")
        self.assertEqual(raised.exception.category, "oauth_server_error")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(Path(self.tokens).read_bytes(), before)
        with patch.object(whoop_auth.requests, "post") as replay:
            with self.assertRaises(whoop_auth.WhoopAuthError) as second:
                whoop_auth.get_valid_access_token("client", "secret")
        self.assertEqual(second.exception.category, "refresh_outcome_ambiguous")
        replay.assert_not_called()

    def test_ambiguous_refresh_timeout_is_not_replayed(self):
        self._expired()
        before = Path(self.tokens).read_bytes()
        with patch.object(whoop_auth.requests, "post", side_effect=requests.Timeout) as post:
            with self.assertRaises(whoop_auth.WhoopAuthError) as raised:
                whoop_auth.get_valid_access_token("client", "secret")
        self.assertEqual(raised.exception.category, "network_error")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(Path(self.tokens).read_bytes(), before)
        with patch.object(whoop_auth.requests, "post") as replay:
            with self.assertRaises(whoop_auth.WhoopAuthError) as second:
                whoop_auth.get_valid_access_token("client", "secret")
        self.assertEqual(second.exception.category, "refresh_outcome_ambiguous")
        replay.assert_not_called()


class WhoopAtomicIngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "whoop.db")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _records():
        return {
            "/activity/workout": [{
                "id": "w1", "start": "2026-07-21T10:00:00Z",
                "end": "2026-07-21T11:00:00Z", "sport_name": "Weightlifting",
                "score": {"strain": 10},
            }],
            "/recovery": [{
                "cycle_id": 21, "sleep_id": "s1", "created_at": "2026-07-21T07:00:00Z",
                "score": {"recovery_score": 72, "resting_heart_rate": 52,
                          "hrv_rmssd_milli": 61.2},
            }],
            "/activity/sleep": [{
                "id": "s1", "start": "2026-07-20T22:00:00Z",
                "end": "2026-07-21T07:00:00Z",
                "score": {"sleep_performance_percentage": 91,
                          "sleep_efficiency_percentage": 94,
                          "respiratory_rate": 15.1},
            }],
        }

    def _run(self, paginate):
        with patch.object(fetch_data, "DB_PATH", self.db), \
             patch.object(daily_log, "DB_PATH", self.db), \
             patch.object(fetch_data, "get_valid_access_token", return_value="token"), \
             patch.object(fetch_data, "paginate", side_effect=paginate), \
             patch.object(sys, "argv", ["fetch_data.py", "--days", "2"]):
            fetch_data.main()

    def test_repeated_catchup_is_idempotent(self):
        records = self._records()
        self._run(lambda path, *_: records[path])
        self._run(lambda path, *_: records[path])
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workouts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM recovery").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sleep").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            conn.close()

    def test_provider_failure_before_persistence_is_all_or_nothing(self):
        records = self._records()

        def fail_on_recovery(path, *_):
            if path == "/recovery":
                raise requests.ConnectionError("temporary")
            return records[path]

        with self.assertRaises(requests.ConnectionError):
            self._run(fail_on_recovery)
        # The observability ledger is durable even when domain persistence is
        # aborted; no WHOOP canonical table or partial row may be created.
        conn = sqlite3.connect(self.db)
        try:
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertNotIn("recovery", tables)
            event = conn.execute(
                """SELECT outcome, reason FROM morning_pipeline_events
                   WHERE stage='recovery_imported'
                   ORDER BY event_id DESC LIMIT 1"""
            ).fetchone()
            self.assertEqual(event[0], "failed")
            self.assertEqual(
                event[1], "provider_recovery_fetch_ConnectionError"
            )
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
