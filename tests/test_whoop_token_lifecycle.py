"""Single-owner OAuth handoff and rotating-token lifecycle acceptance tests."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import deploy
import token_handoff
import whoop_auth


class Response:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.ok = 200 <= status < 300

    def json(self):
        return self._payload


class WhoopTokenLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.local = self.root / "workstation"
        self.target = self.root / "production"
        self.local.mkdir()
        self.target.mkdir()
        self.env = {
            "WHOOP_CLIENT_ID": "client",
            "WHOOP_REDIRECT_URI": "http://localhost:8080/callback",
            "WHOOP_TRANSFER_MARKER": str(self.local / "token.transferred"),
        }

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, refresh="refresh-0"):
        return {
            "access_token": "access-0", "refresh_token": refresh,
            "expires_in": 3600, "scope": "offline read:recovery read:sleep",
        }

    def validate(self, payload=None, **overrides):
        args = {
            "authorization_client_id": "client",
            "authorization_redirect_uri": "http://localhost:8080/callback",
            "expected_client_id": "client",
            "expected_redirect_uri": "http://localhost:8080/callback",
        }
        args.update(overrides)
        return whoop_auth.validate_authorization_tokens(payload or self.payload(), **args)

    def test_authorization_rejects_incomplete_or_unbound_responses(self):
        for mutation, category in (
            ({"access_token": ""}, "access_token_missing"),
            ({"refresh_token": ""}, "refresh_token_missing"),
            ({"scope": "read:recovery"}, "offline_scope_missing"),
        ):
            payload = {**self.payload(), **mutation}
            with self.assertRaises(whoop_auth.WhoopAuthError) as raised:
                self.validate(payload)
            self.assertEqual(raised.exception.category, category)
        with self.assertRaises(whoop_auth.WhoopAuthError) as raised:
            self.validate(authorization_client_id="other")
        self.assertEqual(raised.exception.category, "client_id_mismatch")
        with self.assertRaises(whoop_auth.WhoopAuthError) as raised:
            self.validate(authorization_redirect_uri="http://wrong")
        self.assertEqual(raised.exception.category, "redirect_uri_mismatch")

    def test_single_owner_install_two_rotations_restart_and_local_refusal(self):
        tokens = self.validate()
        local_tokens = self.local / "tokens.json"
        local_tokens.write_text(json.dumps(tokens), encoding="utf-8")
        target_tokens = self.target / "tokens.json"
        upload = self.target / ".tokens-upload-test.json"
        upload.write_text(json.dumps({
            "tokens": tokens,
            "authorization_context": {
                "client_id": "client",
                "redirect_uri": "http://localhost:8080/callback",
            },
        }), encoding="utf-8")

        with patch.dict(os.environ, self.env, clear=False), \
             patch.object(token_handoff, "HERE", self.target), \
             patch.object(whoop_auth, "TOKENS_FILE", str(target_tokens)), \
             patch.object(whoop_auth, "LOCK_FILE", str(target_tokens) + ".lock"):
            installed = token_handoff.accept_uploaded_token(upload, service_user="unused")
            self.assertTrue(installed["ok"])
            self.assertFalse(upload.exists())
            self.assertEqual(
                json.loads(target_tokens.read_text(encoding="utf-8"))["refresh_token"],
                "refresh-0",
            )

            with patch.object(whoop_auth.requests, "post", return_value=Response(200, {
                "access_token": "access-1", "refresh_token": "refresh-1",
                "expires_in": 3600, "scope": "offline read:recovery read:sleep",
            })) as first_post, patch.object(whoop_auth, "_is_current", return_value=False):
                self.assertEqual(whoop_auth.get_valid_access_token("client", "secret"), "access-1")
            self.assertEqual(first_post.call_args.kwargs["data"]["refresh_token"], "refresh-0")
            first = json.loads(target_tokens.read_text(encoding="utf-8"))
            self.assertEqual(first["refresh_token"], "refresh-1")

            # A process restart reloads the persisted newest token before rotating again.
            with patch.object(whoop_auth.requests, "post", return_value=Response(200, {
                "access_token": "access-2", "refresh_token": "refresh-2",
                "expires_in": 3600, "scope": "offline read:recovery read:sleep",
            })) as second_post, patch.object(whoop_auth, "_is_current", return_value=False):
                self.assertEqual(whoop_auth.get_valid_access_token("client", "secret"), "access-2")
            self.assertEqual(second_post.call_args.kwargs["data"]["refresh_token"], "refresh-1")
            second = json.loads(target_tokens.read_text(encoding="utf-8"))
            self.assertEqual(second["refresh_token"], "refresh-2")
            audit = [json.loads(line) for line in
                     Path(whoop_auth.audit_file_path()).read_text(encoding="utf-8").splitlines()]
            self.assertEqual([item["result_category"] for item in audit], [
                "installed_single_owner", "rotated", "rotated",
            ])
            self.assertEqual(
                audit[1]["old_token_fingerprint_short"],
                whoop_auth.short_token_fingerprint("refresh-0"),
            )
            self.assertEqual(
                audit[1]["new_token_fingerprint_short"],
                whoop_auth.short_token_fingerprint("refresh-1"),
            )
            for event in audit:
                self.assertEqual(event["token_store"], str(target_tokens.resolve()))
                self.assertEqual(event["refresh_lock"], str(target_tokens.resolve()) + ".lock")
                self.assertTrue(event["refresh_lock_held"])
                self.assertIsInstance(event["pid"], int)
                self.assertTrue(event["process"])
                self.assertIn(event["actor"], {
                    "manual_or_handoff", "morning_pipeline", "systemd",
                })
                self.assertTrue(event["token_store_write_confirmed"])
                self.assertIsNotNone(event["access_token_expires_at"])

        with patch.dict(os.environ, self.env, clear=False), \
             patch.object(whoop_auth, "TOKENS_FILE", str(local_tokens)), \
             patch.object(whoop_auth, "LOCK_FILE", str(local_tokens) + ".lock"):
            token_handoff.finalize_workstation_handoff(
                whoop_auth.token_fingerprint("refresh-0"), "production.example"
            )
            self.assertFalse(local_tokens.exists())
            self.assertTrue(Path(whoop_auth.transferred_marker_path()).exists())
            # Even if a stale copy reappears, the workstation marker fails closed.
            local_tokens.write_text(json.dumps(tokens), encoding="utf-8")
            with self.assertRaises(whoop_auth.WhoopAuthError) as raised:
                whoop_auth.get_valid_access_token("client", "secret")
            self.assertEqual(raised.exception.category, "token_transferred_to_production")

    def test_failed_refresh_audit_has_no_token_values(self):
        target_tokens = self.target / "tokens.json"
        target_tokens.write_text(json.dumps({**self.payload(), "obtained_at": 1}), encoding="utf-8")
        before_bytes = target_tokens.read_bytes()
        before_inode = target_tokens.stat().st_ino
        with patch.dict(os.environ, {"WHOOP_TRANSFER_MARKER": str(self.target / "none")}), \
             patch.object(whoop_auth, "TOKENS_FILE", str(target_tokens)), \
             patch.object(whoop_auth, "LOCK_FILE", str(target_tokens) + ".lock"), \
             patch.object(whoop_auth.requests, "post", return_value=Response(400, {
                 "error": "invalid_grant",
             })):
            with self.assertRaises(whoop_auth.WhoopAuthError):
                whoop_auth.get_valid_access_token("client", "secret")
            self.assertEqual(target_tokens.read_bytes(), before_bytes)
            self.assertEqual(target_tokens.stat().st_ino, before_inode)
            raw = Path(whoop_auth.audit_file_path()).read_text(encoding="utf-8")
            self.assertNotIn("refresh-0", raw)
            event = json.loads(raw)
            self.assertEqual(event["result_category"], "invalid_grant")
            self.assertEqual(
                event["token_fingerprint_short"],
                whoop_auth.short_token_fingerprint("refresh-0"),
            )

    def test_force_refresh_rotates_a_current_token(self):
        target_tokens = self.target / "tokens.json"
        with patch.dict(os.environ, {
                 "WHOOP_TRANSFER_MARKER": str(self.target / "none"),
             }), patch.object(whoop_auth, "TOKENS_FILE", str(target_tokens)), \
             patch.object(whoop_auth, "LOCK_FILE", str(target_tokens) + ".lock"):
            whoop_auth.save_tokens({
                "access_token": "access-current",
                "refresh_token": "refresh-current",
                "scope": "offline read:recovery",
                "expires_in": 3600,
            })
            response = Response(200, {
                "access_token": "access-next",
                "refresh_token": "refresh-next",
                "scope": "offline read:recovery",
                "expires_in": 3600,
            })
            with patch.object(whoop_auth.requests, "post", return_value=response) as post:
                access = whoop_auth.get_valid_access_token(
                    "client", "secret", force_refresh=True,
                )
            self.assertEqual("access-next", access)
            self.assertEqual("refresh-next", whoop_auth.load_tokens()["refresh_token"])
            self.assertEqual("refresh-current", post.call_args.kwargs["data"]["refresh_token"])

    def test_deploy_and_rollback_inventory_excludes_token_storage(self):
        for name in (
            "tokens.json", "tokens.json.lock", "tokens.json.transferred",
            "token_rotation_audit.jsonl",
        ):
            path = deploy.HERE / name
            self.assertTrue(deploy.is_excluded(path))


if __name__ == "__main__":
    unittest.main()
