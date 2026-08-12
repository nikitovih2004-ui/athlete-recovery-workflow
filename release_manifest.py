"""Build and verify a content-addressed, Git-provenanced release manifest.

The VPS never needs Git: it receives this manifest with the runtime artifact and
is verified against its SHA-256 inventory before the service is restarted.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess

import conversation_contract


MANIFEST_NAME = "release-manifest.json"
FORMAT_VERSION = 1


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=False,
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError("Git provenance check failed.")
    return result.stdout.strip()


def require_clean_committed_head(root: Path) -> tuple[str, str]:
    """Fail closed unless deployment is exactly a clean committed Git HEAD."""
    if _git(root, "status", "--porcelain"):
        raise RuntimeError("Refusing deployment from a dirty Git worktree.")
    commit = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if len(commit) != 40 or branch in {"", "HEAD"}:
        raise RuntimeError("Refusing deployment without a named committed branch.")
    return commit, branch


def require_descends_from(root: Path, deployed_commit: str, candidate_commit: str) -> None:
    """Reject a candidate that would move production off its recorded lineage."""
    for value in (deployed_commit, candidate_commit):
        if (
            not isinstance(value, str)
            or len(value) != 40
            or any(char not in "0123456789abcdef" for char in value.lower())
        ):
            raise RuntimeError("Release lineage contains an invalid Git commit.")
    result = subprocess.run(
        [
            "git", "-C", str(root), "merge-base", "--is-ancestor",
            deployed_commit, candidate_commit,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 1:
        raise RuntimeError(
            "Refusing deployment: candidate is not a descendant of the "
            f"recorded production commit {deployed_commit[:12]}."
        )
    if result.returncode != 0:
        raise RuntimeError(
            "Cannot prove release lineage; the recorded production commit "
            "is unavailable in this checkout."
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(root: Path, runtime_files: list[Path]) -> dict:
    """Return a deterministic manifest for all files that form the runtime tree."""
    commit, branch = require_clean_committed_head(root)
    files = {}
    for path in sorted(set(runtime_files)):
        relative = path.relative_to(root).as_posix()
        files[relative] = sha256_file(path)
    if not files:
        raise RuntimeError("Release manifest has no runtime files.")
    return {
        "format_version": FORMAT_VERSION,
        "git_commit": commit,
        "git_branch": branch,
        "built_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "schema": {
            "conversation_contract": conversation_contract.SCHEMA_VERSION,
            "migration_strategy": "additive-idempotent-v1",
        },
        "files": files,
    }


def serialize(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def validate_tree(root: Path, manifest: dict) -> list[str]:
    """Return paths whose absence or bytes diverge from the signed inventory."""
    if manifest.get("format_version") != FORMAT_VERSION or not isinstance(manifest.get("files"), dict):
        return ["invalid_manifest"]
    mismatches = []
    for relative, expected in manifest["files"].items():
        path = root / relative
        if not isinstance(relative, str) or not isinstance(expected, str) or len(expected) != 64:
            mismatches.append(str(relative))
        elif not path.is_file() or sha256_file(path) != expected:
            mismatches.append(relative)
    return mismatches
