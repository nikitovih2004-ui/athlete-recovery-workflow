"""Bounded REST client for the conversation router.

Responsibilities kept deliberately narrow:
  * send the *system instruction* and the *untrusted user text* as separate parts;
  * request structured JSON output (responseMimeType) so the model is nudged,
    but the independent Python validator remains the real gate;
  * enforce a shared wall-clock deadline: primary model + at most one fallback;
  * classify failures into transient (retry the fallback) vs permanent (fail
    closed immediately) so a 400/401/403/safety error is never retried blindly.

No secrets, DB, filesystem or shell access. The transport is injectable so tests
run without network calls.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import gemini_transport

try:
    import requests
except Exception:  # pragma: no cover - requests is a hard dependency in prod
    requests = None


API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# HTTP statuses worth retrying on the fallback model.
TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
# Statuses that are the caller's / config's fault — never retry blindly.
PERMANENT_STATUS = frozenset({400, 401, 403, 404, 413})

# Timeout budget policy: the primary model gets a capped slice of the total
# deadline so it can never starve the fallback. Whatever's left after the
# primary attempt goes to the fallback, but only if that remainder still
# meets a guaranteed minimum — otherwise the fallback call would be started
# with almost no chance of finishing, so it's skipped in favor of an honest
# classified outage instead of a second doomed timeout.
DEFAULT_DEADLINE_S = 35.0
DEFAULT_PRIMARY_TIMEOUT_S = 18.0
DEFAULT_FALLBACK_MIN_TIMEOUT_S = 13.0


def _allocate_call_timeout(model_index, elapsed_s, deadline_s, primary_timeout_s,
                            fallback_min_timeout_s):
    """Return the timeout (seconds) for this model attempt, or None to skip it.

    model_index 0 is the primary model and is capped at primary_timeout_s so
    it can never consume the whole deadline. Every later attempt (fallback)
    gets whatever remains of the deadline, but only if that remainder is at
    least fallback_min_timeout_s - otherwise there isn't enough time left for
    a meaningful call and the attempt is skipped rather than made anyway.
    """
    remaining = deadline_s - elapsed_s
    if remaining <= 0:
        return None
    if model_index == 0:
        return min(primary_timeout_s, remaining)
    if remaining < fallback_min_timeout_s:
        return None
    return remaining


class GeminiError(Exception):
    """Base class. `category` is 'transient' | 'permanent' | 'safety'."""

    category = "permanent"

    def __init__(self, message, detail=None, *, model=None, attempt_count=0,
                 latency_ms=None):
        super().__init__(message)
        self.detail = detail
        self.model = model
        self.attempt_count = attempt_count
        self.latency_ms = latency_ms
        self.relay_attempts = ()


class GeminiUnavailable(GeminiError):
    category = "transient"


class GeminiRejected(GeminiError):
    category = "permanent"


class GeminiSafetyBlock(GeminiError):
    category = "safety"


@dataclass
class HttpResponse:
    status_code: int
    json_body: Optional[dict]


@dataclass
class GeminiResult:
    text: str
    model: str
    latency_ms: int
    attempt_count: int


@dataclass
class GeminiToolCallResult:
    name: str
    args: dict
    canonical_call: str
    model: str
    latency_ms: int
    attempt_count: int
    relay_metadata: Optional[dict] = None


class _TransientTransport(Exception):
    """Raised by a transport for connect/read timeouts and network errors."""


def _requests_transport(model, payload, timeout, api_key):
    if requests is None:  # pragma: no cover
        raise _TransientTransport("requests not installed")
    url = API_URL.format(model=model)
    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:  # timeout/connection/etc.
        raise _TransientTransport(f"{type(exc).__name__}") from exc
    try:
        body = resp.json()
    except ValueError:
        body = None
    return HttpResponse(resp.status_code, body)


def _extract_text(body):
    """Pull the first candidate's text, or raise a classified error."""
    if not isinstance(body, dict):
        raise GeminiRejected("empty response body")
    # Safety / policy blocks surface as promptFeedback or a non-STOP finishReason.
    feedback = body.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise GeminiSafetyBlock("prompt blocked", feedback.get("blockReason"))
    candidates = body.get("candidates")
    if not candidates:
        raise GeminiRejected("no candidates")
    first = candidates[0] or {}
    finish = first.get("finishReason")
    if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
        raise GeminiSafetyBlock("candidate blocked", finish)
    parts = (first.get("content") or {}).get("parts") or []
    texts = [p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p]
    joined = "".join(texts).strip()
    if not joined:
        raise GeminiRejected("no text in candidate", finish)
    return joined


