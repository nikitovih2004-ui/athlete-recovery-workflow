# Publishing checklist

The old private repository remains private. The public repository must be a new
repository created from a reviewed sanitized commit, with no imported history.

## Content gate

- [ ] Canonical source is the reviewed production-ready line; no private branch
      or merge history is imported.
- [ ] `data/`, databases, logs, backups, generated dashboards, screenshots,
      videos, design references, private handoffs, local metadata, and runtime
      state are absent.
- [ ] `.env.example` contains only blank secrets/identifiers and safe defaults.
- [ ] No token, key, certificate, capability URL, host, IP, private date, health
      value, lifestyle note, chat export, or provider response is present.
- [ ] No forbidden provider/tool references or agent workspace metadata remain.
- [ ] README links resolve to files in the export.

## Code and dependency gate

- [ ] `scripts/public_acceptance_scan.py` passes in a fresh clone.
- [ ] A second independent secret scanner and entropy scan pass over the full
      new history, archive, release, CI, and issue templates.
- [ ] Dependency lock is regenerated with hashes from a clean supported Python
      environment and installs with `--require-hashes`.
- [ ] Third-party notices, license texts, SBOM, container digest, and provider
      terms review are complete.
- [ ] CI runs tests, secret scanning, dependency audit, license policy, and
      container checks with least-privilege permissions.
- [ ] Project license is selected only after dependency and UI provenance review.

## Operational gate

- [ ] WHOOP grants/client secret, Telegram token, Gemini/direct key or relay
      bearer, cloud keys, and SSH credentials are rotated or revoked.
- [ ] Private dashboard access is verified; public Funnel/CDN exposure is off.
- [ ] Backup, restore, retention, log rotation, and rollback procedures are
      documented and tested.
- [ ] No production deploy or credential is coupled to the public push.

## Fresh-clone acceptance

1. Create an empty directory outside every existing worktree.
2. Copy only the sanitized commit or clone the new repository.
3. Confirm the history has one sanitized initial commit and no unexpected refs.
4. Run the acceptance scan and full offline test suite with synthetic values.
5. Enumerate tracked files and MIME types; investigate every binary.
6. Inspect the archive, release assets, CI configuration, issues, and actions
   logs for secrets or personal data.
7. Only after approval, create the repository, push the sanitized commit, enable
   branch protection/security features, and then change visibility if intended.

No step in this checklist authorizes credential rotation, production changes,
repository creation, push, or visibility changes by itself; those require the
operator's explicit approval at the applicable stage.
