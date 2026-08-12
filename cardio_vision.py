"""Bounded Gemini Vision extraction for cardio screenshots.

The model receives the original Telegram bytes and may return exactly one of
two typed function calls.  Python validates every value before persistence.
"""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import json
import time

import requests
from PIL import Image, ImageChops, ImageOps

import cardio_extraction
import gemini_transport

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
PERMANENT_STATUS = frozenset({400, 401, 403, 404, 413})
PROMPT_VERSION = "cardio_vision_tool_v1"


class VisionProviderError(RuntimeError):
    def __init__(self, category, *, model=None, attempts=0, latency_ms=None,
                 status_code=None, partial=None):
        super().__init__(category)
        self.category = category
        self.model = model
        self.attempt_count = attempts
        self.latency_ms = latency_ms
        self.status_code = status_code
        self.partial = partial or {}
        self.relay_attempts = ()


def _value_field(value_type):
    return {
        "type": "object",
        "properties": {
            "value": {"type": value_type, "nullable": True},
            "confidence": {"type": "number"},
        },
        "required": ["value", "confidence"],
    }


_EXTRACTION_PROPERTIES = {
    "activity_type": _value_field("string"),
    "duration": _value_field("string"),
    "strain": _value_field("number"),
    "avg_hr_bpm": _value_field("number"),
    "max_hr_bpm": _value_field("number"),
    "calories_kcal": _value_field("number"),
    "steps": _value_field("integer"),
    "distance_km": _value_field("number"),
    "zone_0_duration": _value_field("string"),
    "zone_1_duration": _value_field("string"),
    "zone_2_duration": _value_field("string"),
    "zone_3_duration": _value_field("string"),
    "zone_4_duration": _value_field("string"),
    "zone_5_duration": _value_field("string"),
    "date_ref": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["today", "yesterday", "absolute", "unspecified"],
            },
            "value": {"type": "string", "nullable": True},
            "confidence": {"type": "number"},
        },
        "required": ["kind", "value", "confidence"],
    },
}
_EXTRACTION_REQUIRED = list(_EXTRACTION_PROPERTIES)

FUNCTION_DECLARATIONS = [
    {
        "name": "log_cardio_from_image",
        "description": (
            "Use when activity type, duration, and at least one reliable effort "
            "metric (average HR, calories, or strain) are visible. Transcribe "
            "all six heart-rate-zone durations when the card shows them."
        ),
        "parameters": {
            "type": "object",
            "properties": _EXTRACTION_PROPERTIES,
            "required": _EXTRACTION_REQUIRED,
        },
    },
    {
        "name": "request_cardio_clarification",
        "description": (
            "Use only when exactly one required field is not reliably readable. "
            "Preserve every field that was reliably extracted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                **_EXTRACTION_PROPERTIES,
                "missing_field": {
                    "type": "string",
                    "enum": ["activity_type", "duration", "effort_metric"],
                },
            },
            "required": [*_EXTRACTION_REQUIRED, "missing_field"],
        },
    },
]
ALLOWED_CALLS = frozenset({
    "log_cardio_from_image", "request_cardio_clarification",
})


def _instruction(caption):
    caption_text = str(caption or "").strip() or "(no caption)"
    return (
        "Analyze the attached fitness screenshot visually. Transcribe only values "
        "visible in the image; never estimate hidden values. The Telegram caption "
        f"is: {json.dumps(caption_text, ensure_ascii=False)}. Interpret relative "
        "dates in the caption as date_ref, not as image content. RACE WALKING is "
        "a walking/cardio activity. Duration strings must remain H:MM:SS or MM:SS. "
        "Extract every visible heart-rate-zone duration from Zone 0 through Zone 5, "
        "including explicit zero durations. Optional absent/obscured metrics use "
        "null with honest confidence. Select exactly one declared function."
    )


