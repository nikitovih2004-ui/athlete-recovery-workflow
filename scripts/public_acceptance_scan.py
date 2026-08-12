"""Fail closed when a public export contains forbidden local/private artifacts."""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
FORBIDDEN_WORDS = ("clau" + "de", "anth" + "ropic")
FORBIDDEN_PARTS = {
    "data", "design-reference", "." + "co" + "dex", "." + "ag" + "ents", ".mcp" + ".json",
}
FORBIDDEN_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".log", ".bak", ".backup",
    ".dump", ".sql", ".pem", ".key", ".p12", ".pfx", ".jsonl",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov",
}
ALLOWED_PUBLIC_ASSET = "docs/dashboard-live-example.png"
EXPECTED_PUBLIC_ASSET_SIZE = (1808, 861)
EXPECTED_PUBLIC_ASSET_BYTES = 1_182_201
SECRET_PATTERNS = {
    "private-key": re.compile("-----BEGIN " + "(?:RSA |OPENSSH |EC |DSA )?" + "PRIVATE" + " KEY-----"),
    "telegram-token": re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{24,}\b"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "github-token": re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}


def validate_allowed_asset(path: pathlib.Path) -> list[str]:
    findings: list[str] = []
    try:
        if path.stat().st_size != EXPECTED_PUBLIC_ASSET_BYTES:
            findings.append(f"approved-asset-size:{path.relative_to(ROOT)}")
        from PIL import Image
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            if image.format != "PNG":
                findings.append(f"approved-asset-type:{path.relative_to(ROOT)}")
            if image.size != EXPECTED_PUBLIC_ASSET_SIZE:
                findings.append(f"approved-asset-dimensions:{path.relative_to(ROOT)}")
    except Exception as exc:
        findings.append(f"approved-asset-invalid:{path.relative_to(ROOT)}:{type(exc).__name__}")
    return findings


def tracked_files() -> list[pathlib.Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True,
        capture_output=True,
    )
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        folded = str(relative).lower()
        if any(word in folded for word in FORBIDDEN_WORDS):
            findings.append(f"forbidden-name:{relative}")
        if any(part.lower() in FORBIDDEN_PARTS for part in relative.parts):
            findings.append(f"forbidden-part:{relative}")
        relative_name = relative.as_posix()
        if relative_name == ALLOWED_PUBLIC_ASSET:
            findings.extend(validate_allowed_asset(path))
        elif relative.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append(f"forbidden-suffix:{relative}")
        if relative.name.startswith(".env") and relative.name != ".env.example":
            findings.append(f"environment-file:{relative}")
        if relative_name == ALLOWED_PUBLIC_ASSET:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"unreviewed-binary:{relative}")
            continue
        lowered = text.lower()
        if any(word in lowered for word in FORBIDDEN_WORDS):
            findings.append(f"forbidden-content:{relative}")
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{label}:{relative}")

    if findings:
        print("Public acceptance scan failed:")
        print("\n".join(sorted(set(findings))))
        return 1
    print("Public acceptance scan passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
