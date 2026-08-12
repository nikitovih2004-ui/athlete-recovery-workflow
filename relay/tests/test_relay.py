import base64
import io
import json
import os
import unittest
from contextlib import redirect_stdout

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["label", "score"],
}


class RelayContractTests(unittest.TestCase):
    def setUp(self):
        self.original = os.environ.get("GEMINI_ALLOWED_MODELS")
        os.environ["GEMINI_ALLOWED_MODELS"] = "gemini-2.5-flash"

    def tearDown(self):
        if self.original is None:
            os.environ.pop("GEMINI_ALLOWED_MODELS", None)
        else:
            os.environ["GEMINI_ALLOWED_MODELS"] = self.original

    def test_text_and_image_request_is_parsed_in_memory(self):
        request = app.parse_request({
            "text": "Return a label.",
            "image_bytes_b64": base64.b64encode(b"png-bytes").decode("ascii"),
            "mime_type": "image/png",
            "model": "gemini-2.5-flash",
            "response_schema": SCHEMA,
        })
        self.assertEqual(request["image"], b"png-bytes")
        self.assertEqual(request["model"], "gemini-2.5-flash")

    def test_model_allowlist_is_enforced(self):
        with self.assertRaises(app.ClientError):
            app.parse_request({"text": "x", "model": "gemini-2.5-pro", "response_schema": SCHEMA})

    def test_response_schema_is_checked_before_return(self):
        checked = app._validate_schema(SCHEMA)
        self.assertTrue(app._matches_schema({"label": "HELLO", "score": 1}, checked))
        self.assertFalse(app._matches_schema({"label": "HELLO"}, checked))
        self.assertFalse(app._matches_schema({"label": "HELLO", "score": True}, checked))
        self.assertFalse(app._matches_schema({"label": "HELLO", "score": 1, "extra": 0}, checked))

    def test_unknown_request_fields_are_rejected(self):
        with self.assertRaises(app.ClientError):
            app.parse_request({"text": "x", "model": "gemini-2.5-flash", "response_schema": SCHEMA, "other": "no"})

    def test_provider_http_errors_have_safe_specific_categories(self):
        self.assertEqual(app._provider_http_category(400), "invalid_provider_request")
        self.assertEqual(app._provider_http_category(403), "provider_authorization")
        self.assertEqual(app._provider_http_category(429), "provider_rate_limited")
        self.assertEqual(app._provider_http_category(503), "provider_upstream_5xx")

    def test_safe_error_log_has_location_but_no_sensitive_material(self):
        output = io.StringIO()
        try:
            raise app.ProviderError(
                "invalid_provider_request", provider_status=400,
                exception_class="HTTPError",
            )
        except app.ProviderError as exc:
            with redirect_stdout(output):
                app._log_provider_error(
                    exc, request_id="opaque-request-id",
                    model="gemini-2.5-flash",
                )
        event = json.loads(output.getvalue())
        self.assertEqual(event["safe_category"], "invalid_provider_request")
        self.assertEqual(event["provider_http_status"], 400)
        self.assertEqual(event["exception_class"], "HTTPError")
        self.assertEqual(event["source_file"], "app.py")
        self.assertNotIn("PRIVATE PROMPT", output.getvalue())
        self.assertNotIn("PRIVATE SECRET", output.getvalue())

    def test_provider_probe_schema_is_typed(self):
        schema = app._validate_schema({
            "type": "object",
            "properties": {"ready": {"type": "boolean"}},
            "required": ["ready"],
        })
        self.assertTrue(app._matches_schema({"ready": True}, schema))

    def test_gemini_3_uses_low_thinking_without_sampling_parameters(self):
        config = app._provider_generation_config("gemini-3.6-flash")
        self.assertEqual(
            config, {"thinkingConfig": {"thinkingLevel": "low"}}
        )
        self.assertNotIn("temperature", config)
        self.assertEqual(
            app._provider_generation_config("gemini-2.5-flash"),
            {"temperature": 0},
        )

    def test_function_call_is_selected_after_extra_response_part(self):
        body = {"candidates": [{"content": {"parts": [
            {"text": "internal planning", "thought": True},
            {"functionCall": {
                "name": "return_daily_analysis",
                "args": {"analysis_markdown": "safe"},
            }},
        ]}}]}
        selected = app._select_response_part(body, "functionCall")
        self.assertEqual(
            selected["functionCall"]["name"], "return_daily_analysis"
        )

    def test_request_id_accepts_only_bounded_opaque_values(self):
        self.assertEqual(app._request_id("opaque-request-id"), "opaque-request-id")
        self.assertRegex(app._request_id("bad id with spaces"), r"^[a-f0-9]{32}$")


if __name__ == "__main__":
    unittest.main()
