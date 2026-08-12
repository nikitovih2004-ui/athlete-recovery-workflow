# Security policy

## Supported version

Only the latest sanitized public branch is supported. Historical private
revisions and operator-specific deployments are not public support targets.

## Reporting a vulnerability

Use GitHub Private Vulnerability Reporting after it is enabled for the new
repository. Do not open a public issue containing credentials, health data,
chat identifiers, infrastructure details, database extracts, logs, generated
dashboards, screenshots, or proof-of-concept data from a real person.

Include affected files, a minimal synthetic reproduction, impact, and suggested
mitigation. Maintainers should acknowledge the report privately, preserve
confidentiality, classify severity, and coordinate remediation before public
disclosure. No response-time guarantee is made before a maintainer policy is
formally adopted.

## Threat model

The design assumes one operator, one authorized private Telegram chat, a
private runtime host, a private dashboard access layer, and trusted local
administrators. Important controls include:

- private-chat and sender authorization;
- no webhook/public HTTP surface for the Telegram bot;
- atomic and permission-restricted token/database writes;
- rotating-token quarantine and fail-closed OAuth handling;
- pinned SSH known-host verification;
- allowlisted Gemini functions and strict validation before writes;
- redacted transport errors and credential-free operational categories;
- HTTPS-only relay URLs, disabled redirects, bearer authentication, and size
  caps;
- generated-dashboard integrity checks and atomic replacement.

## Required operator controls

- Keep Tailscale Funnel off and restrict dashboard Serve ACLs.
- Require both Telegram chat and sender IDs in production.
- Store secrets outside Git and rotate them after any suspected exposure.
- Keep code/venv root-owned where practical; grant the runtime user write access
  only to necessary state.
- Maintain encrypted off-host backups and test restoration.
- Pin dependency and container artifacts before a release.

Self-hosting mistakes and third-party incidents are outside maintainer control,
but vulnerabilities in validation, authorization, redaction, storage, or deploy
code remain valid security reports.
