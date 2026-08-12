"""Additive persistence for the conversation router: audit ledger + pending
clarifications. Lives in the same data/whoop.db but only ever creates its own
two tables; it never touches WHOOP / activity / daily_log / morning_context.

The audit row (`conversation_actions`) doubles as the action-level duplicate
ledger: UNIQUE(source, chat_id, message_id) means one Telegram message reserves
exactly one action, and a concurrent duplicate loses the race and reads back the
stored terminal result instead of running Gemini or a tool again.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Optional

import daily_log
import conversation_contract as C

ACTION_LEASE = dt.timedelta(minutes=5)
TERMINAL_ACTIONS = frozenset({
    C.ACTION_NEEDS_CLARIFICATION, C.ACTION_REJECTED, C.ACTION_NOOP,
    C.ACTION_SUCCEEDED, C.ACTION_FAILED,
})


class ActionLeaseLost(RuntimeError):
    pass


def _now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _sha256(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def connect():
    conn = daily_log.connect()  # WAL + busy_timeout on data/whoop.db
    ensure_conversation_tables(conn)
    return conn


def ensure_conversation_tables(conn):
    conn.execute("PRAGMA recursive_triggers = ON")
    """Idempotent, additive migration. Creates only the two conversation tables."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_actions (
            action_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            user_id TEXT,
            message_id TEXT NOT NULL,
            reply_to_message_id TEXT,
            received_at TEXT NOT NULL,
            input_sha256 TEXT NOT NULL,
            input_length INTEGER NOT NULL,
            router_model TEXT,
            router_schema_version TEXT,
            prompt_version TEXT,
            router_response_sha256 TEXT,
            relay_metadata_json TEXT,
            intent TEXT,
            confidence REAL,
            tool_name TEXT,
            validated_arguments_json TEXT,
            status TEXT NOT NULL,
            error_code TEXT,
            error_detail TEXT,
            result_json TEXT,
            response_kind TEXT,
            response_text TEXT,
            reply_delivery_status TEXT,
            reply_message_id TEXT,
            reply_attempt_count INTEGER NOT NULL DEFAULT 0,
            response_claimed_at TEXT,
            processing_token TEXT,
            processing_fence INTEGER NOT NULL DEFAULT 0,
            processing_claimed_at TEXT,
            processing_claim_expires_at TEXT,
            latency_ms INTEGER,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(source, chat_id, message_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_actions_status "
        "ON conversation_actions(status, created_at)"
    )
    existing_action_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(conversation_actions)")
    }
    for name, declaration in {
        "response_kind": "TEXT",
        "response_text": "TEXT",
        "reply_delivery_status": "TEXT",
        "reply_message_id": "TEXT",
        "reply_attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "response_claimed_at": "TEXT",
        "relay_metadata_json": "TEXT",
        "processing_token": "TEXT",
        "processing_fence": "INTEGER NOT NULL DEFAULT 0",
        "processing_claimed_at": "TEXT",
        "processing_claim_expires_at": "TEXT",
    }.items():
        if name not in existing_action_columns:
            conn.execute(
                f"ALTER TABLE conversation_actions ADD COLUMN {name} {declaration}"
            )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_actions (
            pending_id TEXT PRIMARY KEY,
            origin_action_id TEXT NOT NULL,
            source TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            user_id TEXT,
            candidate_intent TEXT NOT NULL,
            partial_arguments_json TEXT NOT NULL,
            missing_fields_json TEXT NOT NULL,
            clarification_question_message_id TEXT,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            resolved_by_action_id TEXT,
            claimed_by_action_id TEXT,
            claimed_at TEXT,
            claim_expires_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_actions_active "
        "ON pending_actions(source, chat_id, user_id, status)"
    )
    existing_pending_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(pending_actions)")
    }
    for name in ("claimed_by_action_id", "claimed_at", "claim_expires_at"):
        if name not in existing_pending_columns:
            conn.execute(f"ALTER TABLE pending_actions ADD COLUMN {name} TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_actions_claim_expiry "
        "ON pending_actions(status, claim_expires_at)"
    )
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_conversation_actions_no_delete
           BEFORE DELETE ON conversation_actions
           BEGIN
             SELECT RAISE(ABORT, 'audit action delete blocked');
           END"""
    )
    trigger_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' "
        "AND name='trg_conversation_actions_success_immutable'"
    ).fetchone()
    if trigger_row is not None and "correct_activity" not in (trigger_row[0] or ""):
        conn.execute("DROP TRIGGER trg_conversation_actions_success_immutable")
    conn.execute(
        """CREATE TRIGGER IF NOT EXISTS trg_conversation_actions_success_immutable
           BEFORE UPDATE ON conversation_actions
           WHEN OLD.status = 'succeeded'
             AND OLD.tool_name IN ('log_strength_workout', 'log_cardio',
                                   'log_supplement', 'save_daily_context',
                                   'soft_delete_activity', 'correct_activity')
             AND (
             NEW.status IS NOT OLD.status OR
             NEW.source IS NOT OLD.source OR
             NEW.chat_id IS NOT OLD.chat_id OR
             NEW.message_id IS NOT OLD.message_id OR
             NEW.input_sha256 IS NOT OLD.input_sha256 OR
             NEW.intent IS NOT OLD.intent OR
             NEW.tool_name IS NOT OLD.tool_name OR
             NEW.validated_arguments_json IS NOT OLD.validated_arguments_json OR
             NEW.result_json IS NOT OLD.result_json OR
             NEW.completed_at IS NOT OLD.completed_at
           )
           BEGIN
             SELECT RAISE(ABORT, 'succeeded audit action is immutable');
           END"""
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Reservation / audit ledger
# --------------------------------------------------------------------------- #

@dataclass
class Reservation:
    action_id: str
    is_new: bool
    status: str
    result_json: Optional[str] = None
    intent: Optional[str] = None
    response_kind: Optional[str] = None
    response_text: Optional[str] = None
    processing_token: Optional[str] = None
    processing_fence: int = 0
    reclaimed: bool = False
    in_progress: bool = False
    tool_name: Optional[str] = None
    validated_arguments_json: Optional[str] = None


@dataclass
class ActionContext:
    source: str
    chat_id: str
    message_id: str
    user_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    received_at: Optional[str] = None
    input_text: str = ""


def reserve(ctx: ActionContext, *, now=None, lease=ACTION_LEASE) -> Reservation:
    """Atomically reserve one action for a Telegram message.

    Returns is_new=True with a fresh action_id, or is_new=False carrying the
    existing row's terminal status/result when this exact message was already
    seen (the duplicate guard). The winner of a concurrent race is the one that
    inserts; the loser reads the committed row.
    """
    action_id = str(uuid.uuid4())
    now_dt = now or dt.datetime.now(dt.timezone.utc)
    if now_dt.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_iso = now_dt.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    lease_expires = (now_dt + lease).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    token = str(uuid.uuid4())
    received_at = ctx.received_at or now_iso
    input_hash = _sha256(ctx.input_text)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """INSERT INTO conversation_actions
                   (action_id, source, chat_id, user_id, message_id,
                    reply_to_message_id, received_at, input_sha256, input_length,
                    status, attempt_count, created_at, updated_at,
                    processing_token, processing_fence, processing_claimed_at,
                    processing_claim_expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (
                    action_id, ctx.source, str(ctx.chat_id),
                    str(ctx.user_id) if ctx.user_id is not None else None,
                    str(ctx.message_id),
                    str(ctx.reply_to_message_id) if ctx.reply_to_message_id is not None else None,
                    received_at, input_hash, len(ctx.input_text or ""),
                    C.ACTION_RECEIVED, 0, now_iso, now_iso,
                    token, now_iso, lease_expires,
                ),
            )
            conn.commit()
            return Reservation(action_id=action_id, is_new=True, status=C.ACTION_RECEIVED,
                               processing_token=token, processing_fence=1)
        except sqlite3.IntegrityError:
            conn.rollback()
            row = conn.execute(
                """SELECT action_id, status, result_json, intent,
                          response_kind, response_text, input_sha256,
                          processing_token, processing_fence,
                          processing_claim_expires_at, tool_name,
                          validated_arguments_json
                   FROM conversation_actions
                   WHERE source = ? AND chat_id = ? AND message_id = ?""",
                (ctx.source, str(ctx.chat_id), str(ctx.message_id)),
            ).fetchone()
            if row is None:  # pragma: no cover - integrity error implies a row
                raise
            if row["input_sha256"] != input_hash:
                raise ActionLeaseLost("message identity was reused with different input")
            if row["status"] not in TERMINAL_ACTIONS and (
                not row["processing_claim_expires_at"]
                or row["processing_claim_expires_at"] <= now_iso
            ):
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    """UPDATE conversation_actions
                       SET processing_token=?, processing_fence=processing_fence+1,
                           processing_claimed_at=?, processing_claim_expires_at=?,
                           attempt_count=attempt_count+1, updated_at=?
                       WHERE action_id=? AND status NOT IN (?,?,?,?,?)
                         AND (processing_claim_expires_at IS NULL OR processing_claim_expires_at<=?)""",
                    (token, now_iso, lease_expires, now_iso, row["action_id"],
                     C.ACTION_NEEDS_CLARIFICATION, C.ACTION_REJECTED, C.ACTION_NOOP,
                     C.ACTION_SUCCEEDED, C.ACTION_FAILED, now_iso),
                )
                if cur.rowcount == 1:
                    fresh = conn.execute(
                        "SELECT processing_fence FROM conversation_actions WHERE action_id=?",
                        (row["action_id"],),
                    ).fetchone()
                    conn.commit()
                    return Reservation(
                        action_id=row["action_id"], is_new=True, status=row["status"],
                        processing_token=token, processing_fence=fresh[0], reclaimed=True,
                        intent=row["intent"], tool_name=row["tool_name"],
                        validated_arguments_json=row["validated_arguments_json"],
                    )
                conn.rollback()
            return Reservation(
                action_id=row["action_id"], is_new=False, status=row["status"],
                result_json=row["result_json"], intent=row["intent"],
                response_kind=row["response_kind"], response_text=row["response_text"],
                processing_token=row["processing_token"],
                processing_fence=row["processing_fence"],
                in_progress=row["status"] not in TERMINAL_ACTIONS,
                tool_name=row["tool_name"],
                validated_arguments_json=row["validated_arguments_json"],
            )
    finally:
        conn.close()


