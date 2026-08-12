"""Deploy the VPS safely without copying local secrets or health data."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import shlex

import paramiko
from dotenv import load_dotenv
import release_manifest

try:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

REMOTE_DIR = os.environ.get("VPS_REMOTE_DIR", "/opt/whoop-workouts").rstrip("/")
VPS_HOST = (os.environ.get("VPS_HOST") or os.environ.get("VPS_IP") or "").strip()
VPS_PORT = int(os.environ.get("VPS_PORT", "22"))
VPS_USER = os.environ.get("VPS_USER", "root").strip()
VPS_PASSWORD = os.environ.get("VPS_PASSWORD", "").strip()
VPS_SSH_KEY = os.environ.get("VPS_SSH_KEY", "").strip() or None
VPS_KNOWN_HOSTS = Path(os.environ.get("VPS_KNOWN_HOSTS", Path.home() / ".ssh" / "known_hosts")).expanduser()
SERVICE_USER = os.environ.get("WHOOP_SERVICE_USER", "whoop").strip()

SKIP_NAMES = {
    ".env", "tokens.json", "tokens.json.lock", "tokens.json.transferred",
    "tokens.json.refresh-ambiguous", "token_rotation_audit.jsonl",
    "oauth_alert_state.json", "oauth_runtime_state.json",
    "dashboard.html", "deploy.py", "temp_cron.txt",
    "AGENTS.md", ".mcp.json", "WORKSPACE_NOTES.md", ".tool-config.json", "EXAMPLE.mp4",
    "PHASE2_SESSION_HANDOFF.md", "PRIVATE_HANDOFF.md",
}
SKIP_DIRS = {
    ".git", ".codex", ".agents", ".local-agent", ".local-state", ".agent-state", ".private-tools", ".github", "venv", ".venv",
    "__pycache__", "data", "backups", "design-reference", "tests", "New folder", "graphify-out",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".cmd", ".ps1", ".vbs", ".mp4"}
SKIP_BACKUP_SUFFIXES = (".db.gz", ".sqlite.gz", ".sqlite3.gz")
REMOTE_RELEASE_MANIFEST = "release-manifest.json"


def validate_config() -> None:
    if not VPS_HOST:
        raise RuntimeError("Set VPS_HOST (or the legacy VPS_IP) in .env before deploying.")
    if not VPS_PASSWORD and not VPS_SSH_KEY:
        raise RuntimeError("Set VPS_PASSWORD or VPS_SSH_KEY in .env before deploying.")
    if not VPS_KNOWN_HOSTS.exists():
        raise RuntimeError(
            f"Known-hosts file is missing: {VPS_KNOWN_HOSTS}. Verify and pin the VPS SSH host key first."
        )


def connect() -> paramiko.SSHClient:
    validate_config()
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.load_host_keys(str(VPS_KNOWN_HOSTS))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        VPS_HOST,
        port=VPS_PORT,
        username=VPS_USER,
        password=VPS_PASSWORD or None,
        key_filename=VPS_SSH_KEY,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        allow_agent=False,
        look_for_keys=bool(VPS_SSH_KEY),
    )
    return client


def run(client: paramiko.SSHClient, command: str) -> str:
    print(f"Running: {command}")
    _, stdout, stderr = client.exec_command(command)
    status = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace").strip()
    err = stderr.read().decode("utf-8", errors="replace").strip()
    if out:
        print(out)
    if err:
        print(err)
    if status:
        raise RuntimeError(f"Remote command failed with exit status {status}: {command}")
    return out


def is_excluded(path: Path) -> bool:
    relative = path.relative_to(HERE)
    return (
        any(part in SKIP_DIRS for part in relative.parts[:-1])
        or path.name in SKIP_NAMES
        or path.suffix.lower() in SKIP_SUFFIXES
        or path.name.lower().endswith(SKIP_BACKUP_SUFFIXES)
    )


def ensure_remote_dir(sftp: paramiko.SFTPClient, path: str) -> None:
    current = "/"
    for part in PurePosixPath(path).parts:
        if part in {"/", ""}:
            continue
        current = posixpath.join(current, part)
        try:
            sftp.stat(current)
        except IOError:
            sftp.mkdir(current)


def upload_candidates() -> list[Path]:
    return sorted(
        path for path in HERE.rglob("*")
        if path.is_file() and not is_excluded(path)
    )


def _sha256_stream(source) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def changed_uploads(sftp: paramiko.SFTPClient, candidates: list[Path]) -> list[Path]:
    changed = []
    for local_path in candidates:
        relative = local_path.relative_to(HERE).as_posix()
        remote_path = posixpath.join(REMOTE_DIR, relative)
        try:
            with sftp.file(remote_path, "rb") as remote_file:
                remote_hash = _sha256_stream(remote_file)
        except IOError:
            remote_hash = None
        with local_path.open("rb") as local_file:
            local_hash = _sha256_stream(local_file)
        if local_hash != remote_hash:
            changed.append(local_path)
    return changed


def upload_project(sftp: paramiko.SFTPClient, paths: list[Path] | None = None) -> None:
    for local_path in paths if paths is not None else upload_candidates():
        relative = local_path.relative_to(HERE).as_posix()
        remote_path = posixpath.join(REMOTE_DIR, relative)
        ensure_remote_dir(sftp, posixpath.dirname(remote_path))
        print(f"Uploading {relative}...")
        sftp.put(str(local_path), remote_path)


def _copy_remote_file(sftp: paramiko.SFTPClient, source: str, destination: str) -> None:
    ensure_remote_dir(sftp, posixpath.dirname(destination))
    with sftp.file(source, "rb") as remote_source:
        with sftp.file(destination, "wb") as remote_destination:
            for chunk in iter(lambda: remote_source.read(1024 * 1024), b""):
                remote_destination.write(chunk)


def create_source_snapshot(
    sftp: paramiko.SFTPClient,
    changed_paths: list[Path],
    timestamp: str,
) -> dict:
    snapshot_root = posixpath.join(REMOTE_DIR, "data", "backups", f"predeploy_{timestamp}")
    ensure_remote_dir(sftp, posixpath.join(snapshot_root, "files"))
    entries = []
    for local_path in changed_paths:
        relative = local_path.relative_to(HERE).as_posix()
        remote_path = posixpath.join(REMOTE_DIR, relative)
        snapshot_path = posixpath.join(snapshot_root, "files", relative)
        try:
            sftp.stat(remote_path)
            existed = True
            _copy_remote_file(sftp, remote_path, snapshot_path)
        except IOError:
            existed = False
        entries.append({"path": relative, "existed": existed})
    # Preserve the prior provenance document alongside the source rollback
    # candidate.  It is not local source and therefore must be snapshotted
    # explicitly.
    manifest_path = posixpath.join(REMOTE_DIR, REMOTE_RELEASE_MANIFEST)
    snapshot_manifest_path = posixpath.join(snapshot_root, "files", REMOTE_RELEASE_MANIFEST)
    try:
        sftp.stat(manifest_path)
        _copy_remote_file(sftp, manifest_path, snapshot_manifest_path)
        entries.append({"path": REMOTE_RELEASE_MANIFEST, "existed": True})
    except IOError:
        entries.append({"path": REMOTE_RELEASE_MANIFEST, "existed": False})
    # dashboard.html is generated and intentionally excluded from source
    # uploads/manifests.  It still must be part of the rollback unit: a failed
    # new builder may already have atomically published a complete but invalid
    # artifact before a later deployment gate rejects the release.
    dashboard_relative = "dashboard.html"
    dashboard_path = posixpath.join(REMOTE_DIR, dashboard_relative)
    dashboard_snapshot_path = posixpath.join(
        snapshot_root, "files", dashboard_relative
    )
    try:
        sftp.stat(dashboard_path)
        _copy_remote_file(sftp, dashboard_path, dashboard_snapshot_path)
        entries.append({"path": dashboard_relative, "existed": True})
    except IOError:
        entries.append({"path": dashboard_relative, "existed": False})
    manifest = {"created_at": timestamp, "files": entries}
    write_remote_file(
        sftp,
        posixpath.join(snapshot_root, "manifest.json"),
        json.dumps(manifest, indent=2),
    )
    return {"root": snapshot_root, "manifest": manifest}


def rollback_sources(sftp: paramiko.SFTPClient, snapshot: dict) -> None:
    snapshot_root = snapshot["root"]
    for entry in snapshot["manifest"]["files"]:
        relative = entry["path"]
        remote_path = posixpath.join(REMOTE_DIR, relative)
        if entry["existed"]:
            snapshot_path = posixpath.join(snapshot_root, "files", relative)
            _copy_remote_file(sftp, snapshot_path, remote_path)
        else:
            try:
                sftp.remove(remote_path)
            except IOError:
                pass


def write_remote_file(sftp: paramiko.SFTPClient, remote_path: str, content: str) -> None:
    with sftp.file(remote_path, "w") as remote_file:
        remote_file.write(content)


def upload_release_manifest(sftp: paramiko.SFTPClient, manifest: dict) -> None:
    write_remote_file(
        sftp, posixpath.join(REMOTE_DIR, REMOTE_RELEASE_MANIFEST),
        release_manifest.serialize(manifest),
    )


def validate_release_lineage(sftp: paramiko.SFTPClient, candidate: dict) -> dict:
    """Fail before backup or upload when a checkout would downgrade production."""
    path = posixpath.join(REMOTE_DIR, REMOTE_RELEASE_MANIFEST)
    try:
        with sftp.file(path, "r") as source:
            deployed = json.loads(source.read().decode("utf-8"))
    except (IOError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            "Production release manifest is unavailable; refusing an "
            "unproven source replacement."
        ) from exc
    deployed_commit = deployed.get("git_commit")
    candidate_commit = candidate.get("git_commit")
    release_manifest.require_descends_from(
        HERE, deployed_commit, candidate_commit,
    )
    return {
        "deployed_commit": deployed_commit,
        "candidate_commit": candidate_commit,
    }


def verify_remote_manifest(sftp: paramiko.SFTPClient, manifest: dict) -> None:
    """Fail closed before restart if any uploaded runtime byte differs."""
    mismatches = []
    for relative, expected in manifest["files"].items():
        try:
            with sftp.file(posixpath.join(REMOTE_DIR, relative), "rb") as remote_file:
                actual = _sha256_stream(remote_file)
        except IOError:
            actual = None
        if actual != expected:
            mismatches.append(relative)
    if mismatches:
        raise RuntimeError("Remote release manifest mismatch: " + ", ".join(mismatches[:10]))


def verify_service_execstart(client: paramiko.SSHClient) -> None:
    value = run(client, "systemctl show whoop-bot.service -p ExecStart --value")
    expected = f"{REMOTE_DIR}/venv/bin/python -u {REMOTE_DIR}/telegram_bot.py"
    if expected not in value:
        raise RuntimeError("whoop-bot.service ExecStart is not the deployed runtime path.")


def preflight(client: paramiko.SSHClient) -> None:
    run(client, f"test -d {shlex.quote(REMOTE_DIR)}")
    run(client, f"test -f {shlex.quote(REMOTE_DIR)}/.env")
    run(client, f"test -f {shlex.quote(REMOTE_DIR)}/data/whoop.db")
    run(client, f"test -f {shlex.quote(REMOTE_DIR)}/dashboard.html")
    run(client, "systemctl cat whoop-bot.service >/dev/null")


def _remote_python(client: paramiko.SSHClient, code: str, as_service_user: bool = False) -> str:
    # `-c` puts the SSH session's actual login cwd on sys.path, not REMOTE_DIR —
    # an explicit `cd` guarantees local-module imports (morning_context,
    # conversation_store, ...) resolve regardless of the remote shell's default.
    python = f"{REMOTE_DIR}/venv/bin/python"
    inner = f"{shlex.quote(python)} -c {shlex.quote(code)}"
    if as_service_user:
        inner = f"runuser -u {shlex.quote(SERVICE_USER)} -- {inner}"
    command = f"cd {shlex.quote(REMOTE_DIR)} && {inner}"
    return run(client, command)


def run_backup(client: paramiko.SSHClient) -> dict:
    command = (
        f"{shlex.quote(REMOTE_DIR)}/venv/bin/python "
        f"{shlex.quote(REMOTE_DIR)}/backup_database.py "
        f"--destination {shlex.quote(REMOTE_DIR)}/data/backups --retain 14"
    )
    output = run(client, command)
    created_lines = [line for line in output.splitlines() if line.startswith("Created ")]
    if len(created_lines) != 1:
        raise RuntimeError("Backup command did not report exactly one created backup.")
    path = created_lines[0].removeprefix("Created ").strip()
    metadata = run(
        client,
        f"test -s {shlex.quote(path)} && sha256sum {shlex.quote(path)} "
        f"&& stat -c %s {shlex.quote(path)}",
    ).splitlines()
    if len(metadata) != 2:
        raise RuntimeError("Backup metadata validation failed.")
    sha256 = metadata[0].split()[0]
    size = int(metadata[1])
    if len(sha256) != 64 or size <= 0:
        raise RuntimeError("Backup hash or size is invalid.")
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return {"path": path, "sha256": sha256, "size": size, "timestamp": timestamp}


def provision(client: paramiko.SSHClient) -> None:
    run(client, "timedatectl set-timezone Europe/Kyiv")
    run(client, "while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 || fuser /var/lib/apt/lists/lock >/dev/null 2>&1; do echo 'Waiting for apt/dpkg lock...'; sleep 3; done")
    # The VPS receives a verified artifact and release manifest; it never needs
    # Git for deploy, provenance, or rollback.
    run(client, "export DEBIAN_FRONTEND=noninteractive && apt-get update && apt-get install -y python3 python3-venv")
    run(client, f"python3 -m venv {shlex.quote(REMOTE_DIR)}/venv")
    run(client, f"{shlex.quote(REMOTE_DIR)}/venv/bin/pip install --upgrade pip")
    run(client, f"{shlex.quote(REMOTE_DIR)}/venv/bin/pip install -r {shlex.quote(REMOTE_DIR)}/requirements.txt")
    run(client, f"test -f {shlex.quote(REMOTE_DIR)}/tokens.json || echo 'WARNING: tokens.json is absent; run auth.py securely on the VPS before the next WHOOP sync.'")
    run(client, f"id -u {shlex.quote(SERVICE_USER)} >/dev/null 2>&1 || useradd --system --home-dir {shlex.quote(REMOTE_DIR)} --shell /usr/sbin/nologin --no-create-home {shlex.quote(SERVICE_USER)}")
    run(client, f"mkdir -p {shlex.quote(REMOTE_DIR)}/data/backups && chown -R {shlex.quote(SERVICE_USER)}:{shlex.quote(SERVICE_USER)} {shlex.quote(REMOTE_DIR)}")
    run(client, f"chmod 0750 {shlex.quote(REMOTE_DIR)} {shlex.quote(REMOTE_DIR)}/data && chmod 0700 {shlex.quote(REMOTE_DIR)}/data/backups")
    run(client, f"chmod 0600 {shlex.quote(REMOTE_DIR)}/.env {shlex.quote(REMOTE_DIR)}/tokens.json")


def service_unit_content() -> str:
    return f"""[Unit]
