"""Shared, privacy-safe relay transport with one bounded fallback attempt.

Fallback is deliberately limited to provider/transport failures: timeout,
unavailable transport, rate limit, provider 5xx, malformed provider result, and
model-specific rejection.  It never retries local request/auth/authorization
failures, oversized input, forbidden declarations, business validation, or an
unknown/multiple native tool call discovered by the caller.
"""
from __future__ import annotations

import base64
import os
import time
import uuid
from urllib.parse import urlsplit

import requests


FALLBACK_ELIGIBLE = frozenset({
    "timeout", "unavailable", "rate_limited", "provider_5xx",
    "malformed_provider_result", "model_rejected",
})

RELAY_ERROR_CATEGORIES = {
    "provider_timeout": "timeout",
    "provider_unavailable": "unavailable",
    "provider_rate_limited": "rate_limited",
    "provider_upstream_5xx": "provider_5xx",
    "provider_malformed_response": "malformed_provider_result",
    "provider_model_rejected": "model_rejected",
    "invalid_provider_request": "invalid_request",
    "provider_authorization": "authorization_failure",
    "provider_model_not_found": "configuration",
}


class RelayTransportError(RuntimeError):
    def __init__(self, category, *, status_code=None, request_id=None, attempts=()):
        super().__init__(category)
        self.category = category
        self.status_code = status_code
        self.request_id = request_id
        self.attempts = tuple(attempts)

    @property
    def fallback_eligible(self):
        return self.category in FALLBACK_ELIGIBLE

def _validated_relay_url(raw):
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in {None, 443}
        or parsed.path not in {"", "/"}
    ):
        raise RelayTransportError("configuration")
    return f"https://{parsed.hostname}"


def _payload(*, text, model, response_schema, image_bytes, mime_type,
             system_instruction, function_declarations, allowed_function_names):
    payload = {"text": text, "model": model, "response_schema": response_schema}
    if system_instruction:
        payload["system_instruction"] = system_instruction
    if image_bytes is not None:
        payload.update(image_bytes_b64=base64.b64encode(image_bytes).decode("ascii"), mime_type=mime_type)
    if function_declarations is not None:
        payload.update(function_declarations=function_declarations,
                       allowed_function_names=sorted(allowed_function_names or []))
        payload.pop("response_schema", None)
    return payload


def _http_category(status_code):
    if status_code == 408:
        return "timeout"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "provider_5xx"
    if status_code == 401:
        return "invalid_relay_auth"
    if status_code == 403:
        return "authorization_failure"
    if status_code in {400, 413}:
        return "invalid_request"
    return "model_rejected"


def _relay_error_category(response):
    """Prefer the relay's safe internal category over a lossy HTTP bucket."""
    try:
        body = response.json()
        category = body.get("error_category")
    except (AttributeError, TypeError, ValueError):
        category = None
    return RELAY_ERROR_CATEGORIES.get(category, _http_category(response.status_code))


def relay_call(*, text, model, response_schema=None, image_bytes=None,
               mime_type=None, system_instruction=None,
               function_declarations=None, allowed_function_names=None,
               timeout=45, request_post=None, request_id=None):
    """Make one request; return typed result, latency, and a safe request ID."""
    if os.environ.get("GEMINI_TRANSPORT", "direct").strip().lower() != "relay":
        raise RelayTransportError("not_configured")
    raw_url = os.environ.get("GEMINI_RELAY_URL", "").strip()
    secret = os.environ.get("GEMINI_RELAY_SECRET", "").strip()
    if not raw_url or not secret:
        raise RelayTransportError("configuration")
    url = _validated_relay_url(raw_url)
    request_post = request_post or requests.post
    request_id = request_id or uuid.uuid4().hex
    started = time.monotonic()
    try:
        response = request_post(
            url + "/v1/generate",
            json=_payload(
                text=text, model=model, response_schema=response_schema,
                image_bytes=image_bytes, mime_type=mime_type,
                system_instruction=system_instruction,
                function_declarations=function_declarations,
                allowed_function_names=allowed_function_names,
            ),
            headers={"Authorization": "Bearer " + secret, "X-Relay-Request-ID": request_id},
            timeout=timeout,
            allow_redirects=False,
        )
    except requests.exceptions.Timeout as exc:
        raise RelayTransportError("timeout", request_id=request_id) from exc
    except requests.exceptions.RequestException as exc:
        raise RelayTransportError("unavailable", request_id=request_id) from exc
    if response.status_code == 200:
        try:
            body = response.json()
            if body.get("status") != "ok" or "result" not in body:
                raise ValueError("bad relay result")
        except (AttributeError, TypeError, ValueError) as exc:
            raise RelayTransportError("malformed_provider_result", status_code=200,
                                      request_id=request_id) from exc
        return body["result"], int((time.monotonic() - started) * 1000), request_id
    response_request_id = response.headers.get("X-Relay-Request-ID") if hasattr(response, "headers") else None
    raise RelayTransportError(
        _relay_error_category(response), status_code=response.status_code,
        request_id=response_request_id or request_id,
    )


def relay_call_with_fallback(*, models, result_validator=None, **kwargs):
    """Try primary then at most one eligible fallback with safe attempt metadata."""
    ordered = []
    for model in models:
        if model and model not in ordered:
            ordered.append(model)
    if not ordered:
        raise RelayTransportError("configuration")
    attempts = []
    for index, model in enumerate(ordered[:2]):
        request_id = uuid.uuid4().hex
        try:
            result, latency_ms, request_id = relay_call(
                model=model, request_id=request_id, **kwargs
            )
            if result_validator is not None:
                try:
                    result = result_validator(result)
                except RelayTransportError as exc:
                    attempts.append({"model": model, "category": exc.category,
                                     "status_code": exc.status_code, "request_id": request_id})
                    if index == 0 and len(ordered) > 1 and exc.fallback_eligible:
                        continue
                    raise RelayTransportError(
                        exc.category, status_code=exc.status_code, request_id=request_id,
                        attempts=attempts,
                    ) from exc
            attempts.append({"model": model, "category": "success", "request_id": request_id})
            return result, {
                "primary_model": ordered[0],
                "fallback_model": ordered[1] if len(ordered) > 1 else None,
                "attempt_count": len(attempts), "final_model": model,
                "final_outcome": "success", "latency_ms": latency_ms,
                "request_id": request_id, "attempts": attempts,
            }
        except RelayTransportError as exc:
            if not attempts or attempts[-1].get("request_id") != request_id:
                attempts.append({"model": model, "category": exc.category,
                                 "status_code": exc.status_code, "request_id": request_id})
            if index == 0 and len(ordered) > 1 and exc.fallback_eligible:
                continue
            raise RelayTransportError(
                exc.category, status_code=exc.status_code, request_id=request_id,
                attempts=attempts,
            ) from exc
