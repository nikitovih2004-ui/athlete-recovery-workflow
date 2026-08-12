"""Create a consistent, compressed local SQLite backup with retention."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "data" / "whoop.db"
DEFAULT_DIR = HERE / "data" / "backups"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prune(destination: Path, retain: int) -> None:
    backups = sorted(destination.glob("whoop-*.db.gz"), key=lambda path: path.name, reverse=True)
    for old in backups[retain:]:
        old.unlink(missing_ok=True)
        old.with_suffix("").with_suffix(".json").unlink(missing_ok=True)


def create_backup(destination: Path, retain: int, dry_run: bool = False) -> Path:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = destination / f"whoop-{stamp}.db.gz"
    if dry_run:
        return target

    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    with tempfile.TemporaryDirectory(dir=destination) as temp_dir:
        snapshot = Path(temp_dir) / "whoop.db"
        source = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
        source.execute("PRAGMA busy_timeout = 30000")
        target_db = sqlite3.connect(snapshot)
        try:
            source.backup(target_db)
            if target_db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite backup quick_check failed")
        finally:
            target_db.close()
            source.close()

        compressed_temp = Path(temp_dir) / target.name
        with snapshot.open("rb") as raw, compressed_temp.open("wb") as destination_file:
            with gzip.GzipFile(
                filename=target.name, mode="wb", compresslevel=9,
                fileobj=destination_file,
            ) as compressed:
                shutil.copyfileobj(raw, compressed)
            destination_file.flush()
            os.fsync(destination_file.fileno())
        os.replace(compressed_temp, target)

    manifest = target.with_suffix("").with_suffix(".json")
    manifest_payload = json.dumps(
        {
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "database": DB_PATH.name,
            "compressed_file": target.name,
            "sha256": sha256(target),
        },
        indent=2,
    )
    fd, manifest_temp_name = tempfile.mkstemp(
        prefix=".backup-manifest-", suffix=".tmp", dir=destination
    )
    manifest_temp = Path(manifest_temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(manifest_payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(manifest_temp, manifest)
    finally:
        manifest_temp.unlink(missing_ok=True)
    try:
        directory_fd = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Windows does not permit opening a directory descriptor. Production
        # is Linux, where the directory fsync is mandatory and succeeds.
        if os.name != "nt":
            raise
    os.chmod(target, 0o600)
    os.chmod(manifest, 0o600)
    prune(destination, retain)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--retain", type=int, default=14)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.retain < 1:
        raise SystemExit("--retain must be at least 1")

    target = create_backup(args.destination, args.retain, args.dry_run)
    print(("Would create " if args.dry_run else "Created ") + str(target))


if __name__ == "__main__":
    main()
