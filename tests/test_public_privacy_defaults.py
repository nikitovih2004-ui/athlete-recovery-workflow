"""Public-export privacy defaults and outbound transport guards."""

import io
import os
import unittest
from unittest.mock import patch

from PIL import Image

import cardio_extraction
import gemini_transport
import phase2_flags
import telegram_bot


class PublicPrivacyDefaultTests(unittest.TestCase):
    def test_external_ai_features_default_off(self):
        names = set(phase2_flags.FLAG_DEFAULTS)
        clean = {key: value for key, value in os.environ.items() if key not in names}
        with patch.dict(os.environ, clean, clear=True):
            self.assertFalse(phase2_flags.router_enabled())
            self.assertFalse(phase2_flags.bounded_agent_enabled())
            self.assertFalse(phase2_flags.factor_capture_enabled())
            self.assertFalse(phase2_flags.weekly_v2_enabled())
            self.assertFalse(phase2_flags.gemini_vision_enabled())

    def test_image_preparation_strips_metadata_and_bounds_dimensions(self):
        source = io.BytesIO()
        image = Image.new("RGB", (3000, 1000), "white")
        exif = Image.Exif()
        exif[0x010E] = "private metadata"
        image.save(source, format="JPEG", exif=exif)

        prepared, mime_type = telegram_bot._prepare_cardio_image(source.getvalue())

        self.assertEqual(mime_type, "image/jpeg")
        with Image.open(io.BytesIO(prepared)) as result:
            self.assertLessEqual(max(result.size), 2048)
            self.assertEqual(len(result.getexif()), 0)

    def test_oversized_encoded_image_is_rejected_before_decode(self):
        with self.assertRaises(cardio_extraction.CardioExtractionError):
            telegram_bot._prepare_cardio_image(b"x" * (8 * 1024 * 1024 + 1))

    def test_relay_requires_https_and_disables_redirects(self):
        environment = {
            "GEMINI_TRANSPORT": "relay",
            "GEMINI_RELAY_URL": "http://relay.example",
            "GEMINI_RELAY_SECRET": "test-only-secret",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(gemini_transport.RelayTransportError):
                gemini_transport.relay_call(
                    text="synthetic", model="test", response_schema={},
                    request_post=lambda *_args, **_kwargs: None,
                )

        calls = []

        class Response:
            status_code = 200
            headers = {}

            @staticmethod
            def json():
                return {"status": "ok", "result": {}}

        def post(*args, **kwargs):
            calls.append((args, kwargs))
            return Response()

        environment["GEMINI_RELAY_URL"] = "https://relay.example"
        with patch.dict(os.environ, environment, clear=True):
            gemini_transport.relay_call(
                text="synthetic", model="test", response_schema={},
                request_post=post,
            )
        self.assertFalse(calls[0][1]["allow_redirects"])


if __name__ == "__main__":
    unittest.main()
