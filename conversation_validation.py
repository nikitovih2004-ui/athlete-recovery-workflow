"""Strict, deterministic validation of the Gemini router envelope.

This layer validates *actions*, not the model's prose. It never trusts the
model's confidence to bypass schema or semantic checks, never coerces types, and
resolves every mutation date server-side against Europe/Kyiv. Any failure before
the tool transaction means zero domain writes.

Pipeline:
    validate_input(text)        -> guards raw user text length / control chars
    parse_router_json(raw)      -> strict JSON, no fences, no dup keys, object root
    validate_envelope(obj)      -> exact keys, types, schema_version, known intent
    validate(...)               -> full semantic + date validation -> ValidationResult
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass, field
from typing import Any, Optional

import conversation_contract as C


@dataclass
class ValidationResult:
    ok: bool
    intent: Optional[str] = None
    confidence: Optional[float] = None
    requires_confirmation: bool = False
    reply_text: Optional[str] = None
    tool: Optional[str] = None
    arguments: dict = field(default_factory=dict)
    resolved_date: Optional[str] = None
    clarification: Optional[dict] = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None

    @classmethod
    def reject(cls, code, detail=None, intent=None, confidence=None):
        return cls(ok=False, error_code=code, error_detail=detail,
                   intent=intent, confidence=confidence)


class RouterParseError(Exception):
    def __init__(self, code, detail=None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------- #
# Raw input guard
# --------------------------------------------------------------------------- #

def _has_control_chars(text):
    for ch in text:
        # Allow tab / newline / carriage return; reject NUL and other C0/C1.
        if ch in ("\t", "\n", "\r"):
            continue
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            return True
    return False


def validate_input(text):
    """Guard the raw user message before it ever reaches Gemini."""
    if not isinstance(text, str):
        return ValidationResult.reject(C.ERR_BAD_TYPE, "input is not a string")
    if _has_control_chars(text):
        return ValidationResult.reject(C.ERR_INPUT_CONTROL_CHARS)
    n = len(text)
    if n < C.MIN_INPUT_CHARS or not text.strip():
        return ValidationResult.reject(C.ERR_INPUT_EMPTY)
    if n > C.MAX_INPUT_CHARS:
        return ValidationResult.reject(C.ERR_INPUT_TOO_LONG, f"{n} > {C.MAX_INPUT_CHARS}")
    return ValidationResult(ok=True)


# --------------------------------------------------------------------------- #
# Strict JSON parsing
# --------------------------------------------------------------------------- #

def _no_duplicate_keys(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise RouterParseError(C.ERR_DUPLICATE_JSON_KEYS, key)
        seen.add(key)
    return dict(pairs)


def parse_router_json(raw):
    """Parse the model output as strict JSON.

    Rejects markdown fences, surrounding prose, non-object roots and duplicate
    keys. Returns a plain dict on success; raises RouterParseError otherwise.
    """
    if not isinstance(raw, str):
        raise RouterParseError(C.ERR_MALFORMED_JSON, "response is not text")
    stripped = raw.strip()
    if not stripped:
        raise RouterParseError(C.ERR_MALFORMED_JSON, "empty response")
    # No fenced or prose-wrapped JSON: it must be a bare object.
    if not (stripped.startswith("{") and stripped.endswith("}")):
        raise RouterParseError(C.ERR_MALFORMED_JSON, "not a bare JSON object")
    try:
        obj = json.loads(stripped, object_pairs_hook=_no_duplicate_keys)
    except RouterParseError:
        raise
    except (ValueError, RecursionError) as exc:
        raise RouterParseError(C.ERR_MALFORMED_JSON, type(exc).__name__)
    if not isinstance(obj, dict):
        raise RouterParseError(C.ERR_NON_OBJECT_ROOT)
    return obj


# --------------------------------------------------------------------------- #
# Envelope validation
# --------------------------------------------------------------------------- #

def validate_envelope(obj):
    """Validate the shared envelope. Returns ValidationResult (ok=True carries
    normalized envelope fields but not yet the semantic arguments)."""
    keys = set(obj.keys())
    unknown = keys - C.ENVELOPE_KEYS
    if unknown:
        return ValidationResult.reject(C.ERR_UNKNOWN_KEYS, ",".join(sorted(unknown)))
    missing = C.ENVELOPE_KEYS - keys
    if missing:
        return ValidationResult.reject(C.ERR_MISSING_KEYS, ",".join(sorted(missing)))

    if obj["schema_version"] != C.SCHEMA_VERSION:
        return ValidationResult.reject(C.ERR_SCHEMA_VERSION, str(obj["schema_version"]))

    intent = obj["intent"]
    if not isinstance(intent, str) or intent not in C.ALLOWED_INTENTS:
        return ValidationResult.reject(C.ERR_UNKNOWN_INTENT, str(intent))

    confidence = obj["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return ValidationResult.reject(C.ERR_BAD_CONFIDENCE, "not a number", intent=intent)
    confidence = float(confidence)
    if not math.isfinite(confidence) or not (0.0 <= confidence <= 1.0):
        return ValidationResult.reject(C.ERR_BAD_CONFIDENCE, str(confidence), intent=intent)

    requires_confirmation = obj["requires_confirmation"]
    if not isinstance(requires_confirmation, bool):
        return ValidationResult.reject(C.ERR_BAD_TYPE, "requires_confirmation", intent=intent)

    arguments = obj["arguments"]
    if not isinstance(arguments, dict):
        return ValidationResult.reject(C.ERR_BAD_TYPE, "arguments must be object", intent=intent)

    reply_text = obj["reply_text"]
    if reply_text is not None:
        if not isinstance(reply_text, str):
            return ValidationResult.reject(C.ERR_BAD_TYPE, "reply_text", intent=intent)
        if len(reply_text) > C.MAX_REPLY_TEXT_CHARS:
            return ValidationResult.reject(C.ERR_STRING_TOO_LONG, "reply_text", intent=intent)

    return ValidationResult(
        ok=True,
        intent=intent,
        confidence=confidence,
        requires_confirmation=requires_confirmation,
        reply_text=reply_text,
        arguments=arguments,
    )


# --------------------------------------------------------------------------- #
# Typed field helpers (no implicit coercion)
# --------------------------------------------------------------------------- #

def _req_str(container, key, max_len, code=C.ERR_BAD_TYPE):
    v = container.get(key)
    if not isinstance(v, str):
        raise RouterParseError(code, key)
    v = v.strip()
    if not v:
        raise RouterParseError(C.ERR_EMPTY_LIST, key)
    if len(v) > max_len:
        raise RouterParseError(C.ERR_STRING_TOO_LONG, key)
    if _has_control_chars(v):
        raise RouterParseError(C.ERR_INPUT_CONTROL_CHARS, key)
    return v


def _opt_number(container, key, max_value, min_value=0):
    v = container.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RouterParseError(C.ERR_BAD_TYPE, key)
    v = float(v)
    if not math.isfinite(v):
        raise RouterParseError(C.ERR_NON_FINITE_NUMBER, key)
    if v < min_value or v > max_value:
        raise RouterParseError(C.ERR_VALUE_OUT_OF_RANGE, key)
    return v


def _req_number(container, key, max_value, min_value=0):
    if container.get(key) is None:
        raise RouterParseError(C.ERR_BAD_TYPE, f"{key} required")
    return _opt_number(container, key, max_value, min_value)


def _opt_str(container, key, max_len):
    v = container.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise RouterParseError(C.ERR_BAD_TYPE, key)
    v = v.strip()
    if not v:
        return None
    if len(v) > max_len:
        raise RouterParseError(C.ERR_STRING_TOO_LONG, key)
    if _has_control_chars(v):
        raise RouterParseError(C.ERR_INPUT_CONTROL_CHARS, key)
    return v


# --------------------------------------------------------------------------- #
# Date resolution (server-side, Europe/Kyiv)
# --------------------------------------------------------------------------- #

def _validate_date_ref(arguments):
    ref = arguments.get("date_ref")
    if not isinstance(ref, dict):
        raise RouterParseError(C.ERR_BAD_TYPE, "date_ref")
    if set(ref.keys()) - {"kind", "value"}:
        raise RouterParseError(C.ERR_UNKNOWN_KEYS, "date_ref")
    kind = ref.get("kind")
    if kind not in C.DATE_KINDS:
        raise RouterParseError(C.ERR_BAD_DATE, f"kind={kind}")
    value = ref.get("value")
    if kind == C.DATE_KIND_ABSOLUTE:
        if not isinstance(value, str):
            raise RouterParseError(C.ERR_BAD_DATE, "absolute value not string")
    elif value is not None:
        raise RouterParseError(C.ERR_BAD_DATE, "value must be null unless absolute")
    return kind, value


def _bounded_iso(iso, local_today):
    try:
        d = dt.date.fromisoformat(iso)
    except (ValueError, TypeError):
        return None, C.ERR_BAD_DATE
    if d > local_today:
        return None, C.ERR_FUTURE_DATE
    if (local_today - d).days > C.MAX_PAST_DAYS:
        return None, C.ERR_DATE_TOO_OLD
    return d.isoformat(), None


def resolve_mutation_date(intent, arguments, local_now, reply_evening_date=None):
    """Resolve a mutation's calendar date server-side.

    Returns (iso_date, error_code). error_code is None on success.
    Priority: exact morning reply -> absolute -> today/yesterday -> intent
    default -> clarification. Never returns a future or too-old date.
    """
    local_today = local_now.date()
    yesterday = (local_today - dt.timedelta(days=1)).isoformat()
    today = local_today.isoformat()

    # 1. Exact morning reply wins outright (only meaningful for daily context).
    if reply_evening_date:
        return _bounded_iso(reply_evening_date, local_today)

    kind, value = _validate_date_ref(arguments)

    if kind == C.DATE_KIND_AMBIGUOUS:
        return None, C.ERR_AMBIGUOUS_DATE
    if kind == C.DATE_KIND_ABSOLUTE:
        return _bounded_iso(value, local_today)
    if kind == C.DATE_KIND_TODAY:
        return today, None
    if kind == C.DATE_KIND_YESTERDAY:
        return yesterday, None

    # kind == unspecified -> intent-specific default
    if intent in (C.INTENT_LOG_STRENGTH, C.INTENT_LOG_CARDIO):
        return (yesterday if local_now.hour < 5 else today), None
    if intent == C.INTENT_LOG_SUPPLEMENT:
        return (yesterday if local_now.hour < 13 else today), None
    if intent == C.INTENT_SAVE_DAILY_CONTEXT:
        # Daily context needs an explicit anchor when it is not a direct reply.
        return None, C.ERR_MISSING_CONTEXT_DATE
    return None, C.ERR_BAD_DATE


# --------------------------------------------------------------------------- #
# Fact-status guard
# --------------------------------------------------------------------------- #

def _classify_fact_status(arguments):
    """Classify a workout/cardio mutation's fact_status.

    Returns 'ok' (completed/current - proceed to validate the rest of the
    arguments), 'planned' (explicit plan - safe no-write with its own
    explanation), or 'ambiguous' (missing, unknown, not_applicable, or any
    other value - ask one yes/no clarifying question rather than guessing).
    """
    status = arguments.get("fact_status")
    if status in C.FACT_STATUSES_ACTIONABLE:
        return "ok"
    if status == C.FACT_STATUS_PLANNED:
        return "planned"
    return "ambiguous"


# --------------------------------------------------------------------------- #
# Per-intent semantic validation
# --------------------------------------------------------------------------- #

def _validate_strength(arguments):
    entries = arguments.get("entries")
    if not isinstance(entries, list):
        raise RouterParseError(C.ERR_BAD_TYPE, "entries")
    if not entries:
        raise RouterParseError(C.ERR_EMPTY_LIST, "entries")
    if len(entries) > C.MAX_WORKOUT_ENTRIES:
        raise RouterParseError(C.ERR_TOO_MANY_ITEMS, "entries")
    clean = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise RouterParseError(C.ERR_BAD_TYPE, "entry")
        if set(entry.keys()) - {"exercise_name", "weight_kg", "sets", "reps"}:
            raise RouterParseError(C.ERR_UNKNOWN_KEYS, "entry")
        name = _req_str(entry, "exercise_name", C.MAX_NAME_CHARS)
        weight = _opt_number(entry, "weight_kg", C.MAX_WEIGHT_KG)
        sets = _opt_number(entry, "sets", C.MAX_SETS, min_value=0)
        reps = _opt_number(entry, "reps", C.MAX_REPS, min_value=0)
        clean.append({
            "exercise_name": name,
            # Round away model float artifacts (e.g. 30.000000000000004 -> 30.0).
            "weight_kg": round(weight, 2) if weight is not None else None,
            "sets": int(sets) if sets is not None else None,
            "reps": int(reps) if reps is not None else None,
        })
    # A completed set with a weight but no reps yields volume 0 - the exact
    # signature of a model parse that dropped the reps. Refuse to store the
    # zero-volume garbage; the user is asked to resend with reps (a plain
    # reject, not a stateful clarification, so the resend is a fresh log).
    if any(e["weight_kg"] is not None and e["reps"] is None for e in clean):
        raise RouterParseError(C.ERR_STRENGTH_MISSING_REPS, "reps")
    return {"entries": clean}


def _validate_cardio(arguments):
    allowed = {
        "date_ref", "fact_status", "activity_type", "start_time",
        "duration_minutes", "distance_km", "avg_hr_bpm", "calories_kcal",
        "hr_zone_minutes", "strain", "max_hr_bpm", "steps",
    }
    if set(arguments.keys()) - allowed:
        raise RouterParseError(C.ERR_UNKNOWN_KEYS, "cardio")
    activity = _req_str(arguments, "activity_type", C.MAX_CARDIO_ACTIVITY_CHARS)
    duration = _req_number(arguments, "duration_minutes", C.MAX_DURATION_MINUTES, min_value=0)
    start_time = _opt_str(arguments, "start_time", 16)
    distance = _opt_number(arguments, "distance_km", C.MAX_DISTANCE_KM)
    avg_hr = _opt_number(arguments, "avg_hr_bpm", C.MAX_HR_BPM)
    calories = _opt_number(arguments, "calories_kcal", C.MAX_CALORIES)
    strain = _opt_number(arguments, "strain", 21)
    max_hr = _opt_number(arguments, "max_hr_bpm", C.MAX_HR_BPM)
    steps = _opt_number(arguments, "steps", 200000)

    zones = arguments.get("hr_zone_minutes")
    clean_zones = None
    if zones is not None:
        if not isinstance(zones, list):
            raise RouterParseError(C.ERR_BAD_TYPE, "hr_zone_minutes")
        if len(zones) > C.MAX_HR_ZONES:
            raise RouterParseError(C.ERR_TOO_MANY_ITEMS, "hr_zone_minutes")
        clean_zones = []
        for z in zones:
            if z is None:
                clean_zones.append(None)
                continue
            if isinstance(z, bool) or not isinstance(z, (int, float)):
                raise RouterParseError(C.ERR_BAD_TYPE, "hr_zone_minutes element")
            zf = float(z)
            if not math.isfinite(zf) or zf < 0 or zf > C.MAX_DURATION_MINUTES:
                raise RouterParseError(C.ERR_VALUE_OUT_OF_RANGE, "hr_zone_minutes element")
            clean_zones.append(zf)
    return {
        "activity_type": activity,
        "start_time": start_time,
        "duration_minutes": duration,
        "distance_km": distance,
        "avg_hr_bpm": int(avg_hr) if avg_hr is not None else None,
        "calories_kcal": calories,
        "strain": strain,
        "max_hr_bpm": int(max_hr) if max_hr is not None else None,
        "steps": int(steps) if steps is not None else None,
        "hr_zone_minutes": clean_zones,
    }


def _validate_supplement(arguments):
    # fact_status is always present (schema-required for every intent) but
    # not_applicable/ignored here - supplements use their own `taken` fact.
    allowed = {"date_ref", "time", "items", "fact_status"}
    if set(arguments.keys()) - allowed:
        raise RouterParseError(C.ERR_UNKNOWN_KEYS, "supplement")
    time_str = _opt_str(arguments, "time", 16)
    items = arguments.get("items")
    if not isinstance(items, list):
        raise RouterParseError(C.ERR_BAD_TYPE, "items")
    if not items:
        raise RouterParseError(C.ERR_EMPTY_LIST, "items")
    if len(items) > C.MAX_SUPPLEMENT_ITEMS:
        raise RouterParseError(C.ERR_TOO_MANY_ITEMS, "items")
    clean = []
    for item in items:
        if not isinstance(item, dict):
            raise RouterParseError(C.ERR_BAD_TYPE, "item")
        if set(item.keys()) - {"name", "dose_text", "taken"}:
            raise RouterParseError(C.ERR_UNKNOWN_KEYS, "item")
        name = _req_str(item, "name", C.MAX_NAME_CHARS)
        dose = _opt_str(item, "dose_text", C.MAX_DOSE_CHARS)
        taken = item.get("taken")
        # taken must be an explicit boolean fact. Unknown -> clarification.
        if taken is None:
            raise RouterParseError(C.ERR_UNKNOWN_SUPPLEMENT_STATUS, name)
        if not isinstance(taken, bool):
            raise RouterParseError(C.ERR_BAD_TYPE, "taken")
        clean.append({"name": name, "dose_text": dose, "taken": taken})
    return {"time": time_str, "items": clean}


def _validate_daily_context(arguments):
    # fact_status is always present (schema-required for every intent) but
    # not_applicable/ignored here.
    allowed = {"date_ref", "notes", "fact_status"}
    if set(arguments.keys()) - allowed:
        raise RouterParseError(C.ERR_UNKNOWN_KEYS, "daily_context")
    notes = _req_str(arguments, "notes", C.MAX_NOTES_CHARS)
    return {"notes": notes}


def _validate_metric_trend(arguments):
    allowed = {"fact_status", "metric", "window_days"}
    if set(arguments) - allowed:
        raise RouterParseError(C.ERR_UNKNOWN_KEYS, "metric_trend")
    metric = arguments.get("metric")
    if not isinstance(metric, str) or metric not in C.TREND_METRICS:
        raise RouterParseError(C.ERR_BAD_TYPE, "metric")
    days = arguments.get("window_days")
    if isinstance(days, bool) or not isinstance(days, int) or days not in C.TREND_WINDOWS_DAYS:
        raise RouterParseError(C.ERR_VALUE_OUT_OF_RANGE, "window_days")
    return {"metric": metric, "window_days": days}


def _validate_factor_observation(arguments):
    allowed = {"fact_status", "factor_type", "factor_key", "window_days"}
    if set(arguments) - allowed:
        raise RouterParseError(C.ERR_UNKNOWN_KEYS, "factor_observation")
    factor_type = arguments.get("factor_type")
    if factor_type not in C.FACTOR_TYPES:
        raise RouterParseError(C.ERR_BAD_TYPE, "factor_type")
    factor_key = _req_str(arguments, "factor_key", C.MAX_FACTOR_KEY_CHARS)
    if factor_type == "daily_factor" and factor_key not in C.DAILY_FACTOR_KEYS:
        raise RouterParseError(C.ERR_BAD_TYPE, "factor_key")
    days = arguments.get("window_days")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= C.MAX_ANALYTICS_LOOKBACK_DAYS:
        raise RouterParseError(C.ERR_VALUE_OUT_OF_RANGE, "window_days")
    return {"factor_type": factor_type, "factor_key": factor_key, "window_days": days}


def _validate_day_snapshot(arguments, local_now):
    if set(arguments) != {"date"}:
        raise RouterParseError(C.ERR_UNKNOWN_KEYS, "day_snapshot")
    value = arguments.get("date")
    bounded, error = _bounded_iso(value, local_now.date())
    if error:
        raise RouterParseError(error, "date")
    return {"date": bounded}


# --------------------------------------------------------------------------- #
# Top-level validate
# --------------------------------------------------------------------------- #

def _clarification_from_error(intent, code, detail):
    """Turn a semantic rejection that a clarifying question could fix into a
    structured clarification, else return None (hard reject)."""
    clarifiable = {
        C.ERR_AMBIGUOUS_DATE: "За какой день это записать?",
        C.ERR_MISSING_CONTEXT_DATE: "За какой день сохранить контекст?",
        C.ERR_UNKNOWN_SUPPLEMENT_STATUS: "Ты уже принял это или только планируешь?",
    }
    if code not in clarifiable:
        return None
    question = clarifiable[code]
    if question is None:
        return None
    return {
        "candidate_intent": intent,
        "missing_fields": [detail] if detail else [],
        "question": question,
    }


def _server_clarification(candidate_intent, missing_fields):
    """Map model classifications to server-authored clarification text only."""
    if candidate_intent not in C.MUTATION_INTENTS:
        return None
    if not isinstance(missing_fields, list) or any(
        not isinstance(field, str) for field in missing_fields
    ) or not missing_fields or len(missing_fields) > 4:
        return None
    missing = frozenset(missing_fields)
    allowed_by_intent = {
        C.INTENT_LOG_STRENGTH: frozenset({"fact_status", "date", "date_ref", "resolved_date"}),
        C.INTENT_LOG_CARDIO: frozenset({"fact_status", "date", "date_ref", "resolved_date"}),
        C.INTENT_LOG_SUPPLEMENT: frozenset({"taken", "fact_status", "date", "date_ref", "resolved_date"}),
        C.INTENT_SAVE_DAILY_CONTEXT: frozenset({"date", "date_ref", "resolved_date"}),
    }
    if not missing.issubset(allowed_by_intent[candidate_intent]):
        return None
    if candidate_intent in {C.INTENT_LOG_STRENGTH, C.INTENT_LOG_CARDIO} \
            and "fact_status" in missing:
        question = C.MSG_FACT_STATUS_CLARIFICATION
        canonical_missing = ["fact_status"]
    elif candidate_intent == C.INTENT_LOG_SUPPLEMENT \
            and missing.intersection({"taken", "fact_status"}):
        question = C.MSG_SUPPLEMENT_STATUS_CLARIFICATION
        canonical_missing = ["taken"]
    elif missing.intersection({"date", "date_ref", "resolved_date"}):
        question = (
            C.MSG_CONTEXT_DATE_CLARIFICATION
            if candidate_intent == C.INTENT_SAVE_DAILY_CONTEXT
            else C.MSG_DATE_CLARIFICATION
        )
        canonical_missing = ["date_ref"]
    else:
        return None
    return {
        "candidate_intent": candidate_intent,
        "missing_fields": canonical_missing,
        "question": question,
    }


def validate(envelope_result, *, local_now, reply_evening_date=None):
    """Full semantic + date validation on a validated envelope.

    `envelope_result` must be a successful validate_envelope() result.
    Returns a ValidationResult ready for the router: ok+tool for a mutation/read,
    ok+clarification/reply_text for non-actioning intents, or a hard reject.
    """
    intent = envelope_result.intent
    confidence = envelope_result.confidence
    arguments = envelope_result.arguments
    base = dict(
        intent=intent,
        confidence=confidence,
        requires_confirmation=envelope_result.requires_confirmation,
        reply_text=envelope_result.reply_text,
    )

    # Non-actioning intents can never open a transaction.
    if intent == C.INTENT_GENERAL_CONVERSATION:
        if confidence < C.confidence_threshold(intent):
            return ValidationResult.reject(C.ERR_LOW_CONFIDENCE, str(confidence), intent=intent, confidence=confidence)
        return ValidationResult(ok=True, tool=None, **base)
    if intent == C.INTENT_UNSUPPORTED_REQUEST:
        return ValidationResult(ok=True, tool=None, **base)
    if intent == C.INTENT_NEEDS_CLARIFICATION:
        candidate = arguments.get("candidate_intent")
        if candidate is not None and candidate not in C.ALLOWED_INTENTS:
            return ValidationResult.reject(C.ERR_UNKNOWN_INTENT, "candidate_intent", intent=intent)
        missing = arguments.get("missing_fields", [])
        clar = _server_clarification(candidate, missing)
        if clar is None:
            return ValidationResult.reject(
                C.ERR_BAD_TYPE, "unsupported_clarification", intent=intent,
                confidence=confidence,
            )
        return ValidationResult(ok=True, tool=None, clarification=clar, **base)

    # From here on: mutation or read intent. Enforce the confidence gate.
    if confidence < C.confidence_threshold(intent):
        return ValidationResult.reject(C.ERR_LOW_CONFIDENCE, str(confidence), intent=intent, confidence=confidence)

    tool = C.tool_for_intent(intent)
    if tool is None:
        return ValidationResult.reject(C.ERR_UNSUPPORTED_TOOL, intent, intent=intent)

    # Reads take no domain arguments beyond an optional period.
    if intent == C.INTENT_GET_TODAY_STATUS:
        return ValidationResult(ok=True, tool=tool, arguments={}, **base)
    if intent == C.INTENT_GET_WEEK_SUMMARY:
        period = arguments.get("period", "last_completed_week")
        if period != "last_completed_week":
            return ValidationResult.reject(C.ERR_BAD_TYPE, "period", intent=intent)
        return ValidationResult(ok=True, tool=tool, arguments={"period": period}, **base)
    if intent == C.INTENT_GET_METRIC_TREND:
        try:
            clean = _validate_metric_trend(arguments)
        except RouterParseError as exc:
            return ValidationResult.reject(exc.code, exc.detail, intent=intent,
                                           confidence=confidence)
        return ValidationResult(ok=True, tool=tool, arguments=clean, **base)
    if intent == C.INTENT_GET_FACTOR_OBSERVATION:
        try:
            clean = _validate_factor_observation(arguments)
        except RouterParseError as exc:
            return ValidationResult.reject(exc.code, exc.detail, intent=intent,
                                           confidence=confidence)
        return ValidationResult(ok=True, tool=tool, arguments=clean, **base)
    if intent == C.INTENT_GET_DAY_SNAPSHOT:
        try:
            clean = _validate_day_snapshot(arguments, local_now)
        except RouterParseError as exc:
            return ValidationResult.reject(
                exc.code, exc.detail, intent=intent, confidence=confidence
            )
        return ValidationResult(ok=True, tool=tool, arguments=clean, **base)
    if intent in {
        C.INTENT_GET_DATA_COVERAGE, C.INTENT_GET_SUPPLEMENT_RECORDS,
    }:
        if arguments:
            return ValidationResult.reject(
                C.ERR_UNKNOWN_KEYS, "read_arguments",
                intent=intent, confidence=confidence,
            )
        return ValidationResult(ok=True, tool=tool, arguments={}, **base)

    # Strength/cardio mutations must carry an explicit, actionable fact_status
    # before any per-intent argument validation runs. Python never assumes
    # "completed" - a plan gets its own explanation, anything else gets one
    # clarifying question instead of a silent reject (the production incident
    # this exists for: a real workout with fact_status simply missing).
    if intent in (C.INTENT_LOG_STRENGTH, C.INTENT_LOG_CARDIO):
        fact = _classify_fact_status(arguments)
        if fact == "planned":
            planned_base = {**base, "reply_text": C.MSG_PLANNED_WORKOUT}
            return ValidationResult(ok=True, tool=None, **planned_base)
        if fact == "ambiguous":
            clar = {
                "candidate_intent": intent,
                "missing_fields": ["fact_status"],
                "question": C.MSG_FACT_STATUS_CLARIFICATION,
            }
            return ValidationResult(ok=True, tool=None, clarification=clar,
                                    arguments=arguments, **base)

    # Mutations: date_ref is required and semantic validation runs.
    try:
        if intent == C.INTENT_LOG_STRENGTH:
            clean_args = _validate_strength(arguments)
        elif intent == C.INTENT_LOG_CARDIO:
            clean_args = _validate_cardio(arguments)
        elif intent == C.INTENT_LOG_SUPPLEMENT:
            clean_args = _validate_supplement(arguments)
        elif intent == C.INTENT_SAVE_DAILY_CONTEXT:
            clean_args = _validate_daily_context(arguments)
        else:  # pragma: no cover - guarded above
            return ValidationResult.reject(C.ERR_UNSUPPORTED_TOOL, intent, intent=intent)

        resolved_date, date_err = resolve_mutation_date(
            intent, arguments, local_now, reply_evening_date=reply_evening_date
        )
        if date_err:
            clar = _clarification_from_error(intent, date_err, None)
            if clar is not None:
                return ValidationResult(ok=True, tool=None, clarification=clar, **base)
            return ValidationResult.reject(date_err, None, intent=intent, confidence=confidence)
    except RouterParseError as exc:
        clar = _clarification_from_error(intent, exc.code, exc.detail)
        if clar is not None:
            return ValidationResult(ok=True, tool=None, clarification=clar,
                                    arguments=arguments, **base)
        return ValidationResult.reject(exc.code, exc.detail, intent=intent, confidence=confidence)

    clean_args["resolved_date"] = resolved_date
    return ValidationResult(ok=True, tool=tool, arguments=clean_args,
                            resolved_date=resolved_date, **base)
