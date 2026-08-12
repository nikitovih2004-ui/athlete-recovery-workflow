# Dependency and build policy

This export keeps dependency review explicit. `requirements.txt` delegates to
the reviewed snapshot in `requirements.lock`; the snapshot is intentionally
checked into the repository so a fresh test run does not silently resolve a
different dependency graph.

Before a public release or a container distribution:

1. Regenerate the lock from the reviewed direct requirements on a supported
   Python version, with hashes and platform scope recorded.
2. Run the complete offline test suite, a vulnerability audit, and a license
   review against that exact graph.
3. Regenerate `THIRD_PARTY_NOTICES.md` from the lock and include the required
   license texts in any redistributed wheel, image, or bundle.
4. Pin container base images by reviewed digest and attach a release SBOM.
5. Review dependency updates as source changes; do not update them directly on
   a production host.

The current lock is an acceptance-test snapshot, not a claim that the release
gate is complete. It has no embedded hashes yet, and the project license is
deliberately unresolved; see [LICENSE_REVIEW.md](../LICENSE_REVIEW.md).