Description=WHOOP Workouts Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={SERVICE_USER}
Group={SERVICE_USER}
UMask=0077
WorkingDirectory={REMOTE_DIR}
ExecStart={REMOTE_DIR}/venv/bin/python -u {REMOTE_DIR}/telegram_bot.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


def configure_service(client: paramiko.SSHClient, sftp: paramiko.SFTPClient) -> bool:
    service = service_unit_content()
    unit_path = "/etc/systemd/system/whoop-bot.service"
    try:
        with sftp.file(unit_path, "r") as existing:
            if existing.read().decode("utf-8", errors="replace") == service:
                run(client, "systemctl enable whoop-bot.service")
                return False
    except IOError:
        pass
    temp_path = "/tmp/whoop-bot.service"
    write_remote_file(sftp, temp_path, service)
    run(client, f"install -m 0644 {temp_path} /etc/systemd/system/whoop-bot.service && rm -f {temp_path}")
    run(client, "systemctl daemon-reload")
    run(client, "systemctl enable whoop-bot.service")
    return True


def stop_service(client: paramiko.SSHClient) -> None:
    run(
        client,
        "systemctl stop whoop-bot.service && "
        "test \"$(systemctl is-active whoop-bot.service)\" = inactive",
    )


def morning_context_row_count(client: paramiko.SSHClient) -> int:
    code = f"""
import sqlite3
path = {str(REMOTE_DIR + '/data/whoop.db')!r}
conn = sqlite3.connect(f'file:{{path}}?mode=ro', uri=True)
conn.execute('PRAGMA query_only=ON')
exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='morning_context'").fetchone()
print(conn.execute('SELECT COUNT(*) FROM morning_context').fetchone()[0] if exists else 0)
conn.close()
"""
    return int(_remote_python(client, code).strip())