def _extract_function_call(body, allowed_names):
    """Return one native function call; prose/multiple/unknown calls are invalid."""
    if not isinstance(body, dict):
        raise ValueError("empty_response")
    feedback = body.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise GeminiSafetyBlock("prompt blocked", feedback.get("blockReason"))
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("no_candidates")
    first = candidates[0] or {}
    finish = first.get("finishReason")
    if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
        raise GeminiSafetyBlock("candidate blocked", finish)
    parts = (first.get("content") or {}).get("parts") or []
    calls = [
        part.get("functionCall")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("functionCall"), dict)
    ]
    if len(calls) != 1:
        raise ValueError("expected_one_function_call")
    call = calls[0]
    name = call.get("name")
    args = call.get("args")
    if name not in allowed_names:
        raise ValueError("unknown_function")
    if not isinstance(args, dict):
        raise ValueError("function_args_not_object")
    return name, args


# JSON schema handed to the API. It nudges the model; the Python validator is the
# authoritative gate and re-checks everything regardless.
#
# `arguments` used to be a bare `{"type": "object"}` with no field-level
# enforcement, which let a model omit `fact_status` entirely - the actual
# production incident this schema exists to prevent: a real, correctly
# classified strength workout with `fact_status` silently missing, hard
# rejected with no way to recover it. Every field used by any intent is
# declared here so `fact_status` can be made a required enum; Python's own
# validators (conversation_validation.py) remain the sole authority on
# semantics/limits - this schema only nudges shape, never trusts it.
#
# `additionalProperties: false` is deliberately NOT set: Gemini's structured
# -output support for that keyword can't be verified without a live API call
# (out of scope here), and a rejected schema would break every request, which
# is worse than the status quo. Unknown keys stay guarded exactly as before,
# by conversation_validation.py's own per-intent allowlists.
FACT_STATUS_ENUM = ["completed", "current", "planned", "unknown", "not_applicable"]

_ARGUMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "fact_status": {"type": "string", "enum": FACT_STATUS_ENUM},
        "date_ref": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "enum": ["today", "yesterday", "absolute", "unspecified", "ambiguous"]},
                "value": {"type": "string", "nullable": True},
            },
        },
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "exercise_name": {"type": "string"},
                    "weight_kg": {"type": "number", "nullable": True},
                    "sets": {"type": "number", "nullable": True},
                    "reps": {"type": "number", "nullable": True},
                },
            },
        },
        "activity_type": {"type": "string", "nullable": True},
        "start_time": {"type": "string", "nullable": True},
        "duration_minutes": {"type": "number", "nullable": True},
        "distance_km": {"type": "number", "nullable": True},
        "avg_hr_bpm": {"type": "number", "nullable": True},
        "calories_kcal": {"type": "number", "nullable": True},
        "strain": {"type": "number", "nullable": True},
        "max_hr_bpm": {"type": "number", "nullable": True},
        "steps": {"type": "integer", "nullable": True},
        "hr_zone_minutes": {"type": "array", "items": {"type": "number"}, "nullable": True},
        "time": {"type": "string", "nullable": True},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "dose_text": {"type": "string", "nullable": True},
                    "taken": {"type": "boolean", "nullable": True},
                },
            },
        },
        "notes": {"type": "string", "nullable": True},
        "period": {"type": "string", "nullable": True},
        "metric": {"type": "string", "nullable": True},
        "window_days": {"type": "integer", "nullable": True},
        "factor_type": {"type": "string", "nullable": True},
        "factor_key": {"type": "string", "nullable": True},
        "candidate_intent": {"type": "string", "nullable": True},
        "missing_fields": {"type": "array", "items": {"type": "string"}, "nullable": True},
        "question": {"type": "string", "nullable": True},
        "topic": {"type": "string", "nullable": True},
        "reason": {"type": "string", "nullable": True},
    },
    "required": ["fact_status"],
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string"},
        "intent": {"type": "string"},
        "confidence": {"type": "number"},
        "requires_confirmation": {"type": "boolean"},
        "arguments": _ARGUMENTS_SCHEMA,
        "reply_text": {"type": "string", "nullable": True},
    },
    "required": [
        "schema_version", "intent", "confidence",
        "requires_confirmation", "arguments", "reply_text",
    ],
}


