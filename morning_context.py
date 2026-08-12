"""Durable state for the WHOOP morning check-in conversation.

One WHOOP recovery result maps to exactly one question about the prior evening.
The state lives in SQLite rather than process memory, so a bot restart or cron retry
cannot create duplicate questions, lose an answer, or send the same analysis twice.
"""
import datetime as dt

import daily_log
import phase2_store


REMINDER_AFTER = dt.timedelta(hours=2)
QUESTION_CLAIM_TIMEOUT = dt.timedelta(minutes=10)
ANALYSIS_CLAIM_TIMEOUT = dt.timedelta(minutes=15)
STATUS_PENDING = "pending"
STATUS_ANSWERED = "answered"
STATUS_ANALYZING = "analyzing"
STATUS_ANALYZED = "analyzed"
STATUS_EXPIRED = "expired"


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _as_date(value):
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _row_dict(row):
    return dict(row) if row is not None else None


def ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS morning_context (
            recovery_date TEXT PRIMARY KEY,
            evening_date TEXT NOT NULL,
            status TEXT NOT NULL,
            question_message_id TEXT,
            question_claimed_at TEXT,
            asked_at TEXT,
            reminded_at TEXT,
            replied_at TEXT,
            analyzed_at TEXT,
            source_message_id TEXT,
            analysis_mode TEXT,
            analysis_claimed_at TEXT,
            analysis_available_at TEXT,
            analysis_attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (status IN ('pending', 'answered', 'analyzing', 'analyzed', 'expired'))
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(morning_context)")}
    if "question_message_id" not in columns:
        conn.execute("ALTER TABLE morning_context ADD COLUMN question_message_id TEXT")
    if "question_claimed_at" not in columns:
        conn.execute("ALTER TABLE morning_context ADD COLUMN question_claimed_at TEXT")
    if "analysis_mode" not in columns:
        conn.execute("ALTER TABLE morning_context ADD COLUMN analysis_mode TEXT")
    if "analysis_claimed_at" not in columns:
        conn.execute("ALTER TABLE morning_context ADD COLUMN analysis_claimed_at TEXT")
    if "analysis_available_at" not in columns:
        conn.execute("ALTER TABLE morning_context ADD COLUMN analysis_available_at TEXT")
    if "analysis_attempt_count" not in columns:
        conn.execute("ALTER TABLE morning_context ADD COLUMN analysis_attempt_count INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_morning_context_status "
        "ON morning_context(status, recovery_date)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_morning_context_question_message_id "
        "ON morning_context(question_message_id) WHERE question_message_id IS NOT NULL"
    )
    conn.commit()


def _connect():
    conn = daily_log.connect()
    ensure_table(conn)
    return conn


def ensure_request(recovery_date):
    """Return the durable context row and whether its first question must be sent."""
    recovery_day = _as_date(recovery_date)
    recovery_iso = recovery_day.isoformat()
    evening_iso = (recovery_day - dt.timedelta(days=1)).isoformat()
    now = _now_iso()

    conn = _connect()
    try:
        # A late reply is ambiguous after the next recovery is already available.
        # Expire the old request instead of attaching that reply to the wrong evening.
        conn.execute(
            """UPDATE morning_context
               SET status = ?, question_claimed_at = NULL, updated_at = ?
               WHERE status = ? AND recovery_date < ?""",
            (STATUS_EXPIRED, now, STATUS_PENDING, recovery_iso),
        )
        row = conn.execute(
            "SELECT * FROM morning_context WHERE recovery_date = ?", (recovery_iso,)
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT OR IGNORE INTO morning_context
                   (recovery_date, evening_date, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (recovery_iso, evening_iso, STATUS_PENDING, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM morning_context WHERE recovery_date = ?", (recovery_iso,)
            ).fetchone()

        conn.commit()
        item = _row_dict(row)
        needs_question = (
            item["status"] == STATUS_PENDING
            and not item["asked_at"]
            and not item["question_message_id"]
            and not item["question_claimed_at"]
        )
        return item, needs_question
    finally:
        conn.close()


def request_for_date(recovery_date):
    """Return an existing context row without creating or changing state."""
    recovery_iso = _as_date(recovery_date).isoformat()
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM morning_context WHERE recovery_date = ?",
            (recovery_iso,),
        ).fetchone()
        return _row_dict(row) if row is not None else None
    finally:
        conn.close()


def claim_question(recovery_date, now=None):
    """Atomically claim the right to send one question for a recovery date.

    A stale claim is retryable after ``QUESTION_CLAIM_TIMEOUT``. The unavoidable
    Telegram/SQLite gap is therefore bounded without allowing ordinary cron
    overlap to send the same question twice.
    """
    ensure_request(recovery_date)
    claim_time = now or dt.datetime.now(dt.timezone.utc)
    if claim_time.tzinfo is None:
        claim_time = claim_time.replace(tzinfo=dt.timezone.utc)
    now_iso = claim_time.isoformat(timespec="seconds")
    stale_before = (claim_time - QUESTION_CLAIM_TIMEOUT).isoformat(timespec="seconds")
    recovery_iso = _as_date(recovery_date).isoformat()

    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """UPDATE morning_context
               SET question_claimed_at = ?, updated_at = ?
               WHERE recovery_date = ? AND status = ?
                 AND question_message_id IS NULL AND asked_at IS NULL
                 AND (question_claimed_at IS NULL OR question_claimed_at <= ?)""",
            (now_iso, now_iso, recovery_iso, STATUS_PENDING, stale_before),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        row = conn.execute(
            "SELECT * FROM morning_context WHERE recovery_date = ?", (recovery_iso,)
        ).fetchone()
        return _row_dict(row)
    finally:
        conn.close()


def mark_question_sent(recovery_date, question_message_id):
    if question_message_id is None:
        return False
    now = _now_iso()
    conn = _connect()
    try:
        cur = conn.execute(
            """UPDATE morning_context
               SET question_message_id = ?, question_claimed_at = NULL,
                   asked_at = ?, updated_at = ?
               WHERE recovery_date = ? AND status = ?
                 AND question_message_id IS NULL AND asked_at IS NULL
                 AND question_claimed_at IS NOT NULL""",
            (
                str(question_message_id),
                now,
                now,
                _as_date(recovery_date).isoformat(),
                STATUS_PENDING,
            ),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def release_question_claim(recovery_date):
    """Release a failed send so a later cron run can retry it."""
    now = _now_iso()
    conn = _connect()
    try:
        cur = conn.execute(
            """UPDATE morning_context
               SET question_claimed_at = NULL, updated_at = ?
               WHERE recovery_date = ? AND status = ?
                 AND question_message_id IS NULL AND asked_at IS NULL""",
            (now, _as_date(recovery_date).isoformat(), STATUS_PENDING),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def reminder_due(recovery_date, now=None):
    """Return the pending row when its one permitted reminder is due."""
    check_time = now or dt.datetime.now(dt.timezone.utc)
    if check_time.tzinfo is None:
        check_time = check_time.replace(tzinfo=dt.timezone.utc)
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT * FROM morning_context
               WHERE recovery_date = ? AND status = ?
                 AND asked_at IS NOT NULL AND question_message_id IS NOT NULL
                 AND reminded_at IS NULL""",
            (_as_date(recovery_date).isoformat(), STATUS_PENDING),
        ).fetchone()
        if row is None:
            return None
        asked_at = dt.datetime.fromisoformat(row["asked_at"])
        if asked_at.tzinfo is None:
            asked_at = asked_at.replace(tzinfo=dt.timezone.utc)
        return _row_dict(row) if check_time - asked_at >= REMINDER_AFTER else None
    finally:
        conn.close()


def mark_reminder_sent(recovery_date):
    now = _now_iso()
    conn = _connect()
    try:
        cur = conn.execute(
            """UPDATE morning_context
               SET reminded_at = ?, updated_at = ?
               WHERE recovery_date = ? AND status = ? AND reminded_at IS NULL""",
            (now, now, _as_date(recovery_date).isoformat(), STATUS_PENDING),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def accept_pending_reply(message_id, question_message_id):
    """Bind a direct Telegram reply to its exact pending morning question."""
    if message_id is None or question_message_id is None:
        return None
    now = _now_iso()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            "SELECT 1 FROM morning_context WHERE source_message_id = ?",
            (str(message_id),),
        ).fetchone()
        if duplicate is not None:
            conn.rollback()
            return None
        row = conn.execute(
            """SELECT * FROM morning_context
               WHERE status = ? AND asked_at IS NOT NULL
                 AND question_message_id = ?
               LIMIT 1""",
            (STATUS_PENDING, str(question_message_id)),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None

        cur = conn.execute(
            """UPDATE morning_context
               SET status = ?, replied_at = ?, source_message_id = ?, updated_at = ?
               WHERE recovery_date = ? AND status = ?""",
            (STATUS_ANSWERED, now, str(message_id), now, row["recovery_date"], STATUS_PENDING),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        row = conn.execute(
            "SELECT * FROM morning_context WHERE recovery_date = ?", (row["recovery_date"],)
        ).fetchone()
        return _row_dict(row)
    finally:
        conn.close()


def accept_and_record_reply(message_id, question_message_id, note, *, source_key,
                            origin_action_id=None, extractor_version="factor_capture_v1",
                            factor_capture_enabled=True):
    """Atomically bind an exact Reply, persist its immutable entry/projection and outbox."""
    if message_id is None or question_message_id is None:
        return None
    body = str(note or "").strip()
    if not body:
        return None
    now = _now_iso()
    conn = _connect()
    try:
        phase2_store.migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            "SELECT * FROM morning_context WHERE source_message_id=?",
            (str(message_id),),
        ).fetchone()
        if duplicate is not None:
            entry = conn.execute(
                "SELECT entry_id FROM daily_context_entries WHERE source_key=?",
                (source_key,),
            ).fetchone()
            conn.rollback()
            item = _row_dict(duplicate)
            item["entry_id"] = entry[0] if entry is not None else None
            item["inserted"] = False
            return item
        row = conn.execute(
            """SELECT * FROM morning_context
               WHERE status=? AND asked_at IS NOT NULL AND question_message_id=? LIMIT 1""",
            (STATUS_PENDING, str(question_message_id)),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        entry_id, inserted, projection = daily_log.append_entry_tx(
            conn, row["evening_date"], body, label=None, source_key=source_key,
            origin_action_id=origin_action_id,
        )
        phase2_store.enqueue_factor_job(
            conn, context_date=row["evening_date"],
            projection_hash=projection["projection_hash"],
            projection_revision=projection["revision"],
            extractor_version=extractor_version,
            origin_action_id=origin_action_id,
            source_key=f"{source_key}:factor-projection:{projection['revision']}",
            enabled=factor_capture_enabled,
        )
        cur = conn.execute(
            """UPDATE morning_context
               SET status=?, replied_at=?, source_message_id=?, updated_at=?
               WHERE recovery_date=? AND status=?""",
            (STATUS_ANSWERED, now, str(message_id), now,
             row["recovery_date"], STATUS_PENDING),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        result = _row_dict(conn.execute(
            "SELECT * FROM morning_context WHERE recovery_date=?", (row["recovery_date"],)
        ).fetchone())
        result.update({"entry_id": entry_id, "inserted": inserted,
                       "projection_hash": projection["projection_hash"],
                       "projection_revision": projection["revision"]})
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reopen_reply(recovery_date):
    """Return a just-accepted reply to pending if its raw log could not be saved."""
    recovery_iso = _as_date(recovery_date).isoformat()
    now = _now_iso()
    conn = _connect()
    try:
        conn.execute(
            """UPDATE morning_context
               SET status = ?, replied_at = NULL, source_message_id = NULL, updated_at = ?
               WHERE recovery_date = ? AND status = ?""",
            (STATUS_PENDING, now, recovery_iso, STATUS_ANSWERED),
        )
        conn.commit()
    finally:
        conn.close()

def recover_stale_analysis_claims(now=None):
    """Return abandoned analysis leases to the retry queue after a service restart."""
    claim_time = now or dt.datetime.now(dt.timezone.utc)
    if claim_time.tzinfo is None:
        claim_time = claim_time.replace(tzinfo=dt.timezone.utc)
    now_iso = claim_time.isoformat(timespec="seconds")
    stale_before = (claim_time - ANALYSIS_CLAIM_TIMEOUT).isoformat(timespec="seconds")
    conn = _connect()
    try:
        cur = conn.execute(
            """UPDATE morning_context
               SET status=?, analysis_claimed_at=NULL, analysis_mode=NULL, updated_at=?
               WHERE status=? AND analysis_claimed_at IS NOT NULL
                 AND analysis_claimed_at <= ?""",
            (STATUS_ANSWERED, now_iso, STATUS_ANALYZING, stale_before),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def answered_contexts():
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM morning_context WHERE status=? ORDER BY recovery_date",
            (STATUS_ANSWERED,),
        ).fetchall()
        return [_row_dict(row) for row in rows]
    finally:
        conn.close()


def analyzed_contexts():
    """Completed contexts; used to retry an independently failed weekly report."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM morning_context WHERE status = ? ORDER BY recovery_date DESC",
            (STATUS_ANALYZED,),
        ).fetchall()
        return [_row_dict(row) for row in rows]
    finally:
        conn.close()

def claim_analysis(recovery_date, mode="whoop", now=None):
    """Claim one specific ready analysis, used for an immediate bot reply."""
    if mode not in {"whoop", "context_only"}:
        return None
    recovery_iso = _as_date(recovery_date).isoformat()
    claim_time = now or dt.datetime.now(dt.timezone.utc)
    if claim_time.tzinfo is None:
        claim_time = claim_time.replace(tzinfo=dt.timezone.utc)
    now_iso = claim_time.isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT * FROM morning_context WHERE recovery_date=? AND status=?
               AND (analysis_available_at IS NULL OR analysis_available_at <= ?)""",
            (recovery_iso, STATUS_ANSWERED, now_iso),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        cur = conn.execute(
            """UPDATE morning_context
               SET status=?, analysis_mode=?, analysis_claimed_at=?,
                   analysis_attempt_count=analysis_attempt_count+1, updated_at=?
               WHERE recovery_date = ? AND status = ?""",
            (STATUS_ANALYZING, mode, now_iso, now_iso, recovery_iso, STATUS_ANSWERED),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None
        claimed = conn.execute(
            "SELECT * FROM morning_context WHERE recovery_date=?", (recovery_iso,)
        ).fetchone()
        return _row_dict(claimed)
    finally:
        conn.close()

def complete_analysis(recovery_date):
    now = _now_iso()
    conn = _connect()
    try:
        cur = conn.execute(
            """UPDATE morning_context
               SET status=?, analyzed_at=?, analysis_claimed_at=NULL,
                   analysis_available_at=NULL, updated_at=?
               WHERE recovery_date = ? AND status = ?""",
            (STATUS_ANALYZED, now, now, _as_date(recovery_date).isoformat(), STATUS_ANALYZING),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def release_analysis(recovery_date, now=None):
    """Make a failed delivery retryable by the bot or the next cron run."""
    failed_at = now or dt.datetime.now(dt.timezone.utc)
    if failed_at.tzinfo is None:
        failed_at = failed_at.replace(tzinfo=dt.timezone.utc)
    now_iso = failed_at.isoformat(timespec="seconds")
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT analysis_attempt_count FROM morning_context WHERE recovery_date=?",
            (_as_date(recovery_date).isoformat(),),
        ).fetchone()
        attempts = int(row[0] or 1) if row else 1
        delay_minutes = min(60, 5 * (2 ** min(max(attempts - 1, 0), 4)))
        available = (failed_at + dt.timedelta(minutes=delay_minutes)).isoformat(timespec="seconds")
        cur = conn.execute(
            """UPDATE morning_context
               SET status=?, analysis_claimed_at=NULL, analysis_mode=NULL,
                   analysis_available_at=?, updated_at=?
               WHERE recovery_date = ? AND status = ?""",
            (STATUS_ANSWERED, available, now_iso,
             _as_date(recovery_date).isoformat(), STATUS_ANALYZING),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()
