"""Six allowlisted, deterministic conversation tools.

Gemini never selects a tool: the router maps an already-validated intent to
exactly one function here via a static table. Each mutation runs a single
`BEGIN IMMEDIATE` transaction so every domain row and the audit-success row
commit together; any error rolls the whole batch back and records a failure.

Idempotency: each domain row carries a deterministic `source_key` built from the
action, so a duplicate action (or a re-run) inserts nothing new.
"""
from __future__ import annotations

import json
import datetime as dt
from dataclasses import dataclass
from typing import Optional

import conversation_contract as C
import conversation_read_models as read_models
import conversation_evidence as evidence
import factor_capture
import phase2_flags
import phase2_store
import conversation_store as store
import daily_log
import workouts_db


class ToolError(Exception):
    pass


@dataclass
class ExecContext:
    action_id: str
    source: str
    chat_id: str
    message_id: str
    local_now: object  # tz-aware datetime (Europe/Kyiv)
    reply_to_message_id: Optional[str] = None
    processing_token: Optional[str] = None
    processing_fence: Optional[int] = None
    pending_id: Optional[str] = None

    def source_key(self, kind, index=0):
        return f"{self.source}:{self.chat_id}:{self.message_id}:{kind}:{index}"


def _raw_marker(action_id):
    # Domain raw_text is not rendered anywhere; keep only a correlation marker,
    # never the raw user health text.
    return f"conversation:{action_id}"


def _minutes_to_hms(minutes):
    if minutes is None:
        return None
    total = int(round(float(minutes) * 60))
    if total < 0:
        return None
    if total == 0:
        return "0:00"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _canonical_cardio_row(row):
    """Return the safe persisted cardio projection used by confirmations.

    This deliberately accepts only a SQLite row that has already been written;
    provider output is never rendered directly to the user.
    """
    if row is None:
        return None
    value = dict(row)
    return {
        "id": value.get("id"),
        "date": value.get("date"),
        "activity_type": value.get("type"),
        "duration_minutes": value.get("duration"),
        "strain": value.get("strain"),
        "steps": value.get("steps"),
        "calories_kcal": value.get("calories"),
        "avg_hr_bpm": value.get("avg_hr"),
        "zones": {
            str(index): value.get(f"hr_zone_{index}_duration")
            for index in range(6)
        },
    }


# --------------------------------------------------------------------------- #
# Mutation tools
# --------------------------------------------------------------------------- #

