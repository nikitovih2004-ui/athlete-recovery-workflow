# Roadmap

This roadmap separates already demonstrated controls from work that must be
completed before a responsible public release.

## Now — public safety gate

- Finish the sanitized initial commit and verify it from a fresh clone.
- Rotate or revoke every credential that appeared in the old private history.
- Resolve dependency licensing, dashboard provenance, and provider terms.
- Generate a hash-locked dependency graph, notices, and a release SBOM.
- Enable private vulnerability reporting, secret push protection, and required
  CI on the new repository.

## Next — reproducible operations

- Pin Python and container base images to reviewed immutable digests.
- Build an immutable release directory with an atomic active-version pointer.
- Make rollback restore source, dependencies, and generated artifacts together.
- Reject stale files on the target host through an exact release manifest.
- Add encrypted off-host backups, restore drills, retention, and RPO/RTO notes.
- Add log rotation and snapshot retention with a minimum-known-good guard.

## Later — privacy and portability

- Add explicit per-image consent and a user-configurable crop/minimization step.
- Define operator-managed export, purge, and backup-destruction procedures.
- Add a documented local-only mode if OAuth handoff without a remote host is a
  real product requirement.
- Add provider-independent fake adapters so core deterministic workflows can be
  demonstrated without any third-party account.
- Review dashboard access patterns for a small private team, without weakening
  the single-operator default.

## Non-goals for the public export

- No hosted multi-tenant service.
- No public health-data dashboard.
- No automatic credential provisioning.
- No claim of medical validation, regulatory compliance, or provider
  endorsement.

Every roadmap item that changes behavior, infrastructure, or legal posture needs
its own tests, privacy review, and rollback plan.
