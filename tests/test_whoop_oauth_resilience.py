import datetime as dt
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import auth
import fetch_data
import oauth_alerting
import oauth_observability
import token_handoff
import whoop_auth


class OAuthResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


class WhoopOAuthResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tokens = self.root / "tokens.json"
        self.ambiguous = self.root / "tokens.json.refresh-ambiguous"
        self.runtime = self.root / "oauth_runtime_state.json"
        self.alert = self.root / "oauth_alert_state.json"
        self.paths = patch.multiple(
            whoop_auth,
            TOKENS_FILE=str(self.tokens),
            LOCK_FILE=str(self.tokens) + ".lock",
            AMBIGUOUS_FILE=str(self.ambiguous),
            RUNTIME_STATE_FILE=str(self.runtime),
        )
        self.paths.start()
        self.alert_path = patch.object(
            oauth_alerting, "ALERT_STATE_FILE", self.alert,
        )
        self.alert_path.start()
        self.marker = patch.dict(
            os.environ,
            {"WHOOP_TRANSFER_MARKER": str(self.root / "none")},
            clear=False,
        )
        self.marker.start()

    def tearDown(self):
        self.marker.stop()
        self.alert_path.stop()
        self.paths.stop()
        self.temp.cleanup()

    def _tokens(self, *, age=0, expires_in=3600, refresh="refresh-old"):
        self.tokens.write_text(json.dumps({
            "access_token": "access-old",
            "refresh_token": refresh,
            "scope": "offline read:recovery",
            "obtained_at": time.time() - age,
            "expires_in": expires_in,
        }), encoding="utf-8")

    @staticmethod
    def _success():
        return OAuthResponse(200, {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "scope": "offline read:recovery",
            "expires_in": 3600,
        })

    def test_valid_access_token_does_not_refresh(self):
        self._tokens(age=10)
        with patch.object(whoop_auth.requests, "post") as post:
            value = whoop_auth.get_valid_access_token("client", "secret")
        self.assertEqual(value, "access-old")
        post.assert_not_called()

    def test_refresh_only_inside_configured_expiry_window(self):
        self._tokens(age=3500)
        with patch.dict(
            os.environ, {"WHOOP_REFRESH_MARGIN_SECONDS": "60"}, clear=False,
        ), patch.object(
            whoop_auth.requests, "post", return_value=self._success(),
        ) as post:
            self.assertEqual(
                whoop_auth.get_valid_access_token("client", "secret"),
                "access-old",
            )
            post.assert_not_called()
        self._tokens(age=3550)
        with patch.object(
            whoop_auth.requests, "post", return_value=self._success(),
        ) as post:
            self.assertEqual(
                whoop_auth.get_valid_access_token("client", "secret"),
                "access-new",
            )
            post.assert_called_once()

    def test_successful_rotation_clears_inflight_marker(self):
        self._tokens(age=4000)
        with patch.object(
            whoop_auth.requests, "post", return_value=self._success(),
        ):
            whoop_auth.get_valid_access_token("client", "secret")
        self.assertFalse(self.ambiguous.exists())
        self.assertEqual(
            whoop_auth.load_tokens()["refresh_token"], "refresh-new",
        )

    def test_http_400_is_terminal_and_not_replayed(self):
        self._tokens(age=4000)
        with patch.object(
            whoop_auth.requests, "post",
            return_value=OAuthResponse(400, {"error": "invalid_grant"}),
        ) as post, patch.object(
            oauth_alerting, "notify_authorization_required",
            return_value=True,
        ):
            with self.assertRaises(whoop_auth.WhoopAuthError):
                whoop_auth.get_valid_access_token("client", "secret")
            with self.assertRaises(whoop_auth.WhoopAuthError) as replay:
                whoop_auth.get_valid_access_token("client", "secret")
        self.assertEqual(replay.exception.category, "refresh_outcome_ambiguous")
        self.assertEqual(post.call_count, 1)

    def test_http_502_is_quarantined_and_not_replayed(self):
        self._tokens(age=4000)
        with patch.object(
            whoop_auth.requests, "post",
            return_value=OAuthResponse(502, {}),
        ) as post, patch.object(
            oauth_alerting, "notify_authorization_required",
            return_value=True,
        ):
            with self.assertRaises(whoop_auth.WhoopAuthError):
                whoop_auth.get_valid_access_token("client", "secret")
            with self.assertRaises(whoop_auth.WhoopAuthError):
                whoop_auth.get_valid_access_token("client", "secret")
        self.assertEqual(post.call_count, 1)

    def test_connect_timeout_is_proven_not_sent_and_not_quarantined(self):
        self._tokens(age=4000)
        with patch.object(
            whoop_auth.requests, "post",
            side_effect=requests.ConnectTimeout(),
        ):
            with self.assertRaises(whoop_auth.WhoopAuthError) as raised:
                whoop_auth.get_valid_access_token("client", "secret")
        self.assertEqual(
            raised.exception.category, "connect_timeout_request_not_sent",
        )
        self.assertFalse(self.ambiguous.exists())

    def test_read_timeout_may_have_been_sent_and_is_quarantined(self):
        self._tokens(age=4000)
        with patch.object(
            whoop_auth.requests, "post", side_effect=requests.ReadTimeout(),
        ), patch.object(
            oauth_alerting, "notify_authorization_required",
            return_value=True,
        ):
            with self.assertRaises(whoop_auth.WhoopAuthError):
                whoop_auth.get_valid_access_token("client", "secret")
        self.assertTrue(self.ambiguous.exists())

    def test_process_crash_after_response_leaves_preflight_quarantine(self):
        self._tokens(age=4000)
        with patch.object(
            whoop_auth.requests, "post", return_value=self._success(),
        ), patch.object(whoop_auth, "save_tokens", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                whoop_auth.get_valid_access_token("client", "secret")
        self.assertTrue(self.ambiguous.exists())
        with patch.object(whoop_auth.requests, "post") as replay:
            with self.assertRaises(whoop_auth.WhoopAuthError):
                whoop_auth.get_valid_access_token("client", "secret")
        replay.assert_not_called()

    def test_disk_write_failure_is_quarantined(self):
        self._tokens(age=4000)
        with patch.object(
            whoop_auth.requests, "post", return_value=self._success(),
        ), patch.object(
            whoop_auth, "save_tokens", side_effect=OSError("disk full"),
        ), patch.object(
            oauth_alerting, "notify_authorization_required",
            return_value=True,
        ):
            with self.assertRaises(whoop_auth.WhoopAuthError) as raised:
                whoop_auth.get_valid_access_token("client", "secret")
        self.assertEqual(raised.exception.category, "token_persistence_failed")
        self.assertTrue(self.ambiguous.exists())
        self.assertNotIn("disk full", str(raised.exception))

    def test_restart_with_matching_quarantine_never_posts(self):
        self._tokens(age=4000)
        whoop_auth._write_ambiguous_refresh(
            "refresh-old", "refresh_inflight",
        )
        with patch.object(whoop_auth.requests, "post") as post, patch.object(
            oauth_alerting, "notify_authorization_required",
            return_value=False,
        ):
            with self.assertRaises(whoop_auth.WhoopAuthError):
                whoop_auth.get_valid_access_token("client", "secret")
        post.assert_not_called()

    def test_immediate_owner_alert_is_redacted_and_deduplicated(self):
        response = Mock()
        response.raise_for_status.return_value = None
        with patch.dict(os.environ, {
            "TELEGRAM_BOT_TOKEN": "bot-secret",
            "TELEGRAM_CHAT_ID": "123",
        }, clear=False), patch.object(
            oauth_alerting.requests, "post", return_value=response,
        ) as post:
            self.assertTrue(oauth_alerting.notify_authorization_required(
                reason="oauth_server_error",
                refresh_fingerprint="full-internal-fingerprint",
            ))
            self.assertFalse(oauth_alerting.notify_authorization_required(
                reason="oauth_server_error",
                refresh_fingerprint="full-internal-fingerprint",
            ))
        self.assertEqual(post.call_count, 1)
        sent_text = post.call_args.kwargs["data"]["text"]
        self.assertNotIn("bot-secret", sent_text)
        self.assertNotIn("full-internal-fingerprint", sent_text)
        self.assertIn("does not prove that WHOOP consent was revoked", sent_text)
        self.assertNotIn("authorization is required", sent_text.lower())
        self.assertNotIn("venv/Scripts/python.exe auth.py", sent_text)

    def test_redacted_observability_reports_quarantine(self):
        self._tokens(age=4000)
        whoop_auth._write_ambiguous_refresh(
            "refresh-old", "oauth_server_error",
        )
        state = oauth_observability.snapshot(
            dt.datetime.now(dt.timezone.utc),
        )
        self.assertTrue(state["authorization_required"])
        self.assertFalse(
            state["ingestion_can_continue_using_current_access_token"]
        )
        self.assertEqual(len(state["refresh_token_fingerprint_short"]), 8)
        self.assertNotIn("refresh-old", json.dumps(state))

    def test_next_cron_after_midnight_is_same_morning(self):
        state = oauth_observability.snapshot(
            dt.datetime(2026, 7, 25, 21, 32, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(
            state["next_scheduled_cron"], "2026-07-26T03:07:00+00:00",
        )


class WhoopApi401Tests(unittest.TestCase):
    @staticmethod
    def _response(status, payload=None):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload or {}).encode()
        return response

    def test_api_401_causes_one_justified_refresh(self):
        state = {"access_token": "old", "force_refresh_used": False}
        with patch.object(
            fetch_data.requests, "get",
            side_effect=[
                self._response(401),
                self._response(200, {"records": []}),
            ],
        ), patch.object(
            fetch_data, "get_valid_access_token", return_value="new",
        ) as refresh, patch.object(fetch_data, "record_runtime_fact"):
            result = fetch_data.get_json("/recovery", state)
        self.assertEqual(result, {"records": []})
        refresh.assert_called_once_with(
            fetch_data.CLIENT_ID,
            fetch_data.CLIENT_SECRET,
            force_refresh=True,
        )
        self.assertEqual(state["access_token"], "new")

    def test_api_5xx_never_triggers_refresh(self):
        with patch.object(
            fetch_data.requests, "get",
            return_value=self._response(502),
        ), patch.object(fetch_data, "get_valid_access_token") as refresh, \
             patch("time.sleep"):
            with self.assertRaises(requests.HTTPError):
                fetch_data.get_json("/recovery", "old")
        refresh.assert_not_called()


class AuthorizationCallbackTests(unittest.TestCase):
    def setUp(self):
        auth._callback_consumed = False

    def test_state_validation_and_one_time_callback(self):
        with self.assertRaisesRegex(ValueError, "authorization_state_invalid"):
            auth.consume_authorization_callback({
                "state": ["wrong"], "code": ["secret-code"],
            })
        self.assertEqual(
            auth.consume_authorization_callback({
                "state": [auth.STATE], "code": ["secret-code"],
            }),
            "secret-code",
        )
        with self.assertRaisesRegex(
            ValueError, "authorization_callback_already_consumed",
        ):
            auth.consume_authorization_callback({
                "state": [auth.STATE], "code": ["other-code"],
            })


if __name__ == "__main__":
    unittest.main()
