"""Durable, restart-safe progress for multi-message Telegram reports."""
from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import daily_log


CLAIM_TIMEOUT = dt.timedelta(minutes=15)
STATUS_PENDING = "pending"
STATUS_SENDING = "sending"
STATUS_DELIVERED = "delivered"


class DeliveryConflict(RuntimeError):
    pass


def _now(value=None):
    result = value or dt.datetime.now(dt.timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=dt.timezone.utc)
    return result


def _digest(payload):
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _connect():
    conn = daily_log.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_deliveries (
            delivery_key TEXT PRIMARY KEY,
            report_kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            total_chunks INTEGER NOT NULL,
            next_chunk INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            claim_token TEXT,
            claimed_at TEXT,
            delivered_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (status IN ('pending', 'sending', 'delivered')),
            CHECK (total_chunks > 0),
            CHECK (next_chunk >= 0 AND next_chunk <= total_chunks)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_report_deliveries_status "
        "ON report_deliveries(status, updated_at)"
    )
    conn.commit()
    return conn


def get(delivery_key):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM report_deliveries WHERE delivery_key=?",
            (str(delivery_key),),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def saved_payload(delivery_key):
    row = get(delivery_key)
    return row["payload"] if row is not None else None


def prepare(delivery_key, report_kind, payload, total_chunks, now=None):
    body = str(payload or "")
    if not body or int(total_chunks) <= 0:
        raise ValueError("report delivery requires a non-empty payload and chunks")
    key = str(delivery_key)
    digest = _digest(body)
    now_iso = _now(now).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT OR IGNORE INTO report_deliveries
               (delivery_key, report_kind, payload, payload_sha256, total_chunks,
                next_chunk, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (
                key, str(report_kind), body, digest, int(total_chunks),
                STATUS_PENDING, now_iso, now_iso,
            ),
        )
        row = conn.execute(
            "SELECT * FROM report_deliveries WHERE delivery_key=?", (key,)
        ).fetchone()
        if (
            row["report_kind"] != str(report_kind)
            or row["payload_sha256"] != digest
            or int(row["total_chunks"]) != int(total_chunks)
        ):
            conn.rollback()
            raise DeliveryConflict(
                "an existing delivery key is bound to a different immutable report"
            )
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def claim(delivery_key, now=None):
    claim_time = _now(now)
    now_iso = claim_time.isoformat(timespec="seconds")
    stale_before = (claim_time - CLAIM_TIMEOUT).isoformat(timespec="seconds")
    token = str(uuid.uuid4())
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """UPDATE report_deliveries
               SET status=?, claim_token=?, claimed_at=?, updated_at=?
               WHERE delivery_key=? AND status != ?
                 AND (status=? OR claimed_at IS NULL OR claimed_at <= ?)""",
            (
                STATUS_SENDING, token, now_iso, now_iso, str(delivery_key),
                STATUS_DELIVERED, STATUS_PENDING, stale_before,
            ),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        row = conn.execute(
            "SELECT * FROM report_deliveries WHERE delivery_key=?",
            (str(delivery_key),),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def advance(delivery_key, claim_token, expected_chunk, now=None):
    now_iso = _now(now).isoformat(timespec="seconds")
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT total_chunks FROM report_deliveries
               WHERE delivery_key=? AND status=? AND claim_token=? AND next_chunk=?""",
            (
                str(delivery_key), STATUS_SENDING, str(claim_token),
                int(expected_chunk),
            ),
        ).fetchone()
        if row is None:
            conn.rollback()
            return False
        next_chunk = int(expected_chunk) + 1
        delivered = next_chunk == int(row["total_chunks"])
        conn.execute(
            """UPDATE report_deliveries
               SET next_chunk=?, status=?, claim_token=?, claimed_at=?,
                   delivered_at=?, updated_at=?
               WHERE delivery_key=? AND status=? AND claim_token=? AND next_chunk=?""",
            (
                next_chunk,
                STATUS_DELIVERED if delivered else STATUS_SENDING,
                None if delivered else str(claim_token),
                None if delivered else now_iso,
                now_iso if delivered else None,
                now_iso,
                str(delivery_key), STATUS_SENDING, str(claim_token),
                int(expected_chunk),
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def release(delivery_key, claim_token, now=None):
    now_iso = _now(now).isoformat(timespec="seconds")
    conn = _connect()
    try:
        cur = conn.execute(
            """UPDATE report_deliveries
               SET status=?, claim_token=NULL, claimed_at=NULL, updated_at=?
               WHERE delivery_key=? AND status=? AND claim_token=?""",
            (
                STATUS_PENDING, now_iso, str(delivery_key),
                STATUS_SENDING, str(claim_token),
            ),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def deliver(delivery_key, report_kind, payload, chunks, send_chunk, now=None):
    """Send only unsent chunks and durably advance after each confirmed response."""
    pieces = list(chunks)
    row = prepare(delivery_key, report_kind, payload, len(pieces), now=now)
    if row["status"] == STATUS_DELIVERED:
        return True
    claimed = claim(delivery_key, now=now)
    if claimed is None:
        return False
    token = claimed["claim_token"]
    try:
        for index in range(int(claimed["next_chunk"]), len(pieces)):
            sent = send_chunk(pieces[index])
            if sent is None or sent is False:
                raise RuntimeError("telegram delivery was not confirmed")
            if not advance(delivery_key, token, index, now=now):
                raise RuntimeError("delivery progress ownership changed")
        return True
    except Exception:
        release(delivery_key, token, now=now)
        raise