def _update(action_id, fields, *, conn=None, processing_token=None,
            processing_fence=None):
    own = conn is None
    if own:
        conn = connect()
    try:
        fields = dict(fields)
        fields["updated_at"] = _now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        where = "WHERE action_id = ?"
        params = list(fields.values()) + [action_id]
        if processing_token is not None and processing_fence is not None:
            where += " AND processing_token = ? AND processing_fence = ?"
            params.extend([processing_token, processing_fence])
        cur = conn.execute(f"UPDATE conversation_actions SET {cols} {where}", params)
        if processing_token is not None and cur.rowcount != 1:
            raise ActionLeaseLost("stale action worker cannot update action")
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def record_router(action_id, *, model, response_sha256, intent, confidence,
                  latency_ms=None, attempt_count=None, prompt_version=None,
                  relay_metadata=None,
                  processing_token=None, processing_fence=None):
    """Persist router metadata (no terminal status yet)."""
    fields = {
        "router_model": model,
        "router_schema_version": C.SCHEMA_VERSION,
        "prompt_version": prompt_version or C.PROMPT_VERSION,
        "router_response_sha256": response_sha256,
        "intent": intent,
        "confidence": confidence,
    }
    if latency_ms is not None:
        fields["latency_ms"] = latency_ms
    if attempt_count is not None:
        fields["attempt_count"] = attempt_count
    if relay_metadata is not None:
        # Only the transport's safe operational fields belong in the audit
        # row; never allow prompts, provider bodies, images or credentials.
        attempts = []
        for attempt in relay_metadata.get("attempts", []) if isinstance(relay_metadata, dict) else []:
            if isinstance(attempt, dict):
                attempts.append({key: attempt.get(key) for key in (
                    "model", "category", "status_code", "request_id"
                ) if attempt.get(key) is not None})
        fields["relay_metadata_json"] = json.dumps({
            "primary_model": relay_metadata.get("primary_model"),
            "fallback_model": relay_metadata.get("fallback_model"),
            "final_model": relay_metadata.get("final_model"),
            "final_outcome": relay_metadata.get("final_outcome"),
            "attempt_count": relay_metadata.get("attempt_count"),
            "request_id": relay_metadata.get("request_id"),
            "attempts": attempts,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    _update(action_id, fields, processing_token=processing_token,
            processing_fence=processing_fence)


def record_validated(action_id, *, intent, tool_name, validated_arguments,
                     processing_token, processing_fence):
    """Persist the deterministic resume payload under the current fencing lease."""
    now = _now_iso()
    conn = connect()
    try:
        cur = conn.execute(
            """UPDATE conversation_actions
               SET intent=?, tool_name=?, validated_arguments_json=?, updated_at=?
               WHERE action_id=? AND processing_token=? AND processing_fence=?
                 AND status=?""",
            (intent, tool_name, json.dumps(validated_arguments, ensure_ascii=False), now,
             action_id, processing_token, processing_fence, C.ACTION_RECEIVED),
        )
        conn.commit()
        if cur.rowcount != 1:
            raise ActionLeaseLost("action lease changed before validation checkpoint")
    finally:
        conn.close()


def mark_failed(action_id, error_code, error_detail=None, *,
                processing_token=None, processing_fence=None):
    """Router outage / permanent router error — no domain write happened."""
    _update(action_id, {
        "status": C.ACTION_FAILED,
        "error_code": error_code,
        # Details can contain model/transport text. The closed error code is
        # sufficient for audit and cannot copy user data into durable storage.
        "error_detail": None,
        "completed_at": _now_iso(),
    }, processing_token=processing_token, processing_fence=processing_fence)


def mark_rejected(action_id, error_code, error_detail=None, intent=None, confidence=None,
                  *, processing_token=None, processing_fence=None):
    fields = {
        "status": C.ACTION_REJECTED,
        "error_code": error_code,
        "error_detail": None,
        "completed_at": _now_iso(),
    }
    if intent is not None:
        fields["intent"] = intent
    if confidence is not None:
        fields["confidence"] = confidence
    _update(action_id, fields, processing_token=processing_token,
            processing_fence=processing_fence)


def mark_noop(action_id, intent, confidence=None, *, processing_token=None,
              processing_fence=None):
    fields = {"status": C.ACTION_NOOP, "intent": intent, "completed_at": _now_iso()}
    if confidence is not None:
        fields["confidence"] = confidence
    _update(action_id, fields, processing_token=processing_token,
            processing_fence=processing_fence)


def mark_clarification(action_id, intent, confidence=None, *,
                       processing_token=None, processing_fence=None):
    fields = {
        "status": C.ACTION_NEEDS_CLARIFICATION,
        "intent": intent,
        "completed_at": _now_iso(),
    }
    if confidence is not None:
        fields["confidence"] = confidence
    _update(action_id, fields, processing_token=processing_token,
            processing_fence=processing_fence)


def finalize_success_tx(conn, action_id, *, tool_name, validated_arguments,
                        result, processing_token=None, processing_fence=None):
    """Flip the audit row to succeeded WITHIN the tool's transaction.

    Does not commit — the caller's single BEGIN IMMEDIATE commits domain rows and
    this audit success together, so they can never diverge.
    """
    existing = conn.execute(
        """SELECT status, processing_token, processing_fence
           FROM conversation_actions WHERE action_id=?""",
        (action_id,),
    ).fetchone()
    if existing is not None and existing["status"] == C.ACTION_SUCCEEDED:
        if processing_token is not None and (
            existing["processing_token"] != processing_token
            or existing["processing_fence"] != processing_fence
        ):
            raise ActionLeaseLost("stale action worker cannot reuse succeeded action")
        return
    now = _now_iso()
    where = "WHERE action_id = ?"
    params = [
        C.ACTION_SUCCEEDED, tool_name,
        json.dumps(validated_arguments, ensure_ascii=False),
        json.dumps(result, ensure_ascii=False), now, now, action_id,
    ]
    if processing_token is not None and processing_fence is not None:
        where += " AND processing_token = ? AND processing_fence = ?"
        params.extend([processing_token, processing_fence])
    cur = conn.execute(
        """UPDATE conversation_actions
           SET status = ?, tool_name = ?, validated_arguments_json = ?,
                result_json = ?, error_code = NULL, error_detail = NULL,
                reply_delivery_status = 'response_missing',
                completed_at = ?, updated_at = ?, processing_claim_expires_at = NULL
           """ + where,
        params,
    )
    if processing_token is not None and cur.rowcount != 1:
        raise ActionLeaseLost("stale action worker cannot commit success")


def finalize_read(action_id, *, tool_name, validated_arguments, result,
                  processing_token=None, processing_fence=None):
    """Terminal success for a side-effect-free read tool (its own commit)."""
    audit_result = dict(result)
    data = audit_result.get("data")
    if isinstance(data, dict) and data.get("snapshot") == "metric_trend.v1":
        # The 84-day series is response evidence, not durable audit metadata.
        audit_result["data"] = {
            key: value for key, value in data.items() if key != "series"
        }
    _update(action_id, {
        "status": C.ACTION_SUCCEEDED,
        "tool_name": tool_name,
        "validated_arguments_json": json.dumps(validated_arguments, ensure_ascii=False),
        "result_json": json.dumps(audit_result, ensure_ascii=False),
        "reply_delivery_status": "response_missing",
        "completed_at": _now_iso(),
    }, processing_token=processing_token, processing_fence=processing_fence)


def mark_tool_failed(action_id, error_detail=None, tool_name=None, *,
                     processing_token=None, processing_fence=None):
    fields = {
        "status": C.ACTION_FAILED,
        "error_code": "tool_failed",
        "error_detail": None,
        "completed_at": _now_iso(),
    }
    if tool_name is not None:
        fields["tool_name"] = tool_name
    _update(action_id, fields, processing_token=processing_token,
            processing_fence=processing_fence)


def record_response(action_id, kind, text):
    """CAS-persist one bounded response without downgrading delivery state."""
    if text is not None:
        text = str(text)[:C.MAX_REPLY_TEXT_CHARS]
    conn = connect()
    try:
        now = _now_iso()
        cur = conn.execute(
            """UPDATE conversation_actions
               SET response_kind = ?, response_text = ?,
                   reply_delivery_status = ?, response_claimed_at = NULL,
                   updated_at = ?
               WHERE action_id = ? AND response_text IS NULL
                 AND IFNULL(reply_delivery_status, '') NOT IN ('sending', 'delivered')
                 AND (status != ? OR reply_delivery_status = 'response_missing')""",
            (
                str(kind)[:64], text, "pending" if text else "not_required",
                now, action_id, C.ACTION_SUCCEEDED,
            ),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def claim_response_delivery(action_id):
    """Claim one persisted response before calling Telegram."""
    conn = connect()
    try:
        now = _now_iso()
        cur = conn.execute(
            """UPDATE conversation_actions
               SET reply_delivery_status = 'sending', response_claimed_at = ?,
                   updated_at = ?
               WHERE action_id = ? AND response_text IS NOT NULL
                 AND reply_delivery_status IN ('pending', 'failed')""",
            (now, now, action_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def claim_retryable_responses(source, chat_id, *, limit=10, now=None):
    """Claim bounded succeeded responses, including stale interrupted sends."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    now_dt = now or dt.datetime.now(dt.timezone.utc)
    if now_dt.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_iso = now_dt.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    stale_iso = (now_dt.astimezone(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat(
        timespec="seconds"
    )
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT action_id, chat_id, response_text, response_kind
               FROM conversation_actions
               WHERE source = ? AND chat_id = ? AND status = ?
                 AND response_text IS NOT NULL
                 AND (reply_delivery_status IN ('pending', 'failed')
                      OR (reply_delivery_status = 'sending'
                          AND response_claimed_at <= ?))
               ORDER BY completed_at, created_at LIMIT ?""",
            (source, str(chat_id), C.ACTION_SUCCEEDED, stale_iso, limit),
        ).fetchall()
        claimed = []
        for row in rows:
            cur = conn.execute(
                """UPDATE conversation_actions
                   SET reply_delivery_status = 'sending', response_claimed_at = ?,
                       updated_at = ?
                   WHERE action_id = ? AND status = ?
                     AND (reply_delivery_status IN ('pending', 'failed')
                          OR (reply_delivery_status = 'sending'
                              AND response_claimed_at <= ?))""",
                (now_iso, now_iso, row["action_id"], C.ACTION_SUCCEEDED, stale_iso),
            )
            if cur.rowcount == 1:
                claimed.append(dict(row))
        conn.commit()
        return claimed
    finally:
        conn.close()


def missing_success_responses(source, chat_id, *, limit=10):
    """Return committed results whose deterministic response was not persisted.

    This is a read-only precursor to ``record_response`` + the normal atomic
    delivery claim. Concurrent workers may reconstruct the same text, but only
    one can win ``claim_response_delivery``.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT action_id, chat_id, intent, validated_arguments_json,
                      result_json
               FROM conversation_actions
               WHERE source = ? AND chat_id = ? AND status = ?
                 AND response_text IS NULL AND result_json IS NOT NULL
                 AND reply_delivery_status = 'response_missing'
               ORDER BY completed_at, created_at LIMIT ?""",
            (source, str(chat_id), C.ACTION_SUCCEEDED, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def mark_response_delivery(action_id, *, delivered, message_id=None):
    conn = connect()
    try:
        conn.execute(
            """UPDATE conversation_actions
               SET reply_delivery_status = ?, reply_message_id = ?,
                   reply_attempt_count = reply_attempt_count + 1, updated_at = ?
                   , response_claimed_at = NULL
               WHERE action_id = ?""",
            (
                "delivered" if delivered else "failed",
                str(message_id) if message_id is not None else None,
                _now_iso(), action_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def recover_stale_pending_claims(*, now=None, limit=100):
    """Recover expired clarification leases from the claimed action status.

    Successful actions finalize the pending row. Terminal no-write failures may
    reopen it while its user-facing TTL is still live. Indeterminate actions are
    expired fail-closed and are never replayed automatically.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    now_dt = now or dt.datetime.now(dt.timezone.utc)
    if now_dt.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_iso = now_dt.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    conn = connect()
    counts = {"resolved": 0, "reopened": 0, "expired": 0}
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT p.pending_id, p.expires_at, p.claimed_by_action_id,
                      a.status AS action_status
               FROM pending_actions p
               LEFT JOIN conversation_actions a
                 ON a.action_id = p.claimed_by_action_id
               WHERE p.status = ? AND p.claim_expires_at <= ?
               ORDER BY p.claim_expires_at LIMIT ?""",
            (C.PENDING_RESOLVING, now_iso, limit),
        ).fetchall()
        terminal_no_write = {
            C.ACTION_FAILED, C.ACTION_REJECTED, C.ACTION_NOOP,
        }
        for row in rows:
            status = row["action_status"]
            if status == C.ACTION_SUCCEEDED:
                target = C.PENDING_RESOLVED
                counts["resolved"] += 1
            elif status in terminal_no_write and row["expires_at"] > now_iso:
                target = C.PENDING_OPEN
                counts["reopened"] += 1
            else:
                target = C.PENDING_EXPIRED
                counts["expired"] += 1
            conn.execute(
                """UPDATE pending_actions
                   SET status = ?, resolved_at = CASE WHEN ? = ? THEN ? ELSE resolved_at END,
                       resolved_by_action_id = CASE WHEN ? = ? THEN claimed_by_action_id ELSE NULL END,
                       claimed_by_action_id = NULL, claimed_at = NULL,
                       claim_expires_at = NULL, updated_at = ?
                   WHERE pending_id = ? AND status = ?""",
                (target, target, C.PENDING_RESOLVED, now_iso,
                 target, C.PENDING_RESOLVED, now_iso,
                 row["pending_id"], C.PENDING_RESOLVING),
            )
        conn.commit()
        return sum(counts.values())
    finally:
        conn.close()


def expire_stale_pending(*, now=None, limit=100):
    """Boundedly expire idle open clarifications without user activity."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    now_dt = now or dt.datetime.now(dt.timezone.utc)
    if now_dt.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_iso = now_dt.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """UPDATE pending_actions SET status = ?, updated_at = ?
               WHERE pending_id IN (
                   SELECT pending_id FROM pending_actions
                   WHERE status = ? AND expires_at <= ?
                   ORDER BY expires_at LIMIT ?
               )""",
            (C.PENDING_EXPIRED, now_iso, C.PENDING_OPEN, now_iso, limit),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def redact_old_action_payloads(*, before, limit=100):
    """Bounded retention cleanup; preserves correlation and idempotency keys."""
    if before.tzinfo is None:
        raise ValueError("before must be timezone-aware")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    before_iso = before.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    conn = connect()
    try:
        cur = conn.execute(
            """UPDATE conversation_actions
               SET validated_arguments_json = CASE
                     WHEN tool_name IN ('log_strength_workout','log_cardio',
                                        'log_supplement','save_daily_context',
                                        'soft_delete_activity','correct_activity')
                     THEN validated_arguments_json ELSE NULL END,
                   result_json = CASE
                     WHEN tool_name IN ('log_strength_workout','log_cardio',
                                        'log_supplement','save_daily_context',
                                        'soft_delete_activity','correct_activity')
                     THEN result_json ELSE NULL END,
                   response_text = NULL, error_detail = NULL, updated_at = ?
               WHERE action_id IN (
                   SELECT action_id FROM conversation_actions
                   WHERE completed_at < ? AND status IN (?, ?, ?, ?)
                      AND (response_text IS NOT NULL OR error_detail IS NOT NULL
                           OR (tool_name NOT IN ('log_strength_workout','log_cardio',
                                                'log_supplement','save_daily_context',
                                                'soft_delete_activity','correct_activity')
                               AND (validated_arguments_json IS NOT NULL
                                    OR result_json IS NOT NULL)))
                   ORDER BY completed_at LIMIT ?
               )""",
            (_now_iso(), before_iso, C.ACTION_SUCCEEDED, C.ACTION_FAILED,
             C.ACTION_REJECTED, C.ACTION_NOOP, limit),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def get_action(action_id):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM conversation_actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Pending clarifications
# --------------------------------------------------------------------------- #

def _expire_stale(conn, now):
    conn.execute(
        """UPDATE pending_actions SET status = ?, updated_at = ?
           WHERE status = ? AND expires_at <= ?""",
        (C.PENDING_EXPIRED, now, C.PENDING_OPEN, now),
    )


def create_pending(origin_action_id, ctx: ActionContext, candidate_intent,
                   partial_arguments, missing_fields, ttl_minutes=None):
    """Open a clarification, enforcing one active pending per chat/user.

    Any previously open pending for the same chat/user is expired first.
    """
    ttl = ttl_minutes if ttl_minutes is not None else C.PENDING_TTL_MINUTES
    now_dt = dt.datetime.now(dt.timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    expires_at = (now_dt + dt.timedelta(minutes=ttl)).isoformat(timespec="seconds")
    pending_id = str(uuid.uuid4())
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _expire_stale(conn, now)
        conn.execute(
            """UPDATE pending_actions SET status = ?, updated_at = ?
               WHERE status = ? AND source = ? AND chat_id = ?
                 AND IFNULL(user_id,'') = IFNULL(?,'')""",
            (C.PENDING_EXPIRED, now, C.PENDING_OPEN, ctx.source, str(ctx.chat_id),
             str(ctx.user_id) if ctx.user_id is not None else None),
        )
        conn.execute(
            """INSERT INTO pending_actions
               (pending_id, origin_action_id, source, chat_id, user_id,
                candidate_intent, partial_arguments_json, missing_fields_json,
                status, attempt_count, expires_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pending_id, origin_action_id, ctx.source, str(ctx.chat_id),
                str(ctx.user_id) if ctx.user_id is not None else None,
                candidate_intent,
                json.dumps(partial_arguments, ensure_ascii=False),
                json.dumps(list(missing_fields or []), ensure_ascii=False),
                C.PENDING_OPEN, 0, expires_at, now, now,
            ),
        )
        conn.commit()
        return pending_id
    finally:
        conn.close()


def set_clarification_message_id(pending_id, message_id):
    _now = _now_iso()
    conn = connect()
    try:
        conn.execute(
            """UPDATE pending_actions
               SET clarification_question_message_id = ?, updated_at = ?
               WHERE pending_id = ?""",
            (str(message_id) if message_id is not None else None, _now, pending_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_pending_arguments(pending_id, transform):
    """Atomically update one open pending payload under BEGIN IMMEDIATE.

    ``transform`` receives defensive copies of arguments and missing fields and
    returns their replacements. Serializing here prevents two rapid Telegram
    updates from dropping one another's exercise sets.
    """
    now = _now_iso()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _expire_stale(conn, now)
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE pending_id = ? AND status = ?",
            (pending_id, C.PENDING_OPEN),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        arguments = json.loads(row["partial_arguments_json"])
        missing = json.loads(row["missing_fields_json"])
        new_arguments, new_missing = transform(dict(arguments), list(missing))
        conn.execute(
            """UPDATE pending_actions
                  SET partial_arguments_json = ?, missing_fields_json = ?,
                      clarification_question_message_id = NULL, updated_at = ?
                WHERE pending_id = ? AND status = ?""",
            (json.dumps(new_arguments, ensure_ascii=False),
             json.dumps(new_missing, ensure_ascii=False), now,
             pending_id, C.PENDING_OPEN),
        )
        conn.commit()
        return {**dict(row), "partial_arguments_json": json.dumps(new_arguments, ensure_ascii=False),
                "missing_fields_json": json.dumps(new_missing, ensure_ascii=False)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cancel_undelivered_pending(pending_id):
    """Expire a clarification that the user never received."""
    now = _now_iso()
    conn = connect()
    try:
        cur = conn.execute(
            """UPDATE pending_actions SET status = ?, updated_at = ?
               WHERE pending_id = ? AND status = ?
                 AND clarification_question_message_id IS NULL""",
            (C.PENDING_EXPIRED, now, pending_id, C.PENDING_OPEN),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def active_pending(source, chat_id, user_id=None):
    """Return the single open, unexpired pending for this chat/user, if any."""
    now = _now_iso()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _expire_stale(conn, now)
        conn.commit()
        row = conn.execute(
            """SELECT * FROM pending_actions
               WHERE status = ? AND source = ? AND chat_id = ?
                 AND IFNULL(user_id,'') = IFNULL(?,'')
               ORDER BY created_at DESC LIMIT 1""",
            (C.PENDING_OPEN, source, str(chat_id),
             str(user_id) if user_id is not None else None),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_pending(pending_id):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM pending_actions WHERE pending_id = ?", (pending_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def claim_pending_for_resolution(pending_id, resolved_by_action_id):
    """Atomically claim one clarification before any domain tool can run."""
    now_dt = dt.datetime.now(dt.timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    claim_expires = (now_dt + dt.timedelta(minutes=5)).isoformat(timespec="seconds")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _expire_stale(conn, now)
        cur = conn.execute(
            """UPDATE pending_actions
               SET status = ?, resolved_by_action_id = ?,
                   claimed_by_action_id = ?, claimed_at = ?, claim_expires_at = ?,
                   updated_at = ?
               WHERE pending_id = ? AND status = ?""",
            (C.PENDING_RESOLVING, resolved_by_action_id, resolved_by_action_id,
             now, claim_expires, now, pending_id, C.PENDING_OPEN),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def release_pending_claim(pending_id, resolved_by_action_id):
    """Return a transiently failed resolution to its open state."""
    now = _now_iso()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """UPDATE pending_actions
               SET status = ?, resolved_by_action_id = NULL,
                   claimed_by_action_id = NULL, claimed_at = NULL,
                   claim_expires_at = NULL, updated_at = ?
               WHERE pending_id = ? AND status = ?
                 AND resolved_by_action_id = ? AND expires_at > ?""",
            (C.PENDING_OPEN, now, pending_id, C.PENDING_RESOLVING,
             resolved_by_action_id, now),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def resolve_pending(pending_id, resolved_by_action_id):
    """One-use resolution after an atomic claim; accepts legacy open rows."""
    now = _now_iso()
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """UPDATE pending_actions
               SET status = ?, resolved_at = ?, resolved_by_action_id = ?,
                   claimed_by_action_id = NULL, claimed_at = NULL,
                   claim_expires_at = NULL, updated_at = ?
               WHERE pending_id = ? AND status IN (?, ?)
                 AND (resolved_by_action_id IS NULL OR resolved_by_action_id = ?)""",
            (C.PENDING_RESOLVED, now, resolved_by_action_id, now, pending_id,
             C.PENDING_OPEN, C.PENDING_RESOLVING, resolved_by_action_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()
