import base64
import datetime as dt
from io import BytesIO
import unittest

import cardio_extraction as c
import cardio_vision
import telegram_bot
from PIL import Image, ImageDraw


CONFIDENCE = {
    "activity_confidence": .95,
    "duration_confidence": .95,
    "effort_confidence": .95,
}


def _fixture_image():
    """Synthetic image: it deliberately contains no acceptance-image data."""
    image = Image.new("RGB", (720, 1280), "black")
    draw = ImageDraw.Draw(image)
    draw.text((40, 40), "WALKING", fill="white")
    draw.text((40, 100), "4.2 STRAIN  987 STEPS", fill="white")
    draw.text((40, 160), "DURATION 0:31:05", fill="white")
    draw.text((40, 220), "AVG HR 142  CALORIES 260", fill="white")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _typed_args(*, activity_type="walking", activity_confidence=.95,
                duration="0:31:05", strain=4.2, avg_hr=142,
                calories=260, steps=987):
    def field(value, confidence=.95):
        return {"value": value, "confidence": confidence}

    return {
        "activity_type": field(activity_type, activity_confidence),
        "duration": field(duration),
        "strain": field(strain), "avg_hr_bpm": field(avg_hr),
        "max_hr_bpm": field(None), "calories_kcal": field(calories),
        "steps": field(steps), "distance_km": field(None),
        **{f"zone_{index}_duration": field(None) for index in range(6)},
        "date_ref": {"kind": "unspecified", "value": None, "confidence": .95},
    }


def _function_response(name, args):
    return {"candidates": [{"content": {"parts": [{"functionCall": {
        "name": name, "args": args,
    }}]}}]}


def _response(body):
    return type("Response", (), {
        "status_code": 200,
        "json": lambda self: body,
    })()