def daily_log_precheck(client: paramiko.SSHClient) -> dict:
    """Read-only check that daily_log is already in the expected shape.

    conversation_store.connect() -> daily_log.connect() -> daily_log.ensure_table()
    silently renames/recreates daily_log if its columns don't match
    (date, notes, updated_at). That must never happen unattended during a
    migration; this precheck fails closed before run_migration executes.
    """
    code = f"""
import json, sqlite3
path = {str(REMOTE_DIR + '/data/whoop.db')!r}
conn = sqlite3.connect(f'file:{{path}}?mode=ro', uri=True)
conn.execute('PRAGMA query_only=ON')
exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_log'").fetchone() is not None
columns = [row[1] for row in conn.execute('PRAGMA table_info(daily_log)')] if exists else []
conn.close()
print(json.dumps({{'exists': exists, 'columns': columns}}))
"""
    result = json.loads(_remote_python(client, code))
    if result["exists"] and result["columns"] != ["date", "notes", "updated_at"]:
        raise RuntimeError(
            "daily_log precheck failed: unexpected columns "
            f"{result['columns']} (expected ['date', 'notes', 'updated_at']); "
            "refusing to run a migration that would rewrite this table."
        )
    return result


def run_migration(client: paramiko.SSHClient) -> None:
    # Every migration is additive and idempotent; Phase 2 DDL is explicit here,
    # never triggered by a read request.
    code = (
        "import morning_context, morning_observability, conversation_store, phase2_store, report_delivery, workouts_db; "
        "conn = morning_context._connect(); conn.close(); "
        "co = morning_observability._connect(); co.close(); "
        "c2 = conversation_store.connect(); phase2_store.migrate(c2); c2.close(); "
        "c3 = workouts_db.connect(); c3.close(); "
        "c4 = report_delivery._connect(); c4.close()"
    )
    _remote_python(client, code, as_service_user=True)


