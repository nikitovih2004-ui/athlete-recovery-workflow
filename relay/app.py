"""Minimal, memory-only relay for Gemini Developer API.

The module uses only the Python standard library.  It deliberately has no
filesystem writes and suppresses request logging: prompts, images, and Gemini
responses must not become application data or Cloud Run logs.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import ssl
import sys
import traceback
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MAX_REQUEST_BYTES = min(int(os.getenv("RELAY_MAX_REQUEST_BYTES", str(8 * 1024 * 1024))), 8 * 1024 * 1024)
MAX_IMAGE_BYTES = min(int(os.getenv("RELAY_MAX_IMAGE_BYTES", str(5 * 1024 * 1024))), 5 * 1024 * 1024)
TIMEOUT_SECONDS = min(max(float(os.getenv("RELAY_TIMEOUT_SECONDS", "30")), 1), 60)
ALLOWED_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
MODEL_NAME = re.compile(r"^gemini-[a-z0-9][a-z0-9.-]{0,127}$")
SCHEMA_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean"})


class ClientError(ValueError):
    pass


class ProviderError(RuntimeError):
    def __init__(self, category: str, *, provider_status: int | None = None,
                 exception_class: str | None = None):
        super().__init__(category)
        self.category = category
        self.provider_status = provider_status
        self.exception_class = exception_class or type(self).__name__


def _configured_models() -> frozenset[str]:
    models = frozenset(item.strip() for item in os.getenv("GEMINI_ALLOWED_MODELS", "").split(",") if item.strip())
    if not models:
        raise RuntimeError("GEMINI_ALLOWED_MODELS is empty")
    return models


def _expect_string(value, field: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ClientError(f"invalid_{field}")
    return value


def _validate_schema(schema: object, depth: int = 0) -> dict:
    if depth > 12 or not isinstance(schema, dict):
        raise ClientError("invalid_response_schema")
    value_type = schema.get("type")
    if value_type not in SCHEMA_TYPES:
        raise ClientError("invalid_response_schema")
    checked = {"type": value_type}
    if "nullable" in schema:
        if not isinstance(schema["nullable"], bool):
            raise ClientError("invalid_response_schema")
        checked["nullable"] = schema["nullable"]
    if "enum" in schema:
        if not isinstance(schema["enum"], list) or len(schema["enum"]) > 100:
            raise ClientError("invalid_response_schema")
        checked["enum"] = schema["enum"]
    if value_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (not isinstance(properties, dict) or len(properties) > 100 or
                not isinstance(required, list) or not all(isinstance(key, str) and key in properties for key in required)):
            raise ClientError("invalid_response_schema")
        checked["properties"] = {key: _validate_schema(value, depth + 1) for key, value in properties.items()}
        checked["required"] = required
    if value_type == "array":
        checked["items"] = _validate_schema(schema.get("items"), depth + 1)
    return checked


def parse_request(payload: object) -> dict:
    fields = {"text", "image_bytes_b64", "mime_type", "model", "response_schema", "system_instruction", "function_declarations", "allowed_function_names"}
    if not isinstance(payload, dict) or set(payload) - fields:
        raise ClientError("invalid_request")
    text = _expect_string(payload.get("text"), "text", max_length=16_000)
    model = _expect_string(payload.get("model"), "model", max_length=128)
    if not MODEL_NAME.fullmatch(model) or model not in _configured_models():
        raise ClientError("model_not_allowed")
    image_b64 = payload.get("image_bytes_b64")
    image = None
    mime_type = None
    if image_b64 is not None:
        if not isinstance(image_b64, str) or len(image_b64) > (MAX_IMAGE_BYTES * 4 // 3 + 8):
            raise ClientError("invalid_image")
        mime_type = payload.get("mime_type")
        if mime_type not in ALLOWED_MIME_TYPES:
            raise ClientError("invalid_mime_type")
        try:
            image = base64.b64decode(image_b64.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise ClientError("invalid_image") from exc
        if not image or len(image) > MAX_IMAGE_BYTES:
            raise ClientError("image_too_large")
    elif payload.get("mime_type") is not None:
        raise ClientError("invalid_mime_type")
    declarations = payload.get("function_declarations")
    allowed_names = payload.get("allowed_function_names")
    if declarations is not None:
        if (not isinstance(declarations, list) or not declarations or len(declarations) > 32
                or not all(isinstance(item, dict) for item in declarations)
                or not isinstance(allowed_names, list) or not allowed_names
                or not all(isinstance(item, str) for item in allowed_names)
                or payload.get("response_schema") is not None):
            raise ClientError("invalid_function_declarations")
    elif allowed_names is not None:
        raise ClientError("invalid_request")
    system_instruction = payload.get("system_instruction")
    if system_instruction is not None and (not isinstance(system_instruction, str) or len(system_instruction) > 16_000):
        raise ClientError("invalid_system_instruction")
    return {
        "text": text,
        "system_instruction": system_instruction,
        "image": image,
        "mime_type": mime_type,
        "model": model,
        "response_schema": None if declarations is not None else _validate_schema(payload.get("response_schema")),
        "function_declarations": declarations,
        "allowed_function_names": frozenset(allowed_names or []),
    }


def _matches_schema(value: object, schema: dict) -> bool:
    if value is None:
        return bool(schema.get("nullable"))
    schema_type = schema["type"]
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
    }
    if not checks[schema_type](value):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if schema_type == "object":
        if any(key not in value for key in schema["required"]):
            return False
        # Returning undeclared provider fields would violate the relay's typed
        # boundary, even if Gemini happened to emit a valid declared subset.
        return (set(value).issubset(schema["properties"]) and
                all(key not in value or _matches_schema(value[key], child)
                    for key, child in schema["properties"].items()))
    if schema_type == "array":
        return all(_matches_schema(item, schema["items"]) for item in value)
    return True


def _vertex_access_token() -> str:
    """Get a short-lived token from Cloud Run's attached service identity."""
    metadata_request = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/token"
        "?scopes=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform",
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(metadata_request, timeout=3) as response:
            token = json.loads(response.read().decode("utf-8")).get("access_token")
    except (urllib.error.URLError, ValueError, TimeoutError) as exc:
        raise ProviderError("provider_unavailable") from exc
    if not isinstance(token, str) or not token:
        raise ProviderError("provider_unavailable")
    return token


