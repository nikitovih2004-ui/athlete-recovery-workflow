# Privacy and data-flow disclosure

## Scope

This is a self-hosted, single-operator personal project. The repository
maintainer does not receive runtime data by default. The operator chooses and
configures every external service and is responsible for access control,
retention, legal basis, provider agreements, and deletion procedures.

This document describes the software's data flows; it is not a claim of HIPAA,
GDPR, or other regulatory compliance and is not medical advice.

## Data inventory

The canonical SQLite database can contain:

- WHOOP workouts and provider response fields;
- recovery score, resting heart rate, HRV, SpO2, skin temperature, and raw
  provider JSON;
- sleep timing, performance, efficiency, respiratory rate, stages, and raw
  provider JSON;
- manually entered strength/cardio activity and supplements;
- daily free-text context and derived lifestyle-factor categories;
- Telegram chat, sender, message, reply, delivery, and workflow identifiers;
- bounded conversation actions, validation results, and audit metadata.

Raw provider JSON can contain fields beyond those rendered by the dashboard.

## Data-flow matrix

| Source | Data sent | Destination | Local storage |
|---|---|---|---|
| WHOOP OAuth/API | OAuth grants and requested health/activity records | WHOOP API | Tokens in a restricted runtime file; records in SQLite |
| Telegram text | Message text, IDs, timestamps, replies | Telegram; optionally Gemini when an AI feature is enabled | Validated actions, domain records, delivery state, and selected audit metadata |
| Telegram image | Image or image-document plus caption | Telegram; optionally Gemini/relay only when `GEMINI_VISION_ENABLED=true` | The raw image is not intentionally persisted by this app |
| Direct Gemini | Metrics, baselines, workouts, supplements, context, bounded prompts or a sanitized image | Google Gemini Developer API | Parsed results and safe metadata; not provider response bodies |
| Relay mode | The same selected prompt/schema/function/image payload | Operator relay, then Vertex AI | Safe request/model/status metadata; relay is designed to be stateless |
| Legacy CSV import | A capability URL and lifestyle text | Configured CSV host | Imported daily context in SQLite |
| Dashboard | Health, activity, supplement, and lifestyle views | Local file or operator-controlled private network service | Generated `dashboard.html` |

## Default-safe policy

- AI routing, bounded agents, factor capture, memory, analytics, weekly AI, and
  image analysis default to off.
- Credentials and identifiers are blank in `.env.example`.
- Image analysis requires the explicit `GEMINI_VISION_ENABLED=true` flag and an
  authorized private Telegram message.
- Before an enabled image is sent to a provider, the app decodes it, applies
  orientation, converts it to RGB, bounds encoded bytes and decoded pixels,
  resizes it, and re-encodes it as JPEG without carrying EXIF metadata.
- The sanitized full frame is still sent. The software does not know which
  visible details are private; crop sensitive areas before uploading.
- Relay URLs must use HTTPS on the default TLS port and redirects are disabled.

Enabling an API key alone does not enable the feature flags, but directly
running an analysis script with a configured key is itself an explicit outbound
action. Use synthetic data for tests and demonstrations.

## Telegram boundary

The bot rejects group chats and requires the configured private chat. A
production deployment should also require `TELEGRAM_USER_ID`; chat access alone
is not a substitute for sender verification. Telegram retains messages and
attachments under its own policies. Deleting local records does not delete
Telegram copies.

## Gemini and relay boundary

Gemini analysis may receive sensitive health, exercise, supplement, alcohol,
caffeine, stress, sleep, and free-text context. Direct mode sends to the Gemini
Developer API. Relay mode sends through the operator's authenticated endpoint
to Vertex AI. Provider retention, region, abuse monitoring, and deletion are
governed by the operator's provider configuration and agreements.

Do not enable these paths unless the operator has accepted that processing.
Prefer relay/keyless runtime identity where appropriate, a dedicated project,
least-privilege IAM, model allowlists, spending limits, and no prompt logging.

## Dashboard access

The dashboard has no built-in authentication. If it is served, access control
must be provided externally. A Tailscale deployment must keep Funnel off and
restrict Serve through reviewed tailnet ACLs. Never expose `dashboard.html`
through an unauthenticated public web server or CDN.

## Retention and deletion limitations

- Canonical health and lifestyle records have no automatic expiry.
- Some corrections are soft-delete/retraction events rather than erasure.
- Conversation audit rows are durable; selected payloads are redacted over
  time, but validated mutations and identifiers can remain.
- Clarification/session state expires sooner, but expiry does not erase domain
  records already committed.
- Database backups and logs require operator-managed retention. A retracted row
  can remain in backups until every relevant backup expires or is destroyed.

The project therefore does not promise a one-click right-to-erasure workflow.
An operator handling an export or deletion request must inventory the live
database, audit tables, Telegram messages, generated reports, logs, provider
records, and every backup before claiming completion.

## Security and incident response

Treat `.env`, OAuth tokens, Telegram identifiers, SQLite, generated dashboards,
logs, and backups as sensitive. Use restrictive filesystem permissions,
encrypted storage where appropriate, pinned SSH host keys, least-privilege
service accounts, and private vulnerability reporting. If exposure is
suspected, follow [docs/CREDENTIAL_ROTATION_RUNBOOK.md](docs/CREDENTIAL_ROTATION_RUNBOOK.md).

## Children and health decisions

This project is not designed for children and does not provide medical advice,
diagnosis, or treatment. Readiness summaries can be incomplete or incorrect;
do not use them as a substitute for professional care.
