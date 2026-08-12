"""Shared Europe/Kyiv readiness rules for the durable morning workflow."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import daily_log


HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "data" / "whoop.db"
TZ = ZoneInfo("Europe/Kyiv")
CONTEXT_ONLY_CUTOFF = dt.time(23, 0)


def _local_date(value):
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(TZ).date()


def morning_data_status(recovery_date, db_path=None):
    """Return whether D has complete Recovery/HRV/RHR and non-nap Sleep data."""
    target = recovery_date if isinstance(recovery_date, dt.date) else dt.date.fromisoformat(str(recovery_date))
    path = Path(db_path or daily_log.DB_PATH)
    status = {
        "recovery": False, "sleep": False, "ready": False, "error": None,
    }
    if not path.exists():
        status["error"] = "database_missing"
        return status
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        for row in conn.execute(
            "SELECT created_at, recovery_score, hrv_rmssd, resting_hr FROM recovery"
        ):
            if (_local_date(row["created_at"]) == target
                    and all(row[key] is not None for key in
                            ("recovery_score", "hrv_rmssd", "resting_hr"))):
                status["recovery"] = True
                break
        for row in conn.execute("SELECT end, raw_json, performance_pct FROM sleep"):
            raw = json.loads(row["raw_json"] or "{}")
            if raw.get("nap"):
                continue
            if _local_date(row["end"]) == target and row["performance_pct"] is not None:
                status["sleep"] = True
                break
    except sqlite3.Error:
        status["error"] = "database_read_failed"
        return status
    except (ValueError, TypeError, json.JSONDecodeError):
        status["error"] = "canonical_payload_invalid"
        return status
    finally:
        conn.close()
    status["ready"] = status["recovery"] and status["sleep"]
    return status


def analysis_mode(recovery_date, now=None, db_path=None):
    """Choose grounded WHOOP mode, or context-only after the documented cutoff."""
    target = recovery_date if isinstance(recovery_date, dt.date) else dt.date.fromisoformat(str(recovery_date))
    status = morning_data_status(target, db_path=db_path)
    if status["error"]:
        return None
    if status["ready"]:
        return "whoop"
    current = now or dt.datetime.now(TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TZ)
    local_now = current.astimezone(TZ)
    if local_now.date() > target or (
        local_now.date() == target and local_now.time().replace(tzinfo=None) >= CONTEXT_ONLY_CUTOFF
    ):
        return "context_only"
    return None
