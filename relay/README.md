# Stateless Gemini relay (PoC)

This is intentionally a standalone Cloud Run service. It does not import this
repository, open a database, access Telegram, read user files, or persist
requests or Gemini responses.

## HTTP contract

`GET /healthz` and `GET /relay-healthz` return `{ "status": "ok" }` and never
call Gemini.

`GET /readyz` requires the relay bearer token and verifies that the model
allowlist, Vertex project and injected secret are present. It does not make a
paid provider call.

`POST /v1/provider-probe` also requires the bearer token. Its only accepted
input is `{ "model": "<allowlisted model>" }`; the relay supplies a fixed
synthetic prompt and a minimal typed response schema, then performs one real
Vertex request. This endpoint is for deploy/readiness diagnostics, not routine
health checks.

`POST /v1/generate` accepts a JSON request authenticated with:

```
Authorization: Bearer <RELAY_BEARER_TOKEN>
```

```json
{
  "text": "Describe only visible values.",
  "image_bytes_b64": "optional base64 image bytes",
  "mime_type": "image/png",
  "model": "gemini-2.5-flash",
  "response_schema": {
    "type": "object",
    "properties": {"label": {"type": "string"}},
    "required": ["label"]
  }
}
```

The response intentionally has only a status and the parsed, schema-validated
Gemini value:

```json
{"status":"ok","result":{"label":"..."}}
```

Gemini/provider payloads are never returned, logged, or stored.

Provider failures return a safe category and opaque request ID. Structured
Cloud Run error logs contain only the request ID, model, provider HTTP status,
exception class and source location. They never contain prompts, image bytes,
provider response bodies or credentials.

## Required runtime configuration

- `RELAY_BEARER_TOKEN` — injected from Secret Manager on Cloud Run.
- `GEMINI_ALLOWED_MODELS` — comma-separated model allowlist.
- `VERTEX_PROJECT_ID` — the dedicated Google Cloud project used by Vertex AI.

Optional controls: `PORT` (default `8080`), `RELAY_MAX_REQUEST_BYTES`
(default `8 MiB`), `RELAY_MAX_IMAGE_BYTES` (default `5 MiB`), and
`RELAY_TIMEOUT_SECONDS` (default `30`, capped at `60`).

For the PoC, deploy the service with `--min-instances=0`, no VPC connector,
and a dedicated runtime service account that has `roles/aiplatform.user` and
access only to the bearer-token secret. The account needs no Cloud SQL,
Storage, Telegram, or VPS permissions. Gemini is called through Vertex AI with
the runtime identity rather than a Developer API key.

## Local test

```powershell
venv/Scripts/python.exe -m unittest relay/tests/test_relay.py -v
```

No test makes a Gemini call.
