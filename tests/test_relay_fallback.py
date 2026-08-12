"""Fallback policy contracts shared by bounded text and cardio vision."""
import base64
import os
import unittest
from unittest.mock import patch

import bounded_agent
import cardio_vision
from gemini_client import GeminiClient, GeminiRejected, GeminiUnavailable
import requests


class Response:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class ScriptedPost:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, _url, **kwargs):
        self.calls.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def tool_result(name="respond_to_user", args=None):
    return {"status": "ok", "result": {
        "name": name,
        "args": args or {"confidence": .95, "reply_text": "safe"},
    }}


def vision_args():
    def field(value, confidence=.95):
        return {"value": value, "confidence": confidence}
    return {
        "activity_type": field("walking"), "duration": field("0:31:05"),
        "strain": field(4.2), "avg_hr_bpm": field(142),
        "max_hr_bpm": field(None), "calories_kcal": field(None),
        "steps": field(None), "distance_km": field(None),
        **{f"zone_{index}_duration": field(None) for index in range(6)},
        "date_ref": {"kind": "unspecified", "value": None, "confidence": .95},
    }


class RelayFallbackTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "GEMINI_TRANSPORT": "relay", "GEMINI_RELAY_URL": "https://relay.example",
            "GEMINI_RELAY_SECRET": "test-secret",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def _text(self, steps):
        post = ScriptedPost(steps)
        client = GeminiClient("", "primary", fallback_models=["fallback"],
                              relay_request_post=post)
        result = client.generate_tool_call(
            "sys", "user", bounded_agent.FUNCTION_DECLARATIONS,
            allowed_names=bounded_agent.ALLOWED_FUNCTIONS,
        )
        return result, post

    def test_text_primary_success_does_not_call_fallback(self):
        result, post = self._text([Response(200, tool_result())])
        self.assertEqual((result.model, result.attempt_count, len(post.calls)), ("primary", 1, 1))

    def test_text_timeout_429_5xx_and_malformed_use_real_fallback_contract(self):
        failures = [
            requests.exceptions.Timeout(), Response(429), Response(503),
            Response(200, {"status": "ok"}),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                result, post = self._text([failure, Response(200, tool_result())])
                self.assertEqual((result.model, result.attempt_count, len(post.calls)), ("fallback", 2, 2))
                self.assertEqual(result.relay_metadata["attempts"][0]["model"], "primary")

    def test_unknown_tool_is_rejected_without_fallback_or_mutation_path(self):
        post = ScriptedPost([Response(200, tool_result("unknown_tool", {}))])
        client = GeminiClient("", "primary", fallback_models=["fallback"], relay_request_post=post)
        with self.assertRaises(GeminiRejected):
            client.generate_tool_call("sys", "user", bounded_agent.FUNCTION_DECLARATIONS,
                                      allowed_names=bounded_agent.ALLOWED_FUNCTIONS)
        self.assertEqual(len(post.calls), 1)

    def test_both_models_unavailable_is_honest_unavailable(self):
        post = ScriptedPost([requests.exceptions.Timeout(), Response(503)])
        client = GeminiClient("", "primary", fallback_models=["fallback"], relay_request_post=post)
        with self.assertRaises(GeminiUnavailable) as raised:
            client.generate_tool_call("sys", "user", bounded_agent.FUNCTION_DECLARATIONS,
                                      allowed_names=bounded_agent.ALLOWED_FUNCTIONS)
        self.assertEqual(len(raised.exception.relay_attempts), 2)

    def test_invalid_relay_auth_is_not_retried(self):
        post = ScriptedPost([Response(401), Response(200, tool_result())])
        client = GeminiClient("", "primary", fallback_models=["fallback"], relay_request_post=post)
        with self.assertRaises(GeminiRejected):
            client.generate_tool_call("sys", "user", bounded_agent.FUNCTION_DECLARATIONS,
                                      allowed_names=bounded_agent.ALLOWED_FUNCTIONS)
        self.assertEqual(len(post.calls), 1)

    def test_invalid_provider_schema_is_not_misclassified_or_retried(self):
        post = ScriptedPost([Response(422, {
            "status": "provider_error",
            "error_category": "invalid_provider_request",
            "request_id": "safe-request-id",
        }, {"X-Relay-Request-ID": "safe-request-id"})])
        client = GeminiClient("", "primary", fallback_models=["fallback"],
                              relay_request_post=post)
        with self.assertRaises(GeminiRejected) as raised:
            client.generate_tool_call(
                "sys", "user", bounded_agent.FUNCTION_DECLARATIONS,
                allowed_names=bounded_agent.ALLOWED_FUNCTIONS,
            )
        self.assertEqual(raised.exception.detail, "invalid_request")
        self.assertEqual(len(post.calls), 1)

    def test_vision_malformed_primary_uses_fallback_preserving_original_bytes(self):
        image = b"\x89PNG\r\n\x1a\nexample"
        first = Response(200, {"status": "ok", "result": {
            "name": "log_cardio_from_image", "args": {},
        }})
        second = Response(200, {"status": "ok", "result": {
            "name": "log_cardio_from_image", "args": vision_args(),
        }})
        post = ScriptedPost([first, second])
        parsed, meta = cardio_vision.extract(
            image, "image/png", "Cardio for yesterday", api_key="",
            models=("primary", "fallback"), request_post=post,
        )
        self.assertEqual((meta["model"], meta["attempt_count"], len(post.calls)), ("fallback", 2, 2))
        self.assertEqual(parsed["type"], "walking")
        for call in post.calls:
            payload = call["json"]
            self.assertEqual(payload["mime_type"], "image/png")
            self.assertEqual(base64.b64decode(payload["image_bytes_b64"]), image)
            self.assertIn("Cardio for yesterday", payload["text"])

    def test_vision_unknown_call_is_rejected_without_fallback(self):
        post = ScriptedPost([Response(200, tool_result("unknown_tool", {}))])
        with self.assertRaises(cardio_vision.VisionProviderError) as raised:
            cardio_vision.extract(b"\x89PNG\r\n\x1a\nx", "image/png", "cardio", api_key="",
                                  models=("primary", "fallback"), request_post=post)
        self.assertEqual(raised.exception.category, "rejected")
        self.assertEqual(len(post.calls), 1)

    def test_vision_model_rejection_uses_fallback(self):
        post = ScriptedPost([
            Response(422),
            Response(200, {"status": "ok", "result": {
                "name": "log_cardio_from_image", "args": vision_args(),
            }}),
        ])
        parsed, meta = cardio_vision.extract(
            b"\x89PNG\r\n\x1a\nx", "image/png", "cardio", api_key="",
            models=("primary", "fallback"), request_post=post,
        )
        self.assertEqual((parsed["type"], meta["model"], meta["attempt_count"]),
                         ("walking", "fallback", 2))


if __name__ == "__main__":
    unittest.main()