class CardioContractTests(unittest.TestCase):
    def test_whoop_walking_minimum(self):
        value = c.validate({"activity_type": "walking", "duration_seconds": 1723,
            "strain": 7.5, "avg_hr_bpm": 136, "calories_kcal": 198, "steps": 1787,
            "max_hr_bpm": None, "distance_km": None, **CONFIDENCE})
        self.assertEqual(value["type"], "walking")
        self.assertAlmostEqual(value["duration"], 28.717, places=3)
        self.assertEqual(value["avg_hr"], 136)
        self.assertEqual(value["strain"], 7.5)
        self.assertEqual(value["steps"], 1787)
        visible = c.validate({
            "activity_type": "RACE WALKING", "duration": "0:28:43",
            "strain": "7.5", "avg_hr_bpm": "136 bpm",
            "calories_kcal": "198 cals", "steps": "1,787",
            "extra_model_field": "ignored", **CONFIDENCE,
        })
        self.assertEqual((visible["type"], visible["steps"], visible["duration"]),
                         ("walking", 1787, 28.717))

    def test_optional_fields_do_not_block_when_a_persisted_effort_exists(self):
        self.assertEqual(c.validate({"activity_type": "cardio", "duration_seconds": 60,
            "strain": 1.0, "avg_hr_bpm": 120, "max_hr_bpm": None,
            "calories_kcal": None, "distance_km": None, "steps": None,
            **CONFIDENCE})["type"], "cardio")
        value = c.validate({
            "activity_type": "walking", "duration_seconds": 60,
            "strain": "N/A", "avg_hr_bpm": 120, "max_hr_bpm": "N/A",
            "calories_kcal": None, "distance_km": "N/A", "steps": "N/A",
            **CONFIDENCE,
        })
        self.assertIsNone(value["strain"])
        self.assertIsNone(value["steps"])

    def test_malformed_or_missing_effort_rejected(self):
        with self.assertRaises(c.CardioExtractionError):
            c.parse_json("not json")
        with self.assertRaises(c.CardioExtractionError):
            c.validate({"activity_type": "walking", "duration_seconds": 10})
        with self.assertRaisesRegex(c.CardioExtractionError, "activity_type"):
            c.validate({"activity_type": "walking", "duration_seconds": 60,
                        "avg_hr_bpm": 120,
                        **{**CONFIDENCE, "activity_confidence": .2}})
        with self.assertRaisesRegex(c.CardioExtractionError, "duration"):
            c.validate({"activity_type": "walking", "duration_seconds": 60,
                        "avg_hr_bpm": 120,
                        **{**CONFIDENCE, "duration_confidence": .89}})

    def test_primary_failure_fallback_success_metadata(self):
        responses = [
            _response({"candidates": [{"content": {"parts": [{"text": "not a call"}]}}]}),
            _response(_function_response("log_cardio_from_image", _typed_args())),
        ]
        parsed, meta = cardio_vision.extract(
            _fixture_image(), "image/png", "cardio", api_key="fake",
            models=("primary", "fallback"),
            request_post=lambda _url, **_kwargs: responses.pop(0),
        )
        self.assertEqual((parsed["avg_hr"], parsed["strain"], parsed["steps"]),
                         (142, 4.2, 987))
        self.assertEqual(meta["model"], "fallback")
        self.assertEqual(meta["attempt_count"], 2)

    def test_preprocessing_and_typed_payload_use_real_image_bytes(self):
        captured = {}
        image = _fixture_image()

        def post(_url, **kwargs):
            captured["payload"] = kwargs["json"]
            return _response(_function_response("log_cardio_from_image", _typed_args()))

        parsed, _ = cardio_vision.extract(
            image, "image/png", "Cardio for yesterday", api_key="fake",
            models=("primary",), request_post=post,
        )
        payload = captured["payload"]
        self.assertIn("functionDeclarations", payload["tools"][0])
        self.assertEqual(
            payload["toolConfig"]["functionCallingConfig"]["allowedFunctionNames"],
            sorted(cardio_vision.ALLOWED_CALLS),
        )
        inline = payload["contents"][0]["parts"][1]["inlineData"]
        self.assertEqual(inline["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(inline["data"]), image)
        self.assertIn("Cardio for yesterday", payload["contents"][0]["parts"][0]["text"])
        self.assertEqual((parsed["type"], parsed["duration"], parsed["strain"],
                          parsed["avg_hr"], parsed["calories"], parsed["steps"]),
                         ("walking", 31.083, 4.2, 142, 260.0, 987))

    def test_caption_never_overrides_an_unreadable_activity_type(self):
        args = _typed_args(activity_type=None, activity_confidence=.1)
        args["missing_field"] = "activity_type"
        parsed, _ = cardio_vision.extract(
            _fixture_image(), "image/png", "Cardio for yesterday", api_key="fake",
            models=("primary",), request_post=lambda _url, **_kwargs: _response(
                _function_response("request_cardio_clarification", args)
            ),
        )
        self.assertIsNone(parsed["type"])
        self.assertEqual(parsed["needs_clarification"], "activity_type")
        self.assertEqual((parsed["steps"], parsed["avg_hr"]), (987, 142))

    def test_unknown_multiple_or_malformed_calls_are_rejected_before_persistence(self):
        valid_call = {"functionCall": {
            "name": "log_cardio_from_image", "args": _typed_args(),
        }}
        bodies = {
            "unknown": _function_response("not_an_allowlisted_tool", {}),
            "multiple": {"candidates": [{"content": {"parts": [
                valid_call, valid_call,
            ]}}]},
            "malformed": {"candidates": [{"content": {"parts": [
                {"functionCall": {"name": "log_cardio_from_image", "args": {}}},
            ]}}]},
        }
        for name, body in bodies.items():
            with self.subTest(name=name):
                with self.assertRaises(cardio_vision.VisionProviderError) as raised:
                    cardio_vision.extract(
                        _fixture_image(), "image/png", "cardio", api_key="fake",
                        models=("primary",), request_post=lambda _url, **_kwargs: _response(body),
                    )
                self.assertEqual(raised.exception.category, "rejected")

    def test_provider_outage_remains_distinct_from_invalid_extraction(self):
        outage = type("Response", (), {"status_code": 503})()
        with self.assertRaises(cardio_vision.VisionProviderError) as raised:
            cardio_vision.extract(
                _fixture_image(), "image/png", "cardio", api_key="fake",
                models=("primary",), request_post=lambda _url, **_kwargs: outage,
            )
        self.assertEqual(raised.exception.category, "unavailable")

    def test_photo_caption_explicit_date_overrides_delivery_date(self):
        now = dt.datetime(2026, 7, 16, 18, 0, tzinfo=dt.timezone(
            dt.timedelta(hours=3)
        ))
        message = type("Message", (), {
            "caption": "Кардио за вчера",
            "date": int(dt.datetime(2026, 7, 16, 15, tzinfo=dt.timezone.utc).timestamp()),
        })()
        self.assertEqual(telegram_bot._telegram_target_date(message, now), "2026-07-15")


if __name__ == '__main__':
    unittest.main()