def _provider_http_category(status: int) -> str:
    if status in {408, 504}:
        return "provider_timeout"
    if status == 429:
        return "provider_rate_limited"
    if status in {401, 403}:
        return "provider_authorization"
    if status == 404:
        return "provider_model_not_found"
    if status == 400:
        return "invalid_provider_request"
    if status >= 500:
        return "provider_upstream_5xx"
    return "provider_model_rejected"


def _safe_exception_location(exc: BaseException) -> dict:
    frames = traceback.extract_tb(exc.__traceback__)
    frame = next((item for item in reversed(frames) if os.path.basename(item.filename) == "app.py"), None)
    if frame is None:
        return {"source_file": "app.py", "source_function": "unknown", "source_line": 0}
    return {
        "source_file": os.path.basename(frame.filename),
        "source_function": frame.name,
        "source_line": frame.lineno,
    }


def _log_provider_error(exc: ProviderError, *, request_id: str, model: str | None) -> None:
    event = {
        "severity": "ERROR",
        "event": "relay_provider_error",
        "request_id": request_id,
        "safe_category": exc.category,
        "provider_http_status": exc.provider_status,
        "exception_class": exc.exception_class,
        "model": model,
        **_safe_exception_location(exc),
    }
    print(json.dumps(event, separators=(",", ":"), sort_keys=True), file=sys.stdout, flush=True)


def _provider_generation_config(model: str) -> dict:
    if model.startswith("gemini-3"):
        return {"thinkingConfig": {"thinkingLevel": "low"}}
    return {"temperature": 0}


def _select_response_part(response_body: dict, key: str) -> dict:
    try:
        parts = response_body["candidates"][0]["content"]["parts"]
        return next(part for part in parts if key in part)
    except (KeyError, IndexError, TypeError, StopIteration) as exc:
        raise ProviderError(
            "provider_malformed_response",
            exception_class=type(exc).__name__,
        ) from exc


