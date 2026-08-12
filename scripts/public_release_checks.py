"""Check release metadata without inspecting private runtime data."""

from __future__ import annotations

import pathlib
import re
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUIRED_DOCS = (
    "LICENSE_REVIEW.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/DEPENDENCY_POLICY.md",
    "docs/PUBLISHING_CHECKLIST.md",
)


def fail(message: str) -> int:
    print(f"Public release metadata check failed: {message}")
    return 1


def main() -> int:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").strip()
    if requirements != "-r requirements.lock":
        return fail("requirements.txt must point only to requirements.lock")

    lock_lines = [
        line.strip()
        for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lock_lines:
        return fail("requirements.lock is empty")
    pinned = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*==[^=\s]+$")
    if any(not pinned.fullmatch(line) for line in lock_lines):
        return fail("requirements.lock contains an unpinned or unsupported line")

    for relative in REQUIRED_DOCS:
        if not (ROOT / relative).is_file():
            return fail(f"missing required release document: {relative}")

    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    for marker in ("public_acceptance_scan.py", "public_release_checks.py", "unittest discover"):
        if marker not in workflow:
            return fail(f"CI workflow is missing {marker}")

    print("Public release metadata check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
