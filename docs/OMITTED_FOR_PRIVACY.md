# Intentionally omitted for privacy

The export is intentionally incomplete in ways that protect the operator and
make the remaining trust boundaries visible.

## Not included

- all WHOOP, recovery, sleep, workout, supplement, and lifestyle records;
- SQLite databases, raw provider JSON, generated reports, logs, and backups;
- generated `dashboard.html` and any real dashboard output;
- Telegram messages, attachments, file identifiers, chat identifiers, and
  delivery transcripts;
- OAuth grants, access tokens, refresh tokens, client secrets, API keys,
  bearer tokens, SSH keys, passwords, cookies, and capability URLs;
- IP addresses, hostnames, usernames tied to an operator, cloud project IDs,
  private paths, cron output, and incident timestamps;
- screenshots, videos, design-reference exports, fonts, and other binaries;
- private production runbooks, handoff notes, raw prompts, internal traces,
  agent workspace metadata, and local editor configuration;
- old Git history and branches that contained any of the above.

## Why omission is a feature

The public code can explain schema boundaries, validators, tests, and deployment
contracts without transferring a person's health history or making an operator's
infrastructure discoverable. Synthetic fixtures are more useful for review than
redacted-looking real records because they cannot accidentally preserve a date,
identifier, or narrative detail.

## Safe demonstration boundary

Use fake credentials shaped like `100001:test-only-placeholder`, synthetic
dates, local SQLite temporary directories, and mocked HTTP responses. Keep AI
providers disabled unless the demonstration uses synthetic text and a clearly
reviewed endpoint. Never publish a generated dashboard from a real database.

If a future contributor proposes adding an example asset, they must document
its provenance, license, binary contents, metadata, and privacy review before it
is considered for inclusion.
