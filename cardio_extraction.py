"""Typed, bounded normalization for screenshot cardio extraction."""
from __future__ import annotations

import json
import math
import re


class CardioExtractionError(ValueError):
    def __init__(self, code, partial=None):
        super().__init__(code)
        self.partial = partial or {}


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    text = str(value).strip().replace("\u00a0", "").replace(" ", "")
    if not text or text.casefold() in {"n/a", "na", "null", "none", "-", "—"}:
        return None
    # WHOOP thousands use commas; decimal commas are retained when there is
    # exactly one comma and at most two digits after it.
    if "," in text and "." not in text:
        left, right = text.rsplit(",", 1)
        text = f"{left}.{right}" if len(right) <= 2 else left + right
    else:
        text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        number = float(match.group(0))
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _duration_seconds(value):
    number = _number(value) if not isinstance(value, str) or ":" not in value else None
    if number is not None:
        return number
    text = str(value or "").strip()
    match = re.fullmatch(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    return (
        int(match.group(1) or 0) * 3600
        + int(match.group(2)) * 60
        + int(match.group(3))
    )


def _activity(value):
    normalized = re.sub(r"[_-]+", " ", str(value or "").casefold()).strip()
    if not normalized:
        return None
    aliases = (
        (("race walking", "walking", "walk", "ходьб", "прогул"), "walking"),
        (("running", "run", "бег"), "running"),
        (("cycling", "bike", "biking", "вело"), "cycling"),
        (("elliptical", "эллип"), "elliptical"),
        (("rowing", "греб"), "rowing"),
        (("cardio", "кардио"), "cardio"),
    )
    for markers, canonical in aliases:
        if any(marker in normalized for marker in markers):
            return canonical
    return None


def _confidence_value(field, *, required=False):
    if not isinstance(field, dict) or set(field) != {"value", "confidence"}:
        raise CardioExtractionError("malformed")
    confidence = _number(field.get("confidence"))
    if confidence is None or not 0 <= confidence <= 1:
        raise CardioExtractionError("confidence")
    if required and confidence < 0.90:
        return None, confidence
    return field.get("value"), confidence


def _zone_duration(value):
    if value is None:
        return None
    seconds = _duration_seconds(value)
    if seconds is None or not 0 <= seconds <= 86400:
        return None
    seconds = int(round(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (
        f"{hours}:{minutes:02d}:{seconds:02d}"
        if hours else f"{minutes}:{seconds:02d}"
    )


def validate_vision_call(call_name, arguments):
    """Validate a typed Gemini Vision call without trusting model prose."""
    if call_name not in {
        "log_cardio_from_image", "request_cardio_clarification",
    } or not isinstance(arguments, dict):
        raise CardioExtractionError("malformed")
    expected = {
        "activity_type", "duration", "strain", "avg_hr_bpm", "max_hr_bpm",
        "calories_kcal", "steps", "distance_km", "zone_0_duration",
        "zone_1_duration", "zone_2_duration", "zone_3_duration",
        "zone_4_duration", "zone_5_duration", "date_ref",
    }
    if call_name == "request_cardio_clarification":
        expected.add("missing_field")
    if set(arguments) != expected:
        raise CardioExtractionError("malformed")

    raw = {}
    confidences = {}
    for field in expected - {"date_ref", "missing_field"}:
        value, confidence = _confidence_value(
            arguments[field],
            required=field in {"activity_type", "duration"},
        )
        raw[field] = value
        confidences[field] = confidence

    date_ref = arguments["date_ref"]
    if not isinstance(date_ref, dict) or set(date_ref) != {
        "kind", "value", "confidence",
    }:
        raise CardioExtractionError("malformed")
    date_confidence = _number(date_ref.get("confidence"))
    if date_confidence is None or not 0 <= date_confidence <= 1:
        raise CardioExtractionError("confidence")
    if date_ref.get("kind") not in {
        "today", "yesterday", "absolute", "unspecified",
    }:
        raise CardioExtractionError("bad_date")

    activity = _activity(raw["activity_type"])
    duration_seconds = _duration_seconds(raw["duration"])
    parsed = {
        "type": activity,
        "duration": (
            round(duration_seconds / 60, 3)
            if duration_seconds is not None and 1 <= duration_seconds <= 86400
            else None
        ),
        "strain": None,
        "avg_hr": None,
        "max_hr": None,
        "calories": None,
        "steps": None,
        "distance": None,
        "date_ref": {
            "kind": date_ref["kind"], "value": date_ref.get("value"),
            "confidence": date_confidence,
        },
        "field_confidence": confidences,
        "call_name": call_name,
    }
    numeric = {
        "strain": ("strain", 0, 21, float),
        "avg_hr_bpm": ("avg_hr", 1, 300, int),
        "max_hr_bpm": ("max_hr", 1, 300, int),
        "calories_kcal": ("calories", 1, 100000, float),
        "steps": ("steps", 0, 200000, int),
        "distance_km": ("distance", 0, 1000, float),
    }
    for source, (target, minimum, maximum, caster) in numeric.items():
        value = _number(raw[source])
        if (
            value is not None and minimum <= value <= maximum
            and confidences[source] >= 0.90
        ):
            parsed[target] = caster(value)
    for index in range(6):
        field = f"zone_{index}_duration"
        parsed[f"zone_{index}"] = (
            _zone_duration(raw[field]) if confidences[field] >= 0.90 else None
        )

    effort_confidences = [
        confidences[field] for field, target in (
            ("avg_hr_bpm", "avg_hr"), ("calories_kcal", "calories"),
            ("strain", "strain"),
        ) if parsed[target] is not None
    ]
    missing = None
    if parsed["type"] is None or confidences["activity_type"] < 0.90:
        missing = "activity_type"
    elif parsed["duration"] is None or confidences["duration"] < 0.90:
        missing = "duration"
    elif not effort_confidences:
        missing = "effort_metric"
    parsed["confidence"] = min(
        [
            confidences["activity_type"], confidences["duration"],
            max(effort_confidences, default=0),
        ]
    )
    requested_missing = arguments.get("missing_field")
    if call_name == "request_cardio_clarification":
        if requested_missing not in {
            "activity_type", "duration", "effort_metric",
        }:
            raise CardioExtractionError("malformed")
        missing = missing or requested_missing
    elif missing:
        raise CardioExtractionError(missing, parsed)
    parsed["needs_clarification"] = missing
    return parsed


def validate(raw):
    if not isinstance(raw, dict):
        raise CardioExtractionError("malformed")
    clean_raw = dict(raw)
    activity = _activity(
        clean_raw.get("activity_type")
        or clean_raw.get("activity_label")
        or clean_raw.get("type")
    )
    duration = _duration_seconds(
        clean_raw.get("duration_seconds")
        if clean_raw.get("duration_seconds") is not None
        else clean_raw.get("duration")
    )
    partial = {
        "activity_type": activity,
        "duration_seconds": duration,
        "strain": _number(clean_raw.get("strain")),
        "avg_hr_bpm": _number(
            clean_raw.get("avg_hr_bpm") or clean_raw.get("average_hr")
        ),
        "max_hr_bpm": _number(clean_raw.get("max_hr_bpm")),
        "calories_kcal": _number(
            clean_raw.get("calories_kcal") or clean_raw.get("calories")
        ),
        "distance_km": _number(clean_raw.get("distance_km")),
        "steps": _number(clean_raw.get("steps")),
        "activity_confidence": _number(clean_raw.get("activity_confidence")),
        "duration_confidence": _number(clean_raw.get("duration_confidence")),
        "effort_confidence": _number(clean_raw.get("effort_confidence")),
    }
    if activity is None:
        raise CardioExtractionError("activity_type", partial)
    if duration is None or not 1 <= duration <= 86400:
        raise CardioExtractionError("duration", partial)
    confidence_fields = (
        "activity_confidence", "duration_confidence", "effort_confidence"
    )
    for field in confidence_fields:
        value = partial[field]
        if value is None or not 0 <= value <= 1:
            raise CardioExtractionError("confidence", partial)
    if partial["activity_confidence"] < 0.90:
        raise CardioExtractionError("activity_type", partial)
    if partial["duration_confidence"] < 0.90:
        raise CardioExtractionError("duration", partial)
    if partial["effort_confidence"] < 0.90:
        raise CardioExtractionError("effort_metric", partial)
    out = {
        "type": activity,
        "duration": round(float(duration) / 60, 3),
        "strain": None,
        "max_hr": None,
        "steps": None,
        "confidence": min(partial[field] for field in confidence_fields),
    }
    bounds = {
        "avg_hr_bpm": ("avg_hr", 1, 300),
        "calories_kcal": ("calories", 1, 100000),
        "distance_km": ("distance", 0, 1000),
    }
    for source, (target, minimum, maximum) in bounds.items():
        value = partial[source]
        if value is not None and minimum <= value <= maximum:
            out[target] = float(value)
        else:
            partial[source] = None
    optionals = {
        "strain": ("strain", 0, 21, float),
        "max_hr_bpm": ("max_hr", 1, 300, int),
        "steps": ("steps", 0, 200000, int),
    }
    for source, (target, minimum, maximum, caster) in optionals.items():
        value = partial[source]
        if value is not None and minimum <= value <= maximum:
            out[target] = caster(value)
        else:
            partial[source] = None
    if (
        out.get("avg_hr") is None and out.get("calories") is None
        and out.get("strain") is None
    ):
        raise CardioExtractionError("effort_metric", partial)
    return out


def parse_json(text):
    try:
        raw = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CardioExtractionError("malformed") from exc
    return validate(raw)
