"""Additive Phase 2 persistence primitives.

This module deliberately owns only schema and low-level SQLite operations.  A
caller must run :func:`migrate` during deployment and pass an already-open
connection to every runtime API; read paths never perform DDL implicitly.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional


SESSION_TTL = dt.timedelta(hours=24)
MAX_SESSION_BYTES = 8 * 1024
MAX_CLEANUP_ROWS = 1_000
DEFAULT_CLAIM_LEASE = dt.timedelta(minutes=5)
FACTOR_JOB_LEASE = dt.timedelta(minutes=10)
FACTOR_JOB_MAX_ATTEMPTS = 5

SESSION_KEYS = frozenset({
    "active_topic",
    "last_read_intent",
    "last_query",
    "last_evidence_sha256",
    "turn_count",
})
SESSION_STRING_KEYS = frozenset({
    "active_topic", "last_read_intent", "last_evidence_sha256",
})
FORBIDDEN_STATE_KEYS = frozenset({
    "transcript", "raw_transcript", "messages", "raw_message", "raw_text",
    "notes", "daily_notes", "prompt", "response",
})


class Phase2StoreError(Exception):
    """Base persistence-contract error."""


class InvalidSessionState(Phase2StoreError):
    pass


class SessionConflict(Phase2StoreError):
    pass


class FactorConflict(Phase2StoreError):
    pass


class FactorJobConflict(Phase2StoreError):
    pass


@dataclass(frozen=True)
class SessionRecord:
    source: str
    chat_id: str
    user_id: str
    state: dict
    version: int
    expires_at: str


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_utc(value: Optional[dt.datetime]) -> dt.datetime:
    value = value or _utc_now()
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return _as_utc(value).isoformat(timespec="seconds")


def _principal(source: Any, chat_id: Any, user_id: Any) -> tuple[str, str, str]:
    source = str(source or "").strip()
    chat_id = str(chat_id or "").strip()
    user_id = "" if user_id is None else str(user_id).strip()
    if not source or not chat_id:
        raise ValueError("source and chat_id are required")
    return source, chat_id, user_id


def _validate_json_tree(value: Any, *, depth: int = 0) -> None:
    if depth > 6:
        raise InvalidSessionState("session state is too deeply nested")
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        if len(value) > 32:
            raise InvalidSessionState("session list is too large")
        for item in value:
            _validate_json_tree(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 32:
            raise InvalidSessionState("session object is too large")
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidSessionState("session object keys must be strings")
            if key.lower() in FORBIDDEN_STATE_KEYS:
                raise InvalidSessionState(f"forbidden session field: {key}")
            _validate_json_tree(item, depth=depth + 1)
        return
    raise InvalidSessionState(f"unsupported session value: {type(value).__name__}")


def encode_session_state(state: Mapping[str, Any]) -> str:
    """Validate and deterministically serialize the non-transcript state."""
    if not isinstance(state, Mapping):
        raise InvalidSessionState("session state must be an object")
    unknown = set(state) - SESSION_KEYS
    if unknown:
        raise InvalidSessionState(f"unknown session fields: {','.join(sorted(unknown))}")
    clean = dict(state)
    for key in SESSION_STRING_KEYS:
        value = clean.get(key)
        if value is not None and not isinstance(value, str):
            raise InvalidSessionState(f"{key} must be a string or null")
    turn_count = clean.get("turn_count", 0)
    if isinstance(turn_count, bool) or not isinstance(turn_count, int) or not 0 <= turn_count <= 6:
        raise InvalidSessionState("turn_count must be a bounded integer")
    clean["turn_count"] = turn_count
    last_query = clean.get("last_query")
    if last_query is not None and not isinstance(last_query, dict):
        raise InvalidSessionState("last_query must be an object or null")
    _validate_json_tree(clean)
    encoded = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_SESSION_BYTES:
        raise InvalidSessionState("serialized session exceeds 8 KiB")
    return encoded


def migrate(conn: sqlite3.Connection) -> None:
    """Run the idempotent additive Phase 2 migration."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_sessions (
            source TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '',
            state_json TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(source, chat_id, user_id),
            CHECK(length(CAST(state_json AS BLOB)) <= 8192)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_sessions_expiry "
        "ON conversation_sessions(expires_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_factor_observations (
            observation_id TEXT PRIMARY KEY,
            context_date TEXT NOT NULL,
            factor_key TEXT NOT NULL,
            state INTEGER NOT NULL CHECK(state IN (0, 1)),
            extractor_version TEXT NOT NULL,
            confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            source_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_factor_date_key "
        "ON daily_factor_observations(context_date, factor_key)"
    )
    factor_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(daily_factor_observations)")
    }
    for name, declaration in {
        "job_id": "TEXT",
        "projection_hash": "TEXT",
        "projection_revision": "INTEGER",
        "is_current": "INTEGER NOT NULL DEFAULT 1 CHECK(is_current IN (0, 1))",
    }.items():
        if name not in factor_columns:
            conn.execute(
                f"ALTER TABLE daily_factor_observations ADD COLUMN {name} {declaration}"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daily_factor_current "
        "ON daily_factor_observations(context_date, factor_key, is_current)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS factor_extraction_jobs (
            job_id TEXT PRIMARY KEY,
            context_date TEXT NOT NULL,
            projection_hash TEXT NOT NULL,
            projection_revision INTEGER NOT NULL CHECK(projection_revision >= 1),
            extractor_version TEXT NOT NULL,
            origin_action_id TEXT,
            source_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN
                ('pending','running','succeeded','failed','superseded','disabled')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            lease_token TEXT,
            lease_expires_at TEXT,
            last_error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(context_date, projection_hash, projection_revision, extractor_version)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factor_jobs_ready "
        "ON factor_extraction_jobs(status, available_at, lease_expires_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factor_jobs_date_revision "
        "ON factor_extraction_jobs(context_date, projection_revision DESC)"
    )

    pending_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(pending_actions)").fetchall()
    }
    if pending_columns:
        additions = {
            "claimed_by_action_id": "TEXT",
            "claimed_at": "TEXT",
            "claim_expires_at": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in pending_columns:
                conn.execute(f"ALTER TABLE pending_actions ADD COLUMN {name} {declaration}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_actions_claim_expiry "
            "ON pending_actions(status, claim_expires_at)"
        )
    conn.commit()


def save_session(
    conn: sqlite3.Connection,
    source: Any,
    chat_id: Any,
    user_id: Any,
    state: Mapping[str, Any],
    *,
    expected_version: Optional[int],
    now: Optional[dt.datetime] = None,
    ttl: dt.timedelta = SESSION_TTL,
) -> SessionRecord:
    """Create (`expected_version=None`) or CAS-update a session."""
    source, chat_id, user_id = _principal(source, chat_id, user_id)
    encoded = encode_session_state(state)
    now_dt = _as_utc(now)
    now_iso = _iso(now_dt)
    expires_at = _iso(now_dt + ttl)
    if ttl <= dt.timedelta(0) or ttl > SESSION_TTL:
        raise ValueError("session ttl must be positive and at most 24 hours")

    if expected_version is None:
        try:
            conn.execute(
                """INSERT INTO conversation_sessions
                   (source, chat_id, user_id, state_json, version, expires_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?, ?)""",
                (source, chat_id, user_id, encoded, expires_at, now_iso, now_iso),
            )
            version = 1
        except sqlite3.IntegrityError as exc:
            raise SessionConflict("session already exists") from exc
    else:
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version must be a positive integer")
        cur = conn.execute(
            """UPDATE conversation_sessions
               SET state_json = ?, version = version + 1, expires_at = ?, updated_at = ?
               WHERE source = ? AND chat_id = ? AND user_id = ? AND version = ?""",
            (encoded, expires_at, now_iso, source, chat_id, user_id, expected_version),
        )
        if cur.rowcount != 1:
            raise SessionConflict("session version changed or session is missing")
        version = expected_version + 1
    return SessionRecord(source, chat_id, user_id, dict(state), version, expires_at)


def get_session(
    conn: sqlite3.Connection,
    source: Any,
    chat_id: Any,
    user_id: Any,
    *,
    now: Optional[dt.datetime] = None,
) -> Optional[SessionRecord]:
    """Read an unexpired session.  This function never migrates or writes."""
    source, chat_id, user_id = _principal(source, chat_id, user_id)
    row = conn.execute(
        """SELECT source, chat_id, user_id, state_json, version, expires_at
           FROM conversation_sessions
           WHERE source = ? AND chat_id = ? AND user_id = ? AND expires_at > ?""",
        (source, chat_id, user_id, _iso(_as_utc(now))),
    ).fetchone()
    if row is None:
        return None
    return SessionRecord(
        source=row[0], chat_id=row[1], user_id=row[2],
        state=json.loads(row[3]), version=row[4], expires_at=row[5],
    )


def put_factor_observation(
    conn: sqlite3.Connection,
    *,
    context_date: str,
    factor_key: str,
    state: int,
    extractor_version: str,
    confidence: Optional[float],
    source_key: str,
    allowed_factor_keys,
    job_id: Optional[str] = None,
    projection_hash: Optional[str] = None,
    projection_revision: Optional[int] = None,
    is_current: int = 1,
    now: Optional[dt.datetime] = None,
) -> tuple[str, bool]:
    """Insert one allowlisted observation; identical source retries are no-ops."""
    if factor_key not in frozenset(allowed_factor_keys):
        raise ValueError("factor_key is not allowlisted")
    if isinstance(state, bool) or not isinstance(state, int) or state not in (0, 1):
        raise ValueError("factor state must be integer 0 or 1")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            raise ValueError("confidence must be null or between 0 and 1")
        confidence = float(confidence)
    dt.date.fromisoformat(context_date)
    if not extractor_version or not source_key:
        raise ValueError("extractor_version and source_key are required")
    now_iso = _iso(_as_utc(now))
    observation_id = str(uuid.uuid4())
    try:
        conn.execute(
            """INSERT INTO daily_factor_observations
               (observation_id, context_date, factor_key, state, extractor_version,
                 confidence, source_key, created_at, updated_at, job_id,
                 projection_hash, projection_revision, is_current)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (observation_id, context_date, factor_key, state, extractor_version,
             confidence, source_key, now_iso, now_iso, job_id, projection_hash,
             projection_revision, is_current),
        )
        return observation_id, True
    except sqlite3.IntegrityError as exc:
        row = conn.execute(
            """SELECT observation_id, context_date, factor_key, state,
                      extractor_version, confidence, job_id, projection_hash,
                      projection_revision, is_current
               FROM daily_factor_observations WHERE source_key = ?""",
            (source_key,),
        ).fetchone()
        wanted = (context_date, factor_key, state, extractor_version, confidence,
                  job_id, projection_hash, projection_revision, is_current)
        found = tuple(row[1:]) if row else None
        if row and found == wanted:
            return row[0], False
        raise FactorConflict("source_key already identifies different factor data") from exc


def enqueue_factor_job(
    conn: sqlite3.Connection, *, context_date: str, projection_hash: str,
    projection_revision: int, extractor_version: str, origin_action_id: Optional[str],
    source_key: Optional[str] = None, enabled: bool = True,
    now: Optional[dt.datetime] = None,
) -> tuple[str, bool]:
    """Persist one extraction run for the complete date projection."""
    dt.date.fromisoformat(context_date)
    if not projection_hash or not extractor_version or projection_revision < 1:
        raise ValueError("valid projection identity and extractor version are required")
    now_iso = _iso(_as_utc(now))
    key = source_key or (
        f"factor:{context_date}:{projection_revision}:{projection_hash}:{extractor_version}"
    )
    job_id = str(uuid.uuid4())
    status = "pending" if enabled else "disabled"
    try:
        conn.execute(
            """INSERT INTO factor_extraction_jobs
               (job_id, context_date, projection_hash, projection_revision,
                extractor_version, origin_action_id, source_key, status,
                attempt_count, available_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (job_id, context_date, projection_hash, projection_revision,
             extractor_version, origin_action_id, key, status, now_iso, now_iso, now_iso),
        )
        conn.execute(
            """UPDATE factor_extraction_jobs SET status='superseded', completed_at=?,
                      lease_token=NULL, lease_expires_at=NULL, updated_at=?
               WHERE context_date=? AND job_id<>? AND projection_revision<?
                 AND status IN ('pending','failed')""",
            (now_iso, now_iso, context_date, job_id, projection_revision),
        )
        return job_id, True
    except sqlite3.IntegrityError as exc:
        row = conn.execute(
            """SELECT job_id, context_date, projection_hash, projection_revision,
                      extractor_version, origin_action_id
               FROM factor_extraction_jobs WHERE source_key=?""",
            (key,),
        ).fetchone()
        wanted = (context_date, projection_hash, projection_revision,
                  extractor_version, origin_action_id)
        if row is not None and tuple(row[1:]) == wanted:
            return row[0], False
        row = conn.execute(
            """SELECT job_id, context_date, projection_hash, projection_revision,
                      extractor_version, origin_action_id
               FROM factor_extraction_jobs
               WHERE context_date=? AND projection_hash=? AND projection_revision=?
                 AND extractor_version=?""",
            (context_date, projection_hash, projection_revision, extractor_version),
        ).fetchone()
        if row is not None and tuple(row[1:]) == wanted:
            return row[0], False
        raise FactorJobConflict("factor source_key identifies different projection") from exc


def claim_factor_job(conn: sqlite3.Connection, *, job_id: Optional[str] = None,
                     now: Optional[dt.datetime] = None,
                     lease: dt.timedelta = FACTOR_JOB_LEASE):
    now_dt = _as_utc(now)
    now_iso = _iso(now_dt)
    token = str(uuid.uuid4())
    job_filter = " AND job_id=?" if job_id else ""
    # A worker may die while holding its final lease.  Once that lease expires,
    # turn the exhausted run into a terminal failure instead of leaving it
    # permanently "running" or reclaiming it beyond the retry budget.
    conn.execute(
        """UPDATE factor_extraction_jobs
           SET status='failed', completed_at=COALESCE(completed_at, ?),
               lease_token=NULL, lease_expires_at=NULL,
               last_error_code=COALESCE(last_error_code, 'max_attempts_exceeded'),
               updated_at=?
           WHERE attempt_count>=? AND completed_at IS NULL
             AND (status='failed'
                  OR (status='running' AND lease_expires_at<=?))""",
        (now_iso, now_iso, FACTOR_JOB_MAX_ATTEMPTS, now_iso),
    )
    params = [
        now_iso, FACTOR_JOB_MAX_ATTEMPTS,
        now_iso, FACTOR_JOB_MAX_ATTEMPTS,
    ]
    if job_id:
        params.append(job_id)
    row = conn.execute(
        """SELECT * FROM factor_extraction_jobs
           WHERE ((status IN ('pending','failed') AND available_at<=?
                   AND attempt_count<?)
                  OR (status='running' AND lease_expires_at<=?
                      AND attempt_count<?))
           """ + job_filter + " ORDER BY projection_revision DESC, created_at LIMIT 1",
        params,
    ).fetchone()
    if row is None:
        return None
    expires = _iso(now_dt + lease)
    cur = conn.execute(
        """UPDATE factor_extraction_jobs
           SET status='running', lease_token=?, lease_expires_at=?,
               attempt_count=attempt_count+1, updated_at=?
           WHERE job_id=? AND ((status IN ('pending','failed') AND available_at<=?
                                AND attempt_count<?)
              OR (status='running' AND lease_expires_at<=?
                  AND attempt_count<?))""",
        (token, expires, now_iso, row["job_id"],
         now_iso, FACTOR_JOB_MAX_ATTEMPTS,
         now_iso, FACTOR_JOB_MAX_ATTEMPTS),
    )
    if cur.rowcount != 1:
        return None
    claimed = conn.execute(
        "SELECT * FROM factor_extraction_jobs WHERE job_id=?", (row["job_id"],)
    ).fetchone()
    out = dict(claimed)
    out["lease_token"] = token
    return out


def fail_factor_job(conn: sqlite3.Connection, job_id: str, lease_token: str,
                    error_code: str, *, retry_at: Optional[dt.datetime] = None,
                    now: Optional[dt.datetime] = None) -> bool:
    now_dt = _as_utc(now)
    retry = _as_utc(retry_at or (now_dt + dt.timedelta(minutes=5)))
    cur = conn.execute(
        """UPDATE factor_extraction_jobs
           SET status='failed', available_at=?, lease_token=NULL,
               lease_expires_at=NULL, last_error_code=?, updated_at=?,
               completed_at=CASE WHEN attempt_count>=? THEN ? ELSE NULL END
           WHERE job_id=? AND status='running' AND lease_token=?""",
        (_iso(retry), str(error_code or "factor_failed")[:80], _iso(now_dt),
         FACTOR_JOB_MAX_ATTEMPTS, _iso(now_dt), job_id, lease_token),
    )
    return cur.rowcount == 1


def complete_factor_job(
    conn: sqlite3.Connection, job_id: str, lease_token: str, observations,
    *, allowed_factor_keys, now: Optional[dt.datetime] = None,
) -> bool:
    """Fence completion and atomically publish only the latest projection run."""
    row = conn.execute(
        "SELECT * FROM factor_extraction_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    if row is None or row["status"] != "running" or row["lease_token"] != lease_token:
        return False
    current = conn.execute(
        """SELECT projection_hash, revision FROM daily_context_projection_state
           WHERE context_date=?""",
        (row["context_date"],),
    ).fetchone()
    now_iso = _iso(_as_utc(now))
    if current is None or current[0] != row["projection_hash"] or int(current[1]) != int(row["projection_revision"]):
        conn.execute(
            """UPDATE factor_extraction_jobs SET status='superseded', completed_at=?,
                      lease_token=NULL, lease_expires_at=NULL, updated_at=?
               WHERE job_id=? AND lease_token=?""",
            (now_iso, now_iso, job_id, lease_token),
        )
        return False
    conn.execute(
        "UPDATE daily_factor_observations SET is_current=0, updated_at=? "
        "WHERE context_date=? AND is_current=1",
        (now_iso, row["context_date"]),
    )
    for item in observations:
        put_factor_observation(
            conn, context_date=row["context_date"], factor_key=item["factor_key"],
            state=int(item["state"]), extractor_version=row["extractor_version"],
            confidence=item.get("confidence"),
            source_key=f"factor-job:{job_id}:{item['factor_key']}",
            allowed_factor_keys=allowed_factor_keys, job_id=job_id,
            projection_hash=row["projection_hash"],
            projection_revision=row["projection_revision"], is_current=1, now=now,
        )
    cur = conn.execute(
        """UPDATE factor_extraction_jobs SET status='succeeded', completed_at=?,
                  lease_token=NULL, lease_expires_at=NULL, last_error_code=NULL,
                  updated_at=? WHERE job_id=? AND status='running' AND lease_token=?""",
        (now_iso, now_iso, job_id, lease_token),
    )
    return cur.rowcount == 1


def claim_pending_for_resolution(
    conn: sqlite3.Connection,
    pending_id: str,
    action_id: str,
    *,
    now: Optional[dt.datetime] = None,
    lease: dt.timedelta = DEFAULT_CLAIM_LEASE,
) -> bool:
    """Atomically claim one open pending row; exactly one contender wins."""
    if lease <= dt.timedelta(0):
        raise ValueError("claim lease must be positive")
    now_dt = _as_utc(now)
    cur = conn.execute(
        """UPDATE pending_actions
           SET status = 'resolving', claimed_by_action_id = ?, claimed_at = ?,
               claim_expires_at = ?, updated_at = ?
           WHERE pending_id = ? AND status = 'open' AND expires_at > ?""",
        (action_id, _iso(now_dt), _iso(now_dt + lease), _iso(now_dt),
         pending_id, _iso(now_dt)),
    )
    return cur.rowcount == 1


def finalize_pending_resolution(
    conn: sqlite3.Connection,
    pending_id: str,
    action_id: str,
    *,
    now: Optional[dt.datetime] = None,
) -> bool:
    now_iso = _iso(_as_utc(now))
    cur = conn.execute(
        """UPDATE pending_actions
           SET status = 'resolved', resolved_at = ?, resolved_by_action_id = ?,
               claim_expires_at = NULL, updated_at = ?
           WHERE pending_id = ? AND status = 'resolving'
             AND claimed_by_action_id = ?""",
        (now_iso, action_id, now_iso, pending_id, action_id),
    )
    return cur.rowcount == 1


def release_pending_claim(
    conn: sqlite3.Connection,
    pending_id: str,
    action_id: str,
    *,
    now: Optional[dt.datetime] = None,
) -> bool:
    now_iso = _iso(_as_utc(now))
    cur = conn.execute(
        """UPDATE pending_actions
           SET status = 'open', claimed_by_action_id = NULL, claimed_at = NULL,
               claim_expires_at = NULL, updated_at = ?
           WHERE pending_id = ? AND status = 'resolving'
             AND claimed_by_action_id = ? AND expires_at > ?""",
        (now_iso, pending_id, action_id, now_iso),
    )
    return cur.rowcount == 1


def recover_stale_pending_claims(
    conn: sqlite3.Connection,
    *,
    now: Optional[dt.datetime] = None,
    limit: int = 100,
) -> int:
    limit = _bounded_limit(limit)
    now_iso = _iso(_as_utc(now))
    cur = conn.execute(
        """UPDATE pending_actions
           SET status = CASE WHEN expires_at > ? THEN 'open' ELSE 'expired' END,
               claimed_by_action_id = NULL, claimed_at = NULL,
               claim_expires_at = NULL, updated_at = ?
           WHERE pending_id IN (
               SELECT pending_id FROM pending_actions
               WHERE status = 'resolving' AND claim_expires_at <= ?
               ORDER BY claim_expires_at LIMIT ?
           )""",
        (now_iso, now_iso, now_iso, limit),
    )
    return cur.rowcount

def _bounded_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CLEANUP_ROWS:
        raise ValueError(f"limit must be between 1 and {MAX_CLEANUP_ROWS}")
    return limit


def cleanup_expired_sessions(
    conn: sqlite3.Connection, *, now: Optional[dt.datetime] = None, limit: int = 100
) -> int:
    limit = _bounded_limit(limit)
    cur = conn.execute(
        """DELETE FROM conversation_sessions WHERE rowid IN (
               SELECT rowid FROM conversation_sessions
               WHERE expires_at <= ? ORDER BY expires_at LIMIT ?
           )""",
        (_iso(_as_utc(now)), limit),
    )
    return cur.rowcount


def redact_terminal_pending_payloads(
    conn: sqlite3.Connection, *, before: dt.datetime, limit: int = 100
) -> int:
    """Clear resolved/expired partial arguments after the retention window."""
    limit = _bounded_limit(limit)
    cur = conn.execute(
        """UPDATE pending_actions
           SET partial_arguments_json = '{}', missing_fields_json = '[]'
           WHERE pending_id IN (
               SELECT pending_id FROM pending_actions
               WHERE status IN ('resolved', 'expired') AND updated_at < ?
                 AND (partial_arguments_json != '{}' OR missing_fields_json != '[]')
               ORDER BY updated_at LIMIT ?
           )""",
        (_iso(_as_utc(before)), limit),
    )
    return cur.rowcount