def _log_strength(args, ctx):
    date = args["resolved_date"]
    raw = _raw_marker(ctx.action_id)
    conn = store.connect()
    try:
        workouts_db.ensure_tables(conn)  # activity tables live on the same db
        conn.execute("BEGIN IMMEDIATE")
        record_ids = []
        duplicate_count = 0
        for i, entry in enumerate(args["entries"]):
            source_key = ctx.source_key("workout", i)
            row_id = workouts_db.insert_workout_row(
                conn, date, entry["exercise_name"], entry["weight_kg"],
                entry["sets"] if entry["sets"] is not None else 1,
                entry["reps"], raw_text=raw,
                source_key=source_key, origin_action_id=ctx.action_id,
            )
            if row_id is not None:
                workouts_db.link_action_domain(
                    conn, ctx.action_id, "strength", row_id, source_key
                )
                record_ids.append(row_id)
            else:
                duplicate_count += 1
        canonical_entries = []
        if record_ids:
            placeholders = ",".join("?" for _ in record_ids)
            rows = conn.execute(
                f"""SELECT id, date, exercise_name, weight, sets, reps
                       FROM workout_exercises WHERE id IN ({placeholders})
                       ORDER BY id""", record_ids,
            ).fetchall()
            canonical_entries = [dict(row) for row in rows]
        result = {
            "status": "success", "action_id": ctx.action_id,
            "created_count": len(record_ids), "record_ids": record_ids,
            "duplicate_count": duplicate_count,
            "resolved_date": date, "data": {"entries": canonical_entries},
        }
        if ctx.pending_id and not phase2_store.finalize_pending_resolution(
            conn, ctx.pending_id, ctx.action_id
        ):
            raise ToolError("pending strength confirmation ownership changed")
        store.finalize_success_tx(conn, ctx.action_id, tool_name="log_strength_workout",
                                  validated_arguments=args, result=result,
                                  processing_token=ctx.processing_token,
                                  processing_fence=ctx.processing_fence)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _log_cardio(args, ctx):
    date = args["resolved_date"]
    raw = _raw_marker(ctx.action_id)
    time_str = args.get("start_time") or ctx.local_now.strftime("%H:%M")
    zones = args.get("hr_zone_minutes") or []
    zone_texts = [None] * 6
    for i in range(min(len(zones), 6)):
        zone_texts[i] = _minutes_to_hms(zones[i])
    conn = store.connect()
    try:
        workouts_db.ensure_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        source_key = ctx.source_key("cardio", 0)
        row_id = workouts_db.insert_cardio_row(
            conn, date, time_str, args["activity_type"], args["duration_minutes"],
            args.get("distance_km"), args.get("avg_hr_bpm"), args.get("calories_kcal"),
            zone_texts[0], zone_texts[1], zone_texts[2], zone_texts[3],
            zone_texts[4], zone_texts[5], raw_text=raw,
            source_key=source_key, origin_action_id=ctx.action_id,
            strain=args.get("strain"), max_hr=args.get("max_hr_bpm"),
            steps=args.get("steps"),
        )
        if row_id is not None:
            workouts_db.link_action_domain(
                conn, ctx.action_id, "cardio", row_id, source_key
            )
        persisted = conn.execute(
            """SELECT id, date, type, duration, strain, steps, calories, avg_hr,
                      hr_zone_0_duration, hr_zone_1_duration,
                      hr_zone_2_duration, hr_zone_3_duration,
                      hr_zone_4_duration, hr_zone_5_duration
                 FROM cardio_exercises
                WHERE id = ? OR source_key = ?
                ORDER BY CASE WHEN id = ? THEN 0 ELSE 1 END LIMIT 1""",
            (row_id, source_key, row_id),
        ).fetchone()
        record_ids = [row_id] if row_id is not None else []
        result = {
            "status": "success", "action_id": ctx.action_id,
            "created_count": len(record_ids), "record_ids": record_ids,
            "duplicate_count": 0 if row_id is not None else 1,
            "resolved_date": date, "data": {"cardio": _canonical_cardio_row(persisted)},
        }
        if ctx.pending_id and not phase2_store.finalize_pending_resolution(
            conn, ctx.pending_id, ctx.action_id
        ):
            raise ToolError("pending cardio clarification ownership changed")
        store.finalize_success_tx(conn, ctx.action_id, tool_name="log_cardio",
                                  validated_arguments=args, result=result,
                                  processing_token=ctx.processing_token,
                                  processing_fence=ctx.processing_fence)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _log_supplement(args, ctx):
    date = args["resolved_date"]
    raw = _raw_marker(ctx.action_id)
    time_str = args.get("time") or ctx.local_now.strftime("%H:%M")
    conn = store.connect()
    try:
        workouts_db.ensure_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        record_ids = []
        duplicate_count = 0
        for i, item in enumerate(args["items"]):
            source_key = ctx.source_key("supplement", i)
            row_id = workouts_db.insert_supplement_row(
                conn, date, time_str, item["name"], item.get("dose_text"),
                1 if item["taken"] else 0, raw_text=raw,
                source_key=source_key, origin_action_id=ctx.action_id,
            )
            if row_id is not None:
                workouts_db.link_action_domain(
                    conn, ctx.action_id, "supplement", row_id, source_key
                )
                record_ids.append(row_id)
            else:
                duplicate_count += 1
        result = {
            "status": "success", "action_id": ctx.action_id,
            "created_count": len(record_ids), "record_ids": record_ids,
            "duplicate_count": duplicate_count,
            "resolved_date": date, "data": {"items": len(args["items"])},
        }
        store.finalize_success_tx(conn, ctx.action_id, tool_name="log_supplement",
                                  validated_arguments=args, result=result,
                                  processing_token=ctx.processing_token,
                                  processing_fence=ctx.processing_fence)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _save_daily_context(args, ctx):
    date = args["resolved_date"]
    conn = store.connect()
    try:
        phase2_store.migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        entry_id, inserted, projection = daily_log.append_entry_tx(
            conn, date, args["notes"], label="Daily context",
            source_key=ctx.source_key("daily_context", 0),
            origin_action_id=ctx.action_id,
        )
        factor_job_id, factor_job_created = phase2_store.enqueue_factor_job(
            conn, context_date=date,
            projection_hash=projection["projection_hash"],
            projection_revision=projection["revision"],
            extractor_version=factor_capture.SCHEMA_VERSION,
            origin_action_id=ctx.action_id,
            enabled=phase2_flags.factor_capture_enabled(),
        )
        result = {
            "status": "success", "action_id": ctx.action_id,
            "created_count": int(inserted), "record_ids": [entry_id],
            "duplicate_count": int(not inserted),
            "resolved_date": date,
            "data": {
                "notes_len": len((projection or {}).get("notes") or ""),
                "factor_job_id": factor_job_id,
                "factor_job_created": factor_job_created,
            },
        }
        # The audit ledger stores hashes of raw router I/O, never raw health
        # text — redact notes to a length marker before it is persisted.
        audit_args = {**args, "notes": None, "notes_len": len(args["notes"])}
        store.finalize_success_tx(conn, ctx.action_id, tool_name="save_daily_context",
                                  validated_arguments=audit_args, result=result,
                                  processing_token=ctx.processing_token,
                                  processing_fence=ctx.processing_fence)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Read tools (side-effect free, no domain write)