REQUIRED_INDEX_NAME = "idx_morning_context_question_message_id"


def validate_migration(client: paramiko.SSHClient, expected_rows: int) -> dict:
    code = f"""
import json, sqlite3
path = {str(REMOTE_DIR + '/data/whoop.db')!r}
conn = sqlite3.connect(f'file:{{path}}?mode=ro', uri=True)
conn.execute('PRAGMA query_only=ON')
columns = [row[1] for row in conn.execute('PRAGMA table_info(morning_context)')]
index_list = [
    {{'name': row[1], 'unique': bool(row[2])}}
    for row in conn.execute('PRAGMA index_list(morning_context)')
]
index_sql_row = conn.execute(
    "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
    ({REQUIRED_INDEX_NAME!r},),
).fetchone()
tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
phase2 = {{}}
for table in ('conversation_actions', 'pending_actions',
              'conversation_sessions', 'daily_factor_observations',
              'factor_extraction_jobs', 'daily_context_entries',
              'daily_context_projection_state'):
    info_rows = list(conn.execute(f'PRAGMA table_info({{table}})'))
    columns_for_table = [row[1] for row in info_rows]
    table_info = [
        {{'name': row[1], 'type': row[2], 'notnull': bool(row[3]),
          'default': row[4], 'pk': row[5]}}
        for row in info_rows
    ]
    indexes_for_table = []
    for idx in conn.execute(f'PRAGMA index_list({{table}})'):
        indexes_for_table.append({{
            'name': idx[1], 'unique': bool(idx[2]),
            'columns': [r[2] for r in conn.execute(f'PRAGMA index_info({{idx[1]}})')],
        }})
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    phase2[table] = {{'columns': columns_for_table, 'table_info': table_info,
                      'indexes': indexes_for_table,
                      'sql': sql_row[0] if sql_row else None}}
result = {{
    'columns': columns,
    'index_list': index_list,
    'index_sql': index_sql_row[0] if index_sql_row else None,
    'tables': tables,
    'rows': conn.execute('SELECT COUNT(*) FROM morning_context').fetchone()[0],
    'quick_check': conn.execute('PRAGMA quick_check').fetchone()[0],
    'phase2': phase2,
    'cardio_columns': {{
        row[1]: (row[2] or '').upper()
        for row in conn.execute('PRAGMA table_info(cardio_exercises)')
    }},
    'report_delivery_columns': [
        row[1] for row in conn.execute('PRAGMA table_info(report_deliveries)')
    ],
    'report_delivery_indexes': [
        row[1] for row in conn.execute('PRAGMA index_list(report_deliveries)')
    ],
    'morning_observability_columns': [
        row[1] for row in conn.execute(
            'PRAGMA table_info(morning_pipeline_events)'
        )
    ],
    'morning_observability_indexes': [
        row[1] for row in conn.execute(
            'PRAGMA index_list(morning_pipeline_events)'
        )
    ],
    'correction_trigger_sql': (
        conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='trg_conversation_actions_success_immutable'"
        ).fetchone() or [None]
    )[0],
}}
conn.close()
print(json.dumps(result))
"""
    result = json.loads(_remote_python(client, code))
    required_columns = {
        "question_message_id", "question_claimed_at",
        "analysis_mode", "analysis_claimed_at", "analysis_available_at",
        "analysis_attempt_count",
    }
    if not required_columns.issubset(result["columns"]):
        raise RuntimeError("Migration validation failed: expected columns are missing.")

    index_entry = next(
        (idx for idx in result["index_list"] if idx["name"] == REQUIRED_INDEX_NAME), None
    )
    if index_entry is None:
        raise RuntimeError("Migration validation failed: expected index is missing.")
    if not index_entry["unique"]:
        raise RuntimeError("Migration validation failed: expected index is not UNIQUE.")
    index_sql = (result.get("index_sql") or "").replace("\n", " ")
    index_sql_compact = " ".join(index_sql.split())
    if "(question_message_id)" not in index_sql_compact:
        raise RuntimeError(
            "Migration validation failed: expected index is not on question_message_id."
        )
    if "WHERE question_message_id IS NOT NULL" not in index_sql_compact:
        raise RuntimeError(
            "Migration validation failed: expected index is not the partial index on "
            "question_message_id IS NOT NULL."
        )

    if result["rows"] != expected_rows:
        raise RuntimeError(
            f"Migration validation failed: row count changed from {expected_rows} to {result['rows']}."
        )
    if result["quick_check"] != "ok":
        raise RuntimeError("Migration validation failed: SQLite quick_check is not ok.")
    expected_cardio_columns = {
        "strain": "REAL", "max_hr": "INTEGER", "steps": "INTEGER",
    }
    actual_cardio_columns = result.get("cardio_columns") or {}
    if any(actual_cardio_columns.get(name) != type_name
           for name, type_name in expected_cardio_columns.items()):
        raise RuntimeError(
            "Migration validation failed: cardio extraction columns are missing "
            "or have unexpected types."
        )
    if "correct_activity" not in (result.get("correction_trigger_sql") or ""):
        raise RuntimeError(
            "Migration validation failed: correction actions are not protected "
            "by the succeeded-action immutability trigger."
        )
    # Additive conversation tables must exist after the migration (they are only
    # created, never required by the legacy morning_context checks above).
    required_tables = {
        "conversation_actions", "pending_actions",
        "conversation_sessions", "daily_factor_observations",
        "factor_extraction_jobs", "daily_context_entries",
        "daily_context_projection_state", "report_deliveries",
        "morning_pipeline_events",
    }
    if not required_tables.issubset(set(result.get("tables", []))):
        raise RuntimeError("Migration validation failed: conversation tables are missing.")
    required_delivery_columns = {
        "delivery_key", "report_kind", "payload", "payload_sha256",
        "total_chunks", "next_chunk", "status", "claim_token", "claimed_at",
        "delivered_at", "created_at", "updated_at",
    }
    if not required_delivery_columns.issubset(
        set(result.get("report_delivery_columns") or [])
    ):
        raise RuntimeError(
            "Migration validation failed: report delivery columns are incomplete."
        )
    required_observability_columns = {
        "event_id", "pipeline_date", "run_id", "stage", "started_at",
        "finished_at", "outcome", "reason", "duration_ms", "details_json",
    }
    if required_observability_columns != set(
        result.get("morning_observability_columns") or []
    ):
        raise RuntimeError(
            "Migration validation failed: morning observability columns "
            "are missing or unexpected."
        )
    required_observability_indexes = {
        "idx_morning_pipeline_date_stage", "idx_morning_pipeline_run",
    }
    if not required_observability_indexes.issubset(
        set(result.get("morning_observability_indexes") or [])
    ):
        raise RuntimeError(
            "Migration validation failed: morning observability indexes "
            "are incomplete."
        )
    if "idx_report_deliveries_status" not in (
        result.get("report_delivery_indexes") or []
    ):
        raise RuntimeError(
            "Migration validation failed: report delivery index is missing."
        )

    expected_columns = {
        "conversation_actions": {
            "response_kind", "response_text", "reply_delivery_status",
            "reply_message_id", "reply_attempt_count",
            "response_claimed_at", "processing_token", "processing_fence",
            "processing_claimed_at", "processing_claim_expires_at",
        },
        "pending_actions": {
            "claimed_by_action_id", "claimed_at", "claim_expires_at",
        },
        "conversation_sessions": {
            "source", "chat_id", "user_id", "state_json", "version",
            "expires_at", "created_at", "updated_at",
        },
        "daily_factor_observations": {
            "observation_id", "context_date", "factor_key", "state",
            "extractor_version", "confidence", "source_key",
            "created_at", "updated_at", "job_id", "projection_hash",
            "projection_revision", "is_current",
        },
        "factor_extraction_jobs": {
            "job_id", "context_date", "projection_hash", "projection_revision",
            "extractor_version", "origin_action_id", "source_key", "status",
            "attempt_count", "available_at", "lease_token", "lease_expires_at",
            "last_error_code", "created_at", "updated_at", "completed_at",
        },
        "daily_context_entries": {
            "entry_id", "context_date", "notes", "label", "source_key",
            "origin_action_id", "revision", "status", "supersedes_entry_id",
            "status_action_id", "content_sha256", "created_at", "updated_at",
        },
        "daily_context_projection_state": {
            "context_date", "projection_hash", "revision", "updated_at",
        },
    }
    phase2 = result.get("phase2") or {}
    for table, columns_required in expected_columns.items():
        actual = set((phase2.get(table) or {}).get("columns", []))
        if not columns_required.issubset(actual):
            raise RuntimeError(
                f"Migration validation failed: {table} columns are incomplete."
            )

    expected_new_table_info = {
        "conversation_sessions": [
            ("source", "TEXT", True, None, 1),
            ("chat_id", "TEXT", True, None, 2),
            ("user_id", "TEXT", True, "''", 3),
            ("state_json", "TEXT", True, None, 0),
            ("version", "INTEGER", True, "1", 0),
            ("expires_at", "TEXT", True, None, 0),
            ("created_at", "TEXT", True, None, 0),
            ("updated_at", "TEXT", True, None, 0),
        ],
        "daily_factor_observations": [
            ("observation_id", "TEXT", False, None, 1),
            ("context_date", "TEXT", True, None, 0),
            ("factor_key", "TEXT", True, None, 0),
            ("state", "INTEGER", True, None, 0),
            ("extractor_version", "TEXT", True, None, 0),
            ("confidence", "REAL", False, None, 0),
            ("source_key", "TEXT", True, None, 0),
            ("created_at", "TEXT", True, None, 0),
            ("updated_at", "TEXT", True, None, 0),
            ("job_id", "TEXT", False, None, 0),
            ("projection_hash", "TEXT", False, None, 0),
            ("projection_revision", "INTEGER", False, None, 0),
            ("is_current", "INTEGER", True, "1", 0),
        ],
    }
    for table, expected in expected_new_table_info.items():
        actual = [
            (item.get("name"), item.get("type"), bool(item.get("notnull")),
             item.get("default"), item.get("pk"))
            for item in (phase2.get(table) or {}).get("table_info", [])
        ]
        if actual != expected:
            raise RuntimeError(
                f"Migration validation failed: {table} exact schema mismatch."
            )

    expected_added_columns = {
        "conversation_actions": {
            "response_kind": ("TEXT", False, None, 0),
            "response_text": ("TEXT", False, None, 0),
            "reply_delivery_status": ("TEXT", False, None, 0),
            "reply_message_id": ("TEXT", False, None, 0),
            "reply_attempt_count": ("INTEGER", True, "0", 0),
            "response_claimed_at": ("TEXT", False, None, 0),
            "processing_token": ("TEXT", False, None, 0),
            "processing_fence": ("INTEGER", True, "0", 0),
            "processing_claimed_at": ("TEXT", False, None, 0),
            "processing_claim_expires_at": ("TEXT", False, None, 0),
        },
        "pending_actions": {
            "claimed_by_action_id": ("TEXT", False, None, 0),
            "claimed_at": ("TEXT", False, None, 0),
            "claim_expires_at": ("TEXT", False, None, 0),
        },
    }
    for table, expected in expected_added_columns.items():
        actual_by_name = {
            item.get("name"): (item.get("type"), bool(item.get("notnull")),
                               item.get("default"), item.get("pk"))
            for item in (phase2.get(table) or {}).get("table_info", [])
        }
        for name, shape in expected.items():
            if actual_by_name.get(name) != shape:
                raise RuntimeError(
                    f"Migration validation failed: {table}.{name} shape mismatch."
                )

    required_indexes = {
        "conversation_sessions": {"idx_conversation_sessions_expiry"},
        "daily_factor_observations": {
            "idx_daily_factor_date_key", "idx_daily_factor_current",
        },
        "factor_extraction_jobs": {
            "idx_factor_jobs_ready", "idx_factor_jobs_date_revision",
        },
        "daily_context_entries": {"idx_daily_context_entries_date_status"},
        "pending_actions": {"idx_pending_actions_claim_expiry"},
    }
    for table, names in required_indexes.items():
        actual_names = {
            item["name"] for item in (phase2.get(table) or {}).get("indexes", [])
        }
        if not names.issubset(actual_names):
            raise RuntimeError(
                f"Migration validation failed: {table} indexes are incomplete."
            )
    expected_index_shapes = {
        ("conversation_sessions", "idx_conversation_sessions_expiry"):
            (False, ["expires_at"]),
        ("daily_factor_observations", "idx_daily_factor_date_key"):
            (False, ["context_date", "factor_key"]),
        ("daily_factor_observations", "idx_daily_factor_current"):
            (False, ["context_date", "factor_key", "is_current"]),
        ("factor_extraction_jobs", "idx_factor_jobs_ready"):
            (False, ["status", "available_at", "lease_expires_at"]),
        ("factor_extraction_jobs", "idx_factor_jobs_date_revision"):
            (False, ["context_date", "projection_revision"]),
        ("daily_context_entries", "idx_daily_context_entries_date_status"):
            (False, ["context_date", "status", "revision", "entry_id"]),
        ("pending_actions", "idx_pending_actions_claim_expiry"):
            (False, ["status", "claim_expires_at"]),
    }
    for (table, name), expected in expected_index_shapes.items():
        item = next(
            (idx for idx in (phase2.get(table) or {}).get("indexes", [])
             if idx.get("name") == name), None
        )
        actual = (bool(item.get("unique")), item.get("columns")) if item else None
        if actual != expected:
            raise RuntimeError(
                f"Migration validation failed: {name} exact shape mismatch."
            )
    factor_indexes = (phase2.get("daily_factor_observations") or {}).get("indexes", [])
    if not any(item.get("unique") and item.get("columns") == ["source_key"]
               for item in factor_indexes):
        raise RuntimeError(
            "Migration validation failed: factor source_key is not uniquely indexed."
        )
    schema_sql = " ".join(
        ((phase2.get("conversation_sessions") or {}).get("sql") or "").upper().split()
    )
    factor_sql = " ".join(
        ((phase2.get("daily_factor_observations") or {}).get("sql") or "").upper().split()
    )
    for fragment in (
        "CHECK(VERSION >= 1)",
        "CHECK(LENGTH(CAST(STATE_JSON AS BLOB)) <= 8192)",
    ):
        if fragment not in schema_sql:
            raise RuntimeError("Migration validation failed: session CHECK mismatch.")
    for fragment in ("CHECK(STATE IN (0, 1))", "CONFIDENCE >= 0", "CONFIDENCE <= 1"):
        if fragment not in factor_sql:
            raise RuntimeError("Migration validation failed: factor CHECK mismatch.")
    return result


