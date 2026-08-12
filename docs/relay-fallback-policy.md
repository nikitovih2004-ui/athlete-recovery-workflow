# Relay fallback policy

The bounded text agent and cardio vision call `relay_call_with_fallback` with
one primary and at most one distinct fallback model. Each attempt has a fresh
opaque request ID; only model name, safe category, HTTP status (when present),
attempt count and final model/outcome are retained in `relay_metadata_json` on
the audited conversation action (and in the cardio metadata before that action
is written).

Fallback is allowed only after: timeout (`408` or client timeout), unavailable
transport, `429`, provider `5xx`, malformed provider result, or a valid request
that the provider rejects as model-specific. A fallback is never attempted for
relay authentication/configuration failures, `401`/`403`, invalid/oversized
requests (`400`/`413`), forbidden declarations, local business validation,
clarification, or an unknown/undeclared tool call. Unknown and multiple calls
remain a safe rejection and cannot reach mutation code.

The transport never logs prompts, image bytes, provider bodies, API keys or
bearer credentials. Provider response content is not added to audit metadata.
