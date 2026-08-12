# Bounded Gemini agent

## Goal

Replace the semantic intent classifier with native Gemini function calling,
without giving the model authority over data or infrastructure.

Gemini understands free-form language and selects exactly one declared
function. Python remains authoritative for:

- Telegram authorization;
- the function allowlist;
- schema, confidence, date and fact-status validation;
- clarification and destructive confirmation;
- atomic/idempotent persistence, leases and fencing;
- provenance, immutable events and canonical read-back;
- user-visible grounded answers for data reads.

## Boundary

The agent receives no SQL, shell, filesystem, network, secret, delete or
arbitrary-query tool. It can select only:

- four existing mutation tools;
- seven fixed canonical read tools;
- ordinary conversation;
- one bounded clarification;
- unsupported-request refusal.

Corrections/deletions remain deterministic server-side flows with explicit
Reply confirmation and are deliberately not exposed to Gemini.

## Request flow

1. Authorize the private Telegram chat and sender.
2. Reserve the immutable action/idempotency key.
3. Handle an existing pending confirmation deterministically.
4. Run deterministic grounded read/explicit-strength fallbacks where applicable.
5. If `BOUNDED_GEMINI_AGENT_ENABLED=true`, send the message and validated
   read-only session context to Gemini with `functionCallingConfig.mode=ANY`.
6. For a likely personal-data question, narrow that call's allowlist to the
   canonical read tools only. Gemini still selects the applicable read and its
   bounded arguments; Python never constructs them from an exact sentence.
7. Require exactly one allowlisted `functionCall`.
8. Reject a `respond_to_user` result when the independent personal-data guard
   says canonical data was required; no prose fallback is delivered.
9. Convert the call to the existing strict Python validation envelope.
10. Apply confidence, semantic and date gates.
11. Persist validated arguments before executing the allowlisted tool.
12. Execute through the existing atomic transaction and render a grounded reply.

The personal-data guard is a fail-closed selection boundary, not a phrase
router. It recognizes the combination of personal-data concepts, temporal
scope, data-question semantics, and validated read follow-up context. It never
selects a particular read tool, derives tool arguments, queries SQLite, or
permits an answer from model memory. Ordinary conversation keeps the full
bounded declaration set and may use `respond_to_user` only when no stored fact
is needed.

Primary malformed output or transient failure may use one fallback model.
Unknown/multiple/prose-only calls never execute. Permanent auth/config and
safety failures remain fail-closed.

## Rollout

The compatibility JSON router stays available behind the flag for rollback.
Recommended rollout:

1. local fake-provider suite;
2. synthetic real-provider smoke without personal data;
3. production read-only canary;
4. enable the flag for the authorized private chat;
5. monitor safe action metadata (`model`, attempts, latency and error category);
6. remove the compatibility router only after a stable observation window.