class GeminiClient:
    def __init__(self, api_key, model, fallback_models=None, transport=None,
                 deadline_s=DEFAULT_DEADLINE_S,
                 primary_timeout_s=DEFAULT_PRIMARY_TIMEOUT_S,
                 fallback_min_timeout_s=DEFAULT_FALLBACK_MIN_TIMEOUT_S,
                 clock=time.monotonic, response_schema=None,
                 relay_request_post=None):
        self.api_key = api_key
        self.model = model
        self.fallback_models = list(fallback_models or [])
        self._transport = transport
        self.deadline_s = deadline_s
        self.primary_timeout_s = primary_timeout_s
        self.fallback_min_timeout_s = fallback_min_timeout_s
        self._clock = clock
        self.response_schema = response_schema or RESPONSE_SCHEMA
        self._relay_request_post = relay_request_post

    def _models(self):
        """Primary first, then at most one fallback, de-duplicated."""
        seen = set()
        ordered = []
        for candidate in [self.model, *self.fallback_models]:
            if candidate and candidate not in seen:
                seen.add(candidate)
                ordered.append(candidate)
        # Contract: primary + maximum one fallback.
        return ordered[:2]

    def _uses_relay(self):
        return self._transport is None and os.environ.get("GEMINI_TRANSPORT", "direct").strip().lower() == "relay"

    def _call(self, model, payload, timeout):
        if self._transport is not None:
            return self._transport(model, payload, timeout)
        return _requests_transport(model, payload, timeout, self.api_key)

    def generate(self, system_instruction, user_text, temperature=0.1):
        """Return a GeminiResult or raise a classified GeminiError.

        The system instruction and the untrusted user text are separate content
        blocks; the user text is never concatenated into the instruction.
        """
        if not self.api_key:
            raise GeminiUnavailable("missing GEMINI_API_KEY")
        payload = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
                "responseSchema": self.response_schema,
            },
        }
        start = self._clock()
        attempt = 0
        errors = []
        for index, model in enumerate(self._models()):
            elapsed = self._clock() - start
            timeout = _allocate_call_timeout(
                index, elapsed, self.deadline_s,
                self.primary_timeout_s, self.fallback_min_timeout_s,
            )
            if timeout is None:
                if index == 0:
                    break
                errors.append(
                    f"{model}: skipped (only {self.deadline_s - elapsed:.1f}s left, "
                    f"need >= {self.fallback_min_timeout_s:.1f}s)"
                )
                continue
            attempt += 1
            try:
                resp = self._call(model, payload, timeout)
            except _TransientTransport as exc:
                errors.append(f"{model}: transport {exc}")
                continue
            except GeminiError:
                raise
            status = resp.status_code
            if status in PERMANENT_STATUS:
                # Config / auth / request error — do not retry blindly.
                raise GeminiRejected(
                    f"HTTP {status}", f"http_{status}",
                    model=model, attempt_count=attempt,
                    latency_ms=int((self._clock() - start) * 1000),
                )
            if status in TRANSIENT_STATUS:
                errors.append(f"{model}: HTTP {status}")
                continue
            if status != 200:
                errors.append(f"{model}: HTTP {status}")
                continue
            try:
                text = _extract_text(resp.json_body)  # may raise safety/permanent
            except GeminiError as exc:
                exc.model = model
                exc.attempt_count = attempt
                exc.latency_ms = int((self._clock() - start) * 1000)
                raise
            latency_ms = int((self._clock() - start) * 1000)
            return GeminiResult(text=text, model=model, latency_ms=latency_ms,
                                attempt_count=attempt)
        raise GeminiUnavailable(
            "router unavailable", "; ".join(errors) or "deadline exceeded",
            model=self._models()[min(max(attempt - 1, 0), len(self._models()) - 1)]
            if self._models() else None,
            attempt_count=attempt,
            latency_ms=int((self._clock() - start) * 1000),
        )

    def generate_tool_call(self, system_instruction, user_text,
                           function_declarations, *, allowed_names,
                           temperature=0.1):
        """Return exactly one allowlisted native Gemini function call.

        Malformed/unknown calls are model-output failures and may use the single
        fallback model. HTTP auth/config and safety failures remain fail-closed.
        """
        allowed = frozenset(allowed_names)
        declared = [item.get("name") if isinstance(item, dict) else None for item in function_declarations]
        if not allowed or len(declared) != len(set(declared)) or frozenset(declared) != allowed:
            raise GeminiRejected("invalid local tool declarations")
        if self._uses_relay():
            models = self._models()
            if not models:
                raise GeminiUnavailable("missing model")
            def validate_relay_result(result):
                try:
                    name, args = result["name"], result["args"]
                except (KeyError, TypeError) as exc:
                    raise gemini_transport.RelayTransportError(
                        "malformed_provider_result"
                    ) from exc
                # Unknown tools are a safe local rejection, not a reason to
                # try another model or reach a mutation path.
                if name not in allowed or not isinstance(args, dict):
                    raise gemini_transport.RelayTransportError("unknown_tool_call")
                return name, args
            try:
                result, relay_meta = gemini_transport.relay_call_with_fallback(
                    text=user_text, system_instruction=system_instruction,
                    models=models, function_declarations=function_declarations,
                    allowed_function_names=allowed, timeout=self.deadline_s,
                    result_validator=validate_relay_result,
                    request_post=self._relay_request_post,
                )
            except gemini_transport.RelayTransportError as exc:
                error = GeminiUnavailable if exc.fallback_eligible else GeminiRejected
                raised = error("relay transport failed", exc.category,
                               model=models[min(len(exc.attempts), len(models)) - 1],
                               attempt_count=len(exc.attempts))
                raised.relay_attempts = exc.attempts
                raise raised from exc
            name, args = result
            canonical = json.dumps({"name": name, "args": args}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return GeminiToolCallResult(name=name, args=args, canonical_call=canonical,
                                        model=relay_meta["final_model"],
                                        latency_ms=relay_meta["latency_ms"],
                                        attempt_count=relay_meta["attempt_count"],
                                        relay_metadata=relay_meta)
        if not self.api_key:
            raise GeminiUnavailable("missing GEMINI_API_KEY")
        payload = {
            "system_instruction": {"parts": [{"text": system_instruction}]},
            "contents": [{"role": "user", "parts": [{"text": user_text}]}],
            "tools": [{"functionDeclarations": function_declarations}],
            "toolConfig": {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": sorted(allowed),
                },
            },
            "generationConfig": {"temperature": temperature},
        }
        start = self._clock()
        attempt = 0
        errors = []
        saw_malformed = False
        saw_transient = False
        models = self._models()
        for index, model in enumerate(models):
            timeout = _allocate_call_timeout(
                index, self._clock() - start, self.deadline_s,
                self.primary_timeout_s, self.fallback_min_timeout_s,
            )
            if timeout is None:
                errors.append(f"{model}: skipped")
                continue
            attempt += 1
            try:
                response = self._call(model, payload, timeout)
            except _TransientTransport as exc:
                saw_transient = True
                errors.append(f"{model}: transport {exc}")
                continue
            status = response.status_code
            if status in PERMANENT_STATUS:
                raise GeminiRejected(
                    f"HTTP {status}", f"http_{status}", model=model,
                    attempt_count=attempt,
                    latency_ms=int((self._clock() - start) * 1000),
                )
            if status in TRANSIENT_STATUS or status != 200:
                saw_transient = True
                errors.append(f"{model}: HTTP {status}")
                continue
            try:
                name, args = _extract_function_call(response.json_body, allowed)
            except GeminiSafetyBlock as exc:
                exc.model = model
                exc.attempt_count = attempt
                exc.latency_ms = int((self._clock() - start) * 1000)
                raise
            except (TypeError, ValueError) as exc:
                saw_malformed = True
                errors.append(f"{model}: malformed_tool_call:{exc}")
                continue
            canonical = json.dumps(
                {"name": name, "args": args},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            return GeminiToolCallResult(
                name=name, args=args, canonical_call=canonical, model=model,
                latency_ms=int((self._clock() - start) * 1000),
                attempt_count=attempt,
            )
        error_type = GeminiRejected if saw_malformed and not saw_transient else GeminiUnavailable
        raise error_type(
            "agent unavailable", "; ".join(errors) or "deadline exceeded",
            model=models[min(max(attempt - 1, 0), len(models) - 1)]
            if models else None,
            attempt_count=attempt,
            latency_ms=int((self._clock() - start) * 1000),
        )