def validate_data_integrity(client: paramiko.SSHClient) -> dict:
    """Run the read-only action/domain audit before restarting production."""
    code = f"""
import json
import data_integrity
path = {str(REMOTE_DIR + '/data/whoop.db')!r}
print(json.dumps(data_integrity.audit_database(path)))
"""
    result = json.loads(_remote_python(client, code, as_service_user=True))
    if not result.get("ok"):
        codes = ", ".join(
            str(item.get("code")) for item in (result.get("issues") or [])[:10]
        )
        raise RuntimeError(f"Data integrity validation failed: {codes or 'unknown'}")
    return result


def restart_service(client: paramiko.SSHClient) -> str:
    # journalctl's --since parser rejected `date -u --iso-8601=seconds`'s
    # 'T'-separated, colon-offset output ("2026-07-12T11:20:44+00:00") in
    # production with "Failed to parse timestamp". Unix epoch seconds is
    # systemd.time(7)'s own "@<seconds>" syntax: no date/time string parsing,
    # no locale, no timezone ambiguity — there's nothing left to reject.
    since_epoch = run(client, "date -u +%s").strip()
    if not since_epoch.isdigit():
        raise RuntimeError(f"Unexpected timestamp output from date: {since_epoch!r}")
    run(client, "systemctl restart whoop-bot.service")
    return since_epoch