# --------------------------------------------------------------------------- #

def _get_today_status(args, ctx):
    conn = store.connect()
    try:
        snapshot = read_models.today_status(conn, ctx.local_now)
    finally:
        conn.close()
    result = {
        "status": "success", "action_id": ctx.action_id,
        "created_count": 0, "record_ids": [], "resolved_date": None,
        "data": snapshot,
    }
    store.finalize_read(ctx.action_id, tool_name="get_today_status",
                        validated_arguments=args, result=result,
                        processing_token=ctx.processing_token,
                        processing_fence=ctx.processing_fence)
    return result


def _get_week_summary(args, ctx):
    conn = store.connect()
    try:
        snapshot = (
            evidence.weekly_evidence(conn, ctx.local_now)
            if phase2_flags.analytics_v2_enabled()
            else read_models.week_summary(conn, ctx.local_now)
        )
    finally:
        conn.close()
    result = {
        "status": "success", "action_id": ctx.action_id,
        "created_count": 0, "record_ids": [], "resolved_date": None,
        "data": snapshot,
    }
    store.finalize_read(ctx.action_id, tool_name="get_week_summary",
                        validated_arguments=args, result=result,
                        processing_token=ctx.processing_token,
                        processing_fence=ctx.processing_fence)
    return result


def _get_metric_trend(args, ctx):
    conn = store.connect()
    try:
        snapshot = evidence.metric_trend(
            conn, args["metric"], args["window_days"], ctx.local_now
        )
    finally:
        conn.close()
    result = {
        "status": "success", "action_id": ctx.action_id,
        "created_count": 0, "record_ids": [], "resolved_date": None,
        "data": snapshot,
    }
    store.finalize_read(ctx.action_id, tool_name="get_metric_trend",
                        validated_arguments=args, result=result,
                        processing_token=ctx.processing_token,
                        processing_fence=ctx.processing_fence)
    return result


def _get_factor_observation(args, ctx):
    conn = store.connect()
    try:
        snapshot = evidence.factor_observation(
            conn, args["factor_type"], args["factor_key"],
            args["window_days"], ctx.local_now,
        )
    finally:
        conn.close()
    result = {
        "status": "success", "action_id": ctx.action_id,
        "created_count": 0, "record_ids": [], "resolved_date": None,
        "data": snapshot,
    }
    store.finalize_read(ctx.action_id, tool_name="get_factor_observation",
                        validated_arguments=args, result=result,
                        processing_token=ctx.processing_token,
                        processing_fence=ctx.processing_fence)
    return result


def _finalize_extra_read(tool_name, args, ctx, snapshot):
    result = {"status": "success", "action_id": ctx.action_id, "created_count": 0,
              "record_ids": [], "resolved_date": None, "data": snapshot}
    store.finalize_read(ctx.action_id, tool_name=tool_name, validated_arguments=args, result=result,
                        processing_token=ctx.processing_token, processing_fence=ctx.processing_fence)
    return result


def _get_day_snapshot(args, ctx):
    conn = store.connect()
    try: snapshot = read_models.day_snapshot(conn, args["date"])
    finally: conn.close()
    return _finalize_extra_read("get_day_snapshot", args, ctx, snapshot)


def _get_data_coverage(args, ctx):
    conn = store.connect()
    try: snapshot = read_models.data_coverage(conn, ctx.local_now)
    finally: conn.close()
    return _finalize_extra_read("get_data_coverage", args, ctx, snapshot)


def _get_supplement_records(args, ctx):
    conn = store.connect()
    try: snapshot = read_models.supplement_records(conn, ctx.local_now)
    finally: conn.close()
    return _finalize_extra_read("get_supplement_records", args, ctx, snapshot)


# --------------------------------------------------------------------------- #
# Static allowlist + dispatch
# --------------------------------------------------------------------------- #

_TOOLS = {
    "log_strength_workout": _log_strength,
    "log_cardio": _log_cardio,
    "log_supplement": _log_supplement,
    "save_daily_context": _save_daily_context,
    "get_today_status": _get_today_status,
    "get_week_summary": _get_week_summary,
    "get_metric_trend": _get_metric_trend,
    "get_factor_observation": _get_factor_observation,
    "get_day_snapshot": _get_day_snapshot,
    "get_data_coverage": _get_data_coverage,
    "get_supplement_records": _get_supplement_records,
}


def execute(tool_name, args, ctx: ExecContext):
    """Run one allowlisted tool. Raises ToolError for an unknown/disallowed tool."""
    if tool_name not in C.ALLOWED_TOOLS or tool_name not in _TOOLS:
        raise ToolError(f"unsupported tool: {tool_name}")
    return _TOOLS[tool_name](args, ctx)
