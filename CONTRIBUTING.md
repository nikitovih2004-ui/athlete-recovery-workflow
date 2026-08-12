# Contributing

This project handles unusually sensitive personal data. Privacy review is part
of correctness, not an optional follow-up.

## Ground rules

- Use synthetic fixtures only.
- Never attach or commit a database, log, backup, generated dashboard, chat
  export, screenshot, video, provider response, token file, `.env`, key, or real
  identifier.
- External calls must be mocked in tests. A normal test run must not call WHOOP,
  Telegram, Gemini, a relay, a VPS, or a cloud provider.
- Edit `dashboard_template.html` and `dashboard_ui/`; never edit generated
  `dashboard.html`.
- Any new provider, persisted field, log, retention rule, or authorization path
  requires updates to `PRIVACY.md`, `SECURITY.md`, and configuration docs.
- Dependency changes require security and license review plus lock regeneration.
- Deployment changes require rollback and stale-file tests.

## Verification

Create a virtual environment, install `requirements.txt`, then run:

```text
python scripts/public_acceptance_scan.py
python -m unittest discover -s tests -v
```

Use only obviously synthetic environment placeholders. See the CI workflow for
the minimal test environment.

## Pull-request checklist

- [ ] Tests cover success, failure, retry, and authorization boundaries.
- [ ] No real secret, identifier, personal narrative, health value, host, or IP
      appears in code, fixtures, commits, screenshots, or logs.
- [ ] No unreviewed binary or generated artifact is added.
- [ ] Secret/PII acceptance scan passes from a fresh clone.
- [ ] Privacy, security, configuration, and architecture docs are current.
- [ ] Dependency license/security impact is documented.
- [ ] Runtime and deploy behavior has an explicit rollback path.

Security reports must follow `SECURITY.md`, not the public issue tracker.