def validate_service_health(client: paramiko.SSHClient, since_epoch: str) -> None:
    run(client, "systemctl is-active --quiet whoop-bot.service")
    run(client, "systemctl is-enabled --quiet whoop-bot.service")
    before = int(run(client, "systemctl show whoop-bot.service -p NRestarts --value").strip())
    run(client, "sleep 3")
    run(client, "systemctl is-active --quiet whoop-bot.service")
    after = int(run(client, "systemctl show whoop-bot.service -p NRestarts --value").strip())
    if after != before:
        raise RuntimeError("Service health check failed: restart loop detected.")
    since_arg = f"@{since_epoch}"
    logs = run(
        client,
        f"journalctl -u whoop-bot.service --utc --since {shlex.quote(since_arg)} --no-pager -o cat",
    )
    if "Traceback (most recent call last)" in logs:
        raise RuntimeError("Service health check failed: traceback in recent journal.")


def recover_previous_service(client: paramiko.SSHClient) -> None:
    run(client, "systemctl restart whoop-bot.service")
    run(client, "systemctl is-active --quiet whoop-bot.service")


def build_dashboard(client: paramiko.SSHClient) -> None:
    command = (
        f"cd {shlex.quote(REMOTE_DIR)} && runuser -u {shlex.quote(SERVICE_USER)} -- "
        f"{shlex.quote(REMOTE_DIR)}/venv/bin/python {shlex.quote(REMOTE_DIR)}/build_dashboard.py"
    )
    run(client, command)
    validate_dashboard_artifact(client)


