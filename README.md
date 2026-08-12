# Wearable Readiness Pipeline

A self-hosted personal data pipeline that imports WHOOP recovery, sleep, and
workout records; combines them with explicitly logged context; produces a
private readiness dashboard; and can deliver summaries through an authorized
Telegram bot.

This repository is a sanitized public-export candidate. It contains source,
tests, and public documentation only. It intentionally contains no user data,
runtime database, generated dashboard, credentials, logs, backups, screenshots,
videos, or private operational history.

> Publication gate: credential rotation, WHOOP terms review, UI provenance
> review, and a project-license decision are still required. Until a `LICENSE`
> file is added, the source is viewable but is not offered as open source.

## What it demonstrates

- WHOOP OAuth ingestion with rotating-token quarantine and atomic persistence.
- A canonical SQLite read model for daily recovery, sleep, and activity data.
- An authorized private-chat Telegram workflow with idempotent writes.
- Optional, bounded Gemini analysis behind explicit privacy-sensitive flags.
- Metadata-stripped, size-bounded image preparation before optional vision use.
- A generated single-file dashboard with integrity checks and atomic publish.
- Backup, deploy, rollback, observability, and report-delivery contracts.
- A stateless optional relay for keyless Vertex AI calls.

The project is independently developed and is not affiliated with, endorsed by,
or sponsored by WHOOP, Telegram, Google, or their respective affiliates.

## Privacy-first starting point

All external AI feature flags are off by default. The example configuration has
no credential, account identifier, host, project, model, or endpoint values.
Read [PRIVACY.md](PRIVACY.md) before enabling Telegram, Gemini, image analysis,
the relay, a remote dashboard, or the legacy CSV importer.

## Repository map

| Area | Purpose |
|---|---|
| `fetch_data.py`, `whoop_auth.py`, `auth.py` | WHOOP ingestion and OAuth lifecycle |
| `canonical_read_model.py`, `workouts_db.py` | Canonical local data contracts |
| `telegram_bot.py`, `conversation_*` | Authorized conversational interface |
| `generate_insights.py`, `bounded_agent.py` | Optional bounded AI analysis |
| `build_dashboard.py`, `dashboard_template.html`, `dashboard_ui/` | Generated private dashboard |
| `morning_flow.py`, `report_delivery.py` | Scheduled, resumable morning workflow |
| `deploy.py`, `backup_database.py`, `release_manifest.py` | Deployment and recovery gates |
| `relay/` | Optional stateless Gemini relay |
| `tests/` | Offline tests with fake provider boundaries |

## Local verification

Use Python 3.12 or 3.13. Tests must use placeholders and mocked external calls;
they do not require real WHOOP, Telegram, Gemini, VPS, or Cloud credentials.

Windows PowerShell:

```powershell
python -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt
$env:TELEGRAM_BOT_TOKEN='100001:test-only-placeholder'
$env:TELEGRAM_CHAT_ID='100001'
$env:WHOOP_CLIENT_ID='test-client-id'
$env:WHOOP_CLIENT_SECRET='test-client-secret'
$env:WHOOP_REDIRECT_URI='http://localhost:8080/callback'
$env:CONVERSATIONAL_ROUTER_ENABLED='true'
$env:GEMINI_VISION_ENABLED='true'
venv/Scripts/python.exe scripts/public_acceptance_scan.py
venv/Scripts/python.exe -m unittest discover -s tests -v
```

Copy `.env.example` to `.env` only after reviewing
[docs/CONFIGURATION.md](docs/CONFIGURATION.md). Never use production data for a
demo. Generated `dashboard.html` is private runtime output and must not be
committed or hosted publicly.

## Project context

- [Project story](docs/PROJECT_STORY.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Roadmap](docs/ROADMAP.md)
- [What is omitted for privacy](docs/OMITTED_FOR_PRIVACY.md)
- [Security policy](SECURITY.md)
- [Credential rotation runbook](docs/CREDENTIAL_ROTATION_RUNBOOK.md)
- [Publishing checklist](docs/PUBLISHING_CHECKLIST.md)

The current public-export work was assembled through iterative agent-assisted
development in Codex with manual review. That statement describes this export
process; it does not claim exclusive tool authorship of the project's earlier
work.