def call_gemini(request: dict) -> object:
    project_id = os.getenv("VERTEX_PROJECT_ID", "").strip()
    if not project_id:
        raise ProviderError("provider_unavailable")
    parts = [{"text": request["text"]}]
    if request["image"] is not None:
        parts.append({"inlineData": {
            "mimeType": request["mime_type"],
            "data": base64.b64encode(request["image"]).decode("ascii"),
        }})
    request_body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": _provider_generation_config(request["model"]),
    }
    if request["system_instruction"]:
        request_body["systemInstruction"] = {"parts": [{"text": request["system_instruction"]}]}
    if request["function_declarations"]:
        request_body["tools"] = [{"functionDeclarations": request["function_declarations"]}]
        request_body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": sorted(request["allowed_function_names"])}}
    else:
        request_body["generationConfig"].update(responseMimeType="application/json", responseSchema=request["response_schema"])
    body = json.dumps(request_body, separators=(",", ":")).encode("utf-8")
    url = (
        "https://aiplatform.googleapis.com/v1/projects/%s/locations/global/"
        "publishers/google/models/%s:generateContent"
    ) % (project_id, request["model"])
    provider_request = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + _vertex_access_token(),
            # Service-account access tokens do not carry an end-user quota
            # project. Bind quota and Service Usage explicitly to the same
            # project that owns the Vertex resource.
            "X-Goog-User-Project": project_id,
        },
    )
    try:
        with urllib.request.urlopen(provider_request, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()) as response:
            if response.status != 200:
                raise ProviderError(
                    _provider_http_category(response.status),
                    provider_status=response.status,
                )
            response_body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Drain then discard the body.  Provider bodies may contain request
        # material and are deliberately neither logged nor returned.
        exc.read()
        raise ProviderError(
            _provider_http_category(exc.code), provider_status=exc.code,
            exception_class=type(exc).__name__,
        ) from exc
    except TimeoutError as exc:
        raise ProviderError("provider_timeout", exception_class=type(exc).__name__) from exc
    except urllib.error.URLError as exc:
        category = "provider_timeout" if isinstance(exc.reason, TimeoutError) else "provider_unavailable"
        raise ProviderError(category, exception_class=type(exc).__name__) from exc
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderError("provider_malformed_response", exception_class=type(exc).__name__) from exc
    try:
        if request["function_declarations"]:
            part = _select_response_part(response_body, "functionCall")
            call = part["functionCall"]
            result = {"name": call["name"], "args": call["args"]}
            if result["name"] not in request["allowed_function_names"] or not isinstance(result["args"], dict):
                raise ValueError("invalid function call")
        else:
            part = _select_response_part(response_body, "text")
            result = json.loads(part["text"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ProviderError("provider_malformed_response", exception_class=type(exc).__name__) from exc
    if request["response_schema"] is not None and not _matches_schema(result, request["response_schema"]):
        raise ProviderError("provider_malformed_response", exception_class="SchemaMismatch")
    return result


def _request_id(value: str | None) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{8,64}", value):
        return value
    return uuid.uuid4().hex


def _error_http_status(category: str) -> int:
    if category == "invalid_provider_request":
        return 422
    if category == "provider_rate_limited":
        return 429
    if category == "provider_timeout":
        return 504
    if category == "provider_authorization":
        return 502
    return 502


class RelayHandler(BaseHTTPRequestHandler):
    server_version = "GeminiRelay"

    def log_message(self, format: str, *args: object) -> None:
        """Do not emit URLs, request metadata, or accidental prompt data."""

    def _json(self, code: int, value: dict, *, request_id: str | None = None) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if request_id:
            self.send_header("X-Relay-Request-ID", request_id)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        # `/healthz` is intercepted before this container by the Cloud Run
        # frontend in this deployment; keep the health probe on a namespaced,
        # application-owned path so it can always reach this handler.
        if self.path in {"/healthz", "/relay-healthz"}:
            self._json(200, {"status": "ok"})
        elif self.path == "/readyz":
            request_id = _request_id(self.headers.get("X-Relay-Request-ID"))
            if not self._authorized():
                self._json(401, {"status": "unauthorized", "request_id": request_id}, request_id=request_id)
                return
            try:
                _configured_models()
                ready = bool(os.getenv("VERTEX_PROJECT_ID", "").strip() and os.getenv("RELAY_BEARER_TOKEN", "").strip())
            except RuntimeError:
                ready = False
            self._json(200 if ready else 503, {"status": "ready" if ready else "not_ready", "request_id": request_id}, request_id=request_id)
        else:
            self._json(404, {"status": "not_found"})

    def _authorized(self) -> bool:
        expected = os.getenv("RELAY_BEARER_TOKEN", "").strip()
        presented = self.headers.get("Authorization", "")
        return bool(expected and hmac.compare_digest(presented, "Bearer " + expected))

    def _read_json_request(self, *, max_bytes: int = MAX_REQUEST_BYTES) -> object:
        content_length = int(self.headers.get("Content-Length", ""))
        if content_length < 1 or content_length > max_bytes:
            raise ClientError("request_too_large")
        raw = self.rfile.read(content_length)
        if len(raw) != content_length:
            raise ClientError("invalid_request")
        return json.loads(raw.decode("utf-8"))

    def do_POST(self) -> None:
        if self.path not in {"/v1/generate", "/v1/provider-probe"}:
            self._json(404, {"status": "not_found"})
            return
        # Secret Manager preserves a trailing line break if a secret version
        # was supplied through a line-oriented stdin command.  Normalize only
        # this transport artifact in memory; the token is never logged.
        request_id = _request_id(self.headers.get("X-Relay-Request-ID"))
        if not self._authorized():
            self._json(401, {"status": "unauthorized"})
            return
        request = None
        try:
            payload = self._read_json_request(max_bytes=1024 if self.path == "/v1/provider-probe" else MAX_REQUEST_BYTES)
            if self.path == "/v1/provider-probe":
                if not isinstance(payload, dict) or set(payload) != {"model"}:
                    raise ClientError("invalid_request")
                request = parse_request({
                    "text": "Return a JSON object with ready set to true.",
                    "model": payload["model"],
                    "response_schema": {
                        "type": "object",
                        "properties": {"ready": {"type": "boolean"}},
                        "required": ["ready"],
                    },
                })
            else:
                request = parse_request(payload)
            result = call_gemini(request)
        except (ClientError, UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"status": "invalid_request", "request_id": request_id}, request_id=request_id)
        except ProviderError as exc:
            _log_provider_error(exc, request_id=request_id, model=request.get("model") if request else None)
            self._json(
                _error_http_status(exc.category),
                {"status": "provider_error", "error_category": exc.category, "request_id": request_id},
                request_id=request_id,
            )
        except (TypeError, ValueError):
            self._json(400, {"status": "invalid_request", "request_id": request_id}, request_id=request_id)
        else:
            self._json(200, {"status": "ok", "result": result, "request_id": request_id}, request_id=request_id)


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), RelayHandler).serve_forever()


if __name__ == "__main__":
    main()