def validate_dashboard_artifact(client: paramiko.SSHClient) -> dict:
    """Hash and structurally validate the generated single-file dashboard."""
    code = """
import dashboard_contract
import hashlib, json
from pathlib import Path
path = Path('dashboard.html')
raw = path.read_bytes()
text = raw.decode('utf-8')
contract = dashboard_contract.validate_artifact(text)
print(json.dumps({
    'sha256': hashlib.sha256(raw).hexdigest(),
    'size': len(raw),
    'expansion_keys': contract['expansion_keys'],
}))
"""
    result = json.loads(_remote_python(client, code, as_service_user=True))
    if len(result.get("sha256", "")) != 64 or int(result.get("size", 0)) < 100_000:
        raise RuntimeError("Generated dashboard artifact failed hash/size validation.")
    return result


def validate_analysis_contract(client: paramiko.SSHClient) -> dict:
    """Reject a release whose canonical daily metric contract is incomplete."""
    code = """
import datetime as dt
import json
import canonical_read_model as CRM
import generate_insights

expected = {
    'recovery': ('recovery_score', '%'),
    'hrv': ('hrv_rmssd', 'ms'),
    'rhr': ('resting_hr', 'bpm'),
    'sleep_duration': ('sleep_hours', 'h'),
    'sleep_performance': ('sleep_performance', '%'),
}
actual = {
    metric: (spec['source_field'], spec['unit'])
    for metric, spec in CRM.DAILY_METRIC_SPECS.items()
}
if actual != expected:
    raise SystemExit('unknown metric mapping')
start = dt.date(2026, 1, 1)
outcomes = {}
for index in range(29):
    day = start + dt.timedelta(days=index)
    outcomes[day] = {
        'recovery_score': 60 + (index % 20),
        'hrv_rmssd': 50 + index,
        'resting_hr': 70 - (index / 2),
        'sleep_hours': 7 + (index / 100),
        'sleep_performance': 80 + (index / 2),
    }
target = start + dt.timedelta(days=28)
outcomes[target]['recovery_score'] = 100
metrics = CRM.daily_metric_context(outcomes, target)
required = {
    'metric', 'source_field', 'current_value', 'unit', 'baseline_value',
    'baseline_method', 'period_start', 'period_end', 'valid_observations',
    'current_outcome_date', 'comparison_status',
}
for metric in metrics:
    missing = required - set(metric)
    if missing:
        raise SystemExit('missing prompt contract fields: ' + ','.join(sorted(missing)))
    if metric['valid_observations'] != 28:
        raise SystemExit('invalid baseline observation count')
    if metric['period_end'] != (target - dt.timedelta(days=1)).isoformat():
        raise SystemExit('current value entered baseline period')
recovery = next(item for item in metrics if item['metric'] == 'recovery')
if recovery['current_value'] == recovery['baseline_value']:
    raise SystemExit('current value equals synthetic baseline')
prompt = generate_insights.build_llm_prompt(
    'STRUCTURED_METRIC_CONTEXT_JSON:\\n' +
    generate_insights.metric_context_json(metrics)
)
if ('average over N valid days from DATE to DATE' not in prompt
        or 'STRUCTURED_METRIC_CONTEXT_JSON' not in prompt):
    raise SystemExit('analysis prompt contract missing')
print(json.dumps({'ok': True, 'mapping': actual, 'fields': sorted(required)}))
"""
    result = json.loads(_remote_python(client, code, as_service_user=True))
    if result.get("ok") is not True:
        raise RuntimeError("Analysis contract validation failed.")
    return result


def cron_content() -> str:
    return "\n".join((
        f"7,22,37,52 6-23 * * * cd {REMOTE_DIR} && {REMOTE_DIR}/venv/bin/python {REMOTE_DIR}/morning_flow.py >> {REMOTE_DIR}/data/cron.log 2>&1",
        f"0 21 * * * cd {REMOTE_DIR} && {REMOTE_DIR}/venv/bin/python {REMOTE_DIR}/send_reminder.py >> {REMOTE_DIR}/data/cron.log 2>&1",
        f"15 2 * * * cd {REMOTE_DIR} && {REMOTE_DIR}/venv/bin/python {REMOTE_DIR}/backup_database.py >> {REMOTE_DIR}/data/cron.log 2>&1",
        "",
    ))


def configure_cron(client: paramiko.SSHClient, sftp: paramiko.SFTPClient) -> None:
    cron = cron_content()
    temp_path = "/tmp/whoop-crontab"
    write_remote_file(sftp, temp_path, cron)
    run(client, f"crontab -u {shlex.quote(SERVICE_USER)} {temp_path} && rm -f {temp_path}")
    root_cleanup = f"(crontab -l 2>/dev/null || true) | grep -v -F {shlex.quote(REMOTE_DIR)} > /tmp/whoop-root-cron; if [ -s /tmp/whoop-root-cron ]; then crontab /tmp/whoop-root-cron; else crontab -r 2>/dev/null || true; fi; rm -f /tmp/whoop-root-cron"
    run(client, root_cleanup)


def suspend_project_cron(client: paramiko.SSHClient) -> None:
    """Remove only this project's scheduled writers while active code changes."""
    command = (
        f"(crontab -u {shlex.quote(SERVICE_USER)} -l 2>/dev/null || true) "
        f"| grep -v -F {shlex.quote(REMOTE_DIR)} > /tmp/whoop-service-cron; "
        f"if [ -s /tmp/whoop-service-cron ]; then "
        f"crontab -u {shlex.quote(SERVICE_USER)} /tmp/whoop-service-cron; "
        f"else crontab -u {shlex.quote(SERVICE_USER)} -r 2>/dev/null || true; fi; "
        "rm -f /tmp/whoop-service-cron"
    )
    run(client, command)