def _readability_detail(image_bytes):
    """Create a detail view from caller-sanitized image bytes."""
    try:
        with Image.open(BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            background = Image.new("RGB", image.size, image.getpixel(
                (image.width - 1, max(0, image.height // 2))
            ))
            difference = ImageChops.difference(image, background).convert("L")
            mask = difference.point(lambda value: 255 if value >= 10 else 0)
            bbox = mask.getbbox()
            if bbox:
                left, top, right, bottom = bbox
                pad = 8
                image = image.crop((
                    max(0, left - pad), max(0, top - pad),
                    min(image.width, right + pad), min(image.height, bottom + pad),
                ))
            longest = max(image.size)
            if longest < 2400:
                scale = min(4.0, 2400 / max(1, longest))
                image = image.resize(
                    (round(image.width * scale), round(image.height * scale)),
                    Image.Resampling.LANCZOS,
                )
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except Exception:
        return None


def _function_call(body):
    if not isinstance(body, dict):
        raise ValueError("empty_body")
    feedback = body.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise VisionProviderError("safety")
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("no_candidates")
    first = candidates[0] or {}
    if first.get("finishReason") in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"}:
        raise VisionProviderError("safety")
    calls = [
        part["functionCall"]
        for part in ((first.get("content") or {}).get("parts") or [])
        if isinstance(part, dict) and isinstance(part.get("functionCall"), dict)
    ]
    if len(calls) != 1:
        raise ValueError("expected_one_function_call")
    call = calls[0]
    if call.get("name") not in ALLOWED_CALLS or not isinstance(call.get("args"), dict):
        raise ValueError("invalid_function_call")
    return call["name"], call["args"]


def extract(image_bytes, mime_type, caption, *, api_key, models,
            request_post=None, timeout=45):
    """Use caller-sanitized bytes with one bounded provider fallback."""
    relay_mode = __import__("os").environ.get("GEMINI_TRANSPORT", "direct").strip().lower() == "relay"
    # Resolve at call time so callers can substitute the HTTP boundary without
    # changing production transport behavior.
    request_post = request_post or requests.post
    if not api_key and not relay_mode:
        raise VisionProviderError("unavailable")
    if not isinstance(image_bytes, bytes) or not image_bytes:
        raise cardio_extraction.CardioExtractionError("image_decode")
    if mime_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        raise cardio_extraction.CardioExtractionError("image_mime")
    unique_models = []
    for model in models:
        if model and model not in unique_models:
            unique_models.append(model)
    if not unique_models:
        raise VisionProviderError("unavailable")
    if relay_mode:
        started = time.monotonic()
        def validate_relay_result(result):
            try:
                name, arguments = result["name"], result["args"]
            except (KeyError, TypeError) as exc:
                raise gemini_transport.RelayTransportError(
                    "malformed_provider_result"
                ) from exc
            if name not in ALLOWED_CALLS or not isinstance(arguments, dict):
                # Never retry an undeclared tool: it is a safe rejection and
                # must not be allowed to progress toward a DB write.
                raise gemini_transport.RelayTransportError("unknown_tool_call")
            try:
                parsed = cardio_extraction.validate_vision_call(name, arguments)
            except cardio_extraction.CardioExtractionError as exc:
                category = (
                    "malformed_provider_result"
                    if str(exc) in {"malformed", "confidence", "bad_date"}
                    else "business_validation"
                )
                raise gemini_transport.RelayTransportError(category) from exc
            return name, arguments, parsed
        try:
            result, relay_meta = gemini_transport.relay_call_with_fallback(
                text=_instruction(caption), models=unique_models,
                image_bytes=image_bytes, mime_type=mime_type,
                function_declarations=FUNCTION_DECLARATIONS,
                allowed_function_names=ALLOWED_CALLS, timeout=timeout,
                request_post=request_post, result_validator=validate_relay_result,
            )
            name, arguments, parsed = result
        except gemini_transport.RelayTransportError as exc:
            category = "unavailable" if exc.fallback_eligible else "rejected"
            raised = VisionProviderError(category,
                                      model=unique_models[min(len(exc.attempts), len(unique_models)) - 1],
                                      attempts=len(exc.attempts),
                                      latency_ms=int((time.monotonic() - started) * 1000),
                                      status_code=exc.status_code)
            raised.relay_attempts = exc.attempts
            raise raised from exc
        canonical = json.dumps({"name": name, "args": arguments}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return parsed, {"model": relay_meta["final_model"], "attempt_count": relay_meta["attempt_count"], "latency_ms": relay_meta["latency_ms"],
                        "response_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                        "prompt_version": PROMPT_VERSION, "input_mime": mime_type,
                        "input_bytes": len(image_bytes), "call_name": name,
                        "relay": relay_meta}
    parts = [
        {"text": _instruction(caption)},
        {"inlineData": {
            "mimeType": mime_type,
            "data": base64.b64encode(image_bytes).decode("ascii"),
        }},
    ]
    detail = _readability_detail(image_bytes)
    if detail:
        parts.extend([
            {"text": (
                "This is a lossless enlarged detail view of the same attachment. "
                "Use it only to disambiguate small printed digits."
            )},
            {"inlineData": {
                "mimeType": "image/png",
                "data": base64.b64encode(detail).decode("ascii"),
            }},
        ])
    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "tools": [{"functionDeclarations": FUNCTION_DECLARATIONS}],
        "toolConfig": {"functionCallingConfig": {
            "mode": "ANY", "allowedFunctionNames": sorted(ALLOWED_CALLS),
        }},
        "generationConfig": {"temperature": 0},
    }
    started = time.monotonic()
    errors = []
    rejected_status = None
    saw_malformed = False
    for attempt, model in enumerate(unique_models[:2], start=1):
        try:
            response = request_post(
                API_URL.format(model=model), json=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
                timeout=timeout,
            )
        except requests.exceptions.RequestException as exc:
            errors.append(f"{model}:transport:{type(exc).__name__}")
            continue
        if response.status_code in PERMANENT_STATUS:
            rejected_status = response.status_code
            errors.append(f"{model}:http_{response.status_code}")
            continue
        if response.status_code in TRANSIENT_STATUS or response.status_code != 200:
            errors.append(f"{model}:http_{response.status_code}")
            continue
        try:
            name, arguments = _function_call(response.json())
            parsed = cardio_extraction.validate_vision_call(name, arguments)
        except VisionProviderError as exc:
            exc.model = model
            exc.attempt_count = attempt
            exc.latency_ms = int((time.monotonic() - started) * 1000)
            raise
        except (TypeError, ValueError, cardio_extraction.CardioExtractionError) as exc:
            errors.append(f"{model}:malformed:{type(exc).__name__}")
            saw_malformed = True
            continue
        canonical = json.dumps(
            {"name": name, "args": arguments},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return parsed, {
            "model": model,
            "attempt_count": attempt,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "response_sha256": hashlib.sha256(
                canonical.encode("utf-8")
            ).hexdigest(),
            "prompt_version": PROMPT_VERSION,
            "input_mime": mime_type,
            "input_bytes": len(image_bytes),
            "call_name": name,
        }
    raise VisionProviderError(
        # A successful HTTP exchange containing an unknown, multiple, or
        # malformed function call is a rejected model result, not a transport
        # outage.  This keeps provider failures distinct from extraction data.
        "rejected" if rejected_status or saw_malformed else "unavailable",
        model=unique_models[min(len(unique_models), 2) - 1],
        attempts=min(len(unique_models), 2),
        latency_ms=int((time.monotonic() - started) * 1000),
        status_code=rejected_status,
    )