def assert_no_project_writers(client: paramiko.SSHClient) -> None:
    """Fail before upload if a cron writer was already running."""
    # The SSH server may execute this through ``sh -c``.  A plain pattern is
    # then present in the wrapper's own argv and pgrep can report the wrapper
    # as a writer.  Character classes keep the regexp equivalent while making
    # it absent verbatim from the probing process command line.
    scripts = "[m]orning_flow.py|[s]end_reminder.py|[b]ackup_database.py|[w]eekly_report.py"
    command = (
        f"if pgrep -u {shlex.quote(SERVICE_USER)} -f {shlex.quote(scripts)} "
        "> /dev/null; then echo 'project writer is still running' >&2; exit 1; fi"
    )
    run(client, command)


def deploy_project(client: paramiko.SSHClient, sftp: paramiko.SFTPClient) -> dict:
    preflight(client)
    candidates = upload_candidates()
    manifest = release_manifest.build(HERE, candidates)
    lineage = validate_release_lineage(sftp, manifest)
    changed_paths = changed_uploads(sftp, candidates)

    backup = run_backup(client)
    print(
        f"Backup verified: {backup['path']} "
        f"sha256={backup['sha256']} size={backup['size']}"
    )
    snapshot = create_source_snapshot(sftp, changed_paths, backup["timestamp"])
    print(f"Source rollback snapshot: {snapshot['root']}")

    upload_started = False
    service_stopped = False
    cron_suspended = False
    try:
        # The active tree is updated in place, so production must be stopped
        # before the first file changes. This prevents an automatic restart
        # from loading a half-uploaded release against the old schema.
        service_stopped = True
        stop_service(client)
        suspend_project_cron(client)
        cron_suspended = True
        assert_no_project_writers(client)
        upload_started = True
        upload_project(sftp, changed_paths)
        upload_release_manifest(sftp, manifest)
        verify_remote_manifest(sftp, manifest)
        provision(client)
        configure_service(client, sftp)
        verify_service_execstart(client)

        expected_rows = morning_context_row_count(client)
        daily_log_precheck(client)
        run_migration(client)
        migration = validate_migration(client, expected_rows)
        integrity = validate_data_integrity(client)
        analysis_contract = validate_analysis_contract(client)

        since = restart_service(client)
        validate_service_health(client, since)
        build_dashboard(client)
        configure_cron(client, sftp)
        return {
            "backup": backup, "snapshot": snapshot,
            "migration": migration, "integrity": integrity,
            "analysis_contract": analysis_contract,
            "manifest": manifest, "lineage": lineage,
        }
    except Exception as primary_error:
        rollback_errors = []
        if upload_started:
            try:
                rollback_sources(sftp, snapshot)
            except Exception as exc:
                rollback_errors.append(f"source rollback failed: {exc}")
        if service_stopped:
            try:
                recover_previous_service(client)
            except Exception as exc:
                rollback_errors.append(f"previous service recovery failed: {exc}")
        if cron_suspended:
            try:
                configure_cron(client, sftp)
            except Exception as exc:
                rollback_errors.append(f"cron recovery failed: {exc}")
        detail = f"Deployment failed: {primary_error}"
        if rollback_errors:
            detail += "; rollback issues: " + "; ".join(rollback_errors)
        else:
            detail += "; source rollback and previous service recovery completed"
        detail += "; database backup was not restored automatically"
        raise RuntimeError(detail) from primary_error


def rollback_project(client: paramiko.SSHClient, sftp: paramiko.SFTPClient, snapshot_root: str) -> None:
    """Restore one recorded source snapshot; database restore stays explicit."""
    manifest_path = posixpath.join(snapshot_root.rstrip("/"), "manifest.json")
    try:
        with sftp.file(manifest_path, "r") as source:
            snapshot_manifest = json.loads(source.read().decode("utf-8"))
    except (IOError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Rollback snapshot manifest is unavailable or invalid.") from exc
    snapshot = {"root": snapshot_root.rstrip("/"), "manifest": snapshot_manifest}
    service_stopped = False
    cron_suspended = False
    try:
        service_stopped = True
        stop_service(client)
        suspend_project_cron(client)
        cron_suspended = True
        assert_no_project_writers(client)
        rollback_sources(sftp, snapshot)
        manifest_remote = posixpath.join(REMOTE_DIR, REMOTE_RELEASE_MANIFEST)
        try:
            with sftp.file(manifest_remote, "r") as source:
                restored_manifest = json.loads(source.read().decode("utf-8"))
        except IOError:
            restored_manifest = None
        if restored_manifest is not None:
            verify_remote_manifest(sftp, restored_manifest)
        verify_service_execstart(client)
        recover_previous_service(client)
        service_stopped = False
        configure_cron(client, sftp)
        cron_suspended = False
    except Exception as primary_error:
        recovery_errors = []
        if service_stopped:
            try:
                recover_previous_service(client)
            except Exception as exc:
                recovery_errors.append(f"service recovery failed: {exc}")
        if cron_suspended:
            try:
                configure_cron(client, sftp)
            except Exception as exc:
                recovery_errors.append(f"cron recovery failed: {exc}")
        if recovery_errors:
            raise RuntimeError(
                f"Rollback failed: {primary_error}; "
                + "; ".join(recovery_errors)
            ) from primary_error
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rollback", metavar="SNAPSHOT_ROOT",
        help="restore a predeploy snapshot, e.g. /opt/whoop-workouts/data/backups/predeploy_...",
    )
    args = parser.parse_args()
    client = connect()
    try:
        sftp = client.open_sftp()
        try:
            if args.rollback:
                rollback_project(client, sftp, args.rollback)
                print("Source rollback completed; database backups are intentionally not restored automatically.")
                return
            result = deploy_project(client, sftp)
        finally:
            sftp.close()
    finally:
        client.close()
    print(
        "Deployment completed. "
        f"Backup: {result['backup']['path']}. "
        f"Rollback snapshot: {result['snapshot']['root']}. "
        "Migration validated before service restart. "
        "Local .env, tokens, database, reports and generated dashboard were not uploaded."
    )


if __name__ == "__main__":
    main()
