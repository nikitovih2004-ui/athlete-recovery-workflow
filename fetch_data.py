"""
Подтягивает тренировки, recovery и сон из WHOOP API и сохраняет в data/whoop.db (SQLite).
Можно запускать сколько угодно раз — данные обновляются (upsert), дублей не будет.

    python fetch_data.py            # последние 30 дней
    python fetch_data.py --days 90  # последние 90 дней
"""
import argparse
import datetime
import json
import os
import sqlite3

import requests
from dotenv import load_dotenv

import morning_observability
import morning_readiness
from whoop_auth import (
    WhoopAuthError,
    get_valid_access_token,
    record_runtime_fact,
)

load_dotenv()

CLIENT_ID = os.environ["WHOOP_CLIENT_ID"]
CLIENT_SECRET = os.environ["WHOOP_CLIENT_SECRET"]

BASE = "https://api.prod.whoop.com/developer/v2"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "whoop.db")


def _record_runtime_fact_best_effort(name):
    try:
        record_runtime_fact(name)
    except OSError:
        pass


def _provider_error_reason(component, exc):
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return (
            f"provider_{component}_http_status={int(status)}"
            if status is not None
            else f"provider_{component}_http_error"
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return f"provider_{component}_timeout"
    if isinstance(exc, requests.exceptions.RequestException):
        return f"provider_{component}_{type(exc).__name__}"
    return morning_observability.safe_exception_reason(
        exc, prefix=f"provider_{component}"
    )


def _auth_observability(client_id, client_secret, pipeline_date, run_id):
    attempted = morning_observability.StageTimer.start(
        pipeline_date, run_id, "whoop_refresh_attempted"
    )
    result_timer = morning_observability.StageTimer.start(
        pipeline_date, run_id, "whoop_refresh_result"
    )
    try:
        auth = get_valid_access_token(
            client_id, client_secret, return_metadata=True
        )
    except WhoopAuthError as exc:
        did_post = exc.category not in {
            "token_transferred_to_production",
            "tokens_missing",
            "refresh_token_missing",
            "refresh_outcome_ambiguous",
        }
        attempted.finish(
            "success" if did_post else "skipped",
            "refresh_http_post_attempted" if did_post
            else f"refresh_not_attempted:{exc.category}",
        )
        result_timer.finish(
            "failed",
            morning_observability.safe_exception_reason(
                exc, prefix="whoop_oauth"
            ),
        )
        raise
    except Exception as exc:
        attempted.finish(
            "failed",
            morning_observability.safe_exception_reason(
                exc, prefix="refresh_precondition"
            ),
        )
        result_timer.finish(
            "failed",
            morning_observability.safe_exception_reason(
                exc, prefix="whoop_oauth"
            ),
        )
        raise

    # Compatibility with injected test doubles that return the legacy string.
    if isinstance(auth, dict):
        access_token = auth["access_token"]
        refresh_performed = bool(auth.get("refresh_performed"))
        reason = str(auth.get("reason") or "unknown_auth_result")
    else:
        access_token = auth
        refresh_performed = False
        reason = "legacy_access_token_result"
    attempted.finish(
        "success" if refresh_performed else "skipped",
        "refresh_http_post_attempted" if refresh_performed
        else "access_token_current_no_refresh_required",
    )
    result_timer.finish("success", reason)
    return access_token


def get_json(path, token, params=None):
    import time
    try_count = 4
    state = token if isinstance(token, dict) else {
        "access_token": token,
        "force_refresh_used": False,
    }
    for attempt in range(try_count):
        try:
            resp = requests.get(
                f"{BASE}{path}",
                headers={"Authorization": f"Bearer {state['access_token']}"},
                params=params or {},
                timeout=30,
            )
            if resp.status_code == 401 and not state["force_refresh_used"]:
                # A 401 is the only API response that justifies an early
                # rotating refresh. Provider 5xx never triggers OAuth refresh.
                state["access_token"] = get_valid_access_token(
                    CLIENT_ID, CLIENT_SECRET, force_refresh=True,
                )
                state["force_refresh_used"] = True
                continue
            resp.raise_for_status()
            _record_runtime_fact_best_effort(
                "last_successful_whoop_api_call",
            )
            return resp.json()
        except requests.exceptions.RequestException as e:
            response = getattr(e, "response", None)
            if getattr(response, "status_code", None) == 401:
                raise
            if attempt < try_count - 1:
                time.sleep(5)
            else:
                raise


def paginate(path, token, start_iso):
    records = []
    next_token = None
    while True:
        params = {"limit": 25, "start": start_iso}
        if next_token:
            params["nextToken"] = next_token
        data = get_json(path, token, params)
        records.extend(data.get("records", []))
        next_token = data.get("next_token")
        if not next_token:
            break
    return records


def init_db(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS workouts (
            id TEXT PRIMARY KEY, start TEXT, end TEXT, sport_name TEXT,
            strain REAL, avg_hr INTEGER, max_hr INTEGER, kilojoule REAL,
            distance_meter REAL, raw_json TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS recovery (
            cycle_id INTEGER PRIMARY KEY, sleep_id TEXT, created_at TEXT,
            recovery_score INTEGER, resting_hr INTEGER, hrv_rmssd REAL,
            spo2 REAL, skin_temp REAL, raw_json TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sleep (
            id TEXT PRIMARY KEY, start TEXT, end TEXT,
            performance_pct INTEGER, efficiency_pct REAL, respiratory_rate REAL,
            raw_json TEXT
        )"""
    )
    conn.commit()


def upsert_workouts(conn, records):
    for w in records:
        score = w.get("score") or {}
        conn.execute(
            "INSERT OR REPLACE INTO workouts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                w["id"], w["start"], w["end"], w.get("sport_name"),
                score.get("strain"), score.get("average_heart_rate"),
                score.get("max_heart_rate"), score.get("kilojoule"),
                score.get("distance_meter"), json.dumps(w),
            ),
        )


def upsert_recovery(conn, records):
    for r in records:
        score = r.get("score") or {}
        conn.execute(
            "INSERT OR REPLACE INTO recovery VALUES (?,?,?,?,?,?,?,?,?)",
            (
                r["cycle_id"], r.get("sleep_id"), r["created_at"],
                score.get("recovery_score"), score.get("resting_heart_rate"),
                score.get("hrv_rmssd_milli"), score.get("spo2_percentage"),
                score.get("skin_temp_celsius"), json.dumps(r),
            ),
        )


def upsert_sleep(conn, records):
    for s in records:
        score = s.get("score") or {}
        conn.execute(
            "INSERT OR REPLACE INTO sleep VALUES (?,?,?,?,?,?,?)",
            (
                s["id"], s["start"], s["end"],
                score.get("sleep_performance_percentage"),
                score.get("sleep_efficiency_percentage"),
                score.get("respiratory_rate"), json.dumps(s),
            ),
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="За сколько последних дней тянуть данные")
    args = parser.parse_args()

    run_id, pipeline_date = morning_observability.env_context()
    token = _auth_observability(
        CLIENT_ID, CLIENT_SECRET, pipeline_date, run_id
    )
    token_state = {
        "access_token": token,
        "force_refresh_used": False,
    }
    start_iso = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    print(f"Тяну тренировки за последние {args.days} дн...")
    try:
        workouts = paginate("/activity/workout", token_state, start_iso)
    except Exception as exc:
        reason = _provider_error_reason("workout_fetch", exc)
        morning_observability.record_stage(
            pipeline_date, run_id, "recovery_imported", "failed",
            f"blocked_by:{reason}",
        )
        morning_observability.record_stage(
            pipeline_date, run_id, "sleep_imported", "skipped",
            f"blocked_by:{reason}",
        )
        raise
    print(f"  workouts: {len(workouts)}")

    print("Тяну recovery...")
    recovery_timer = morning_observability.StageTimer.start(
        pipeline_date, run_id, "recovery_imported"
    )
    try:
        recovery = paginate("/recovery", token_state, start_iso)
    except Exception as exc:
        reason = _provider_error_reason("recovery_fetch", exc)
        recovery_timer.finish("failed", reason)
        morning_observability.record_stage(
            pipeline_date, run_id, "sleep_imported", "skipped",
            f"blocked_by:{reason}",
        )
        raise
    print(f"  recovery: {len(recovery)}")

    print("Тяну данные о сне...")
    sleep_timer = morning_observability.StageTimer.start(
        pipeline_date, run_id, "sleep_imported"
    )
    try:
        sleep = paginate("/activity/sleep", token_state, start_iso)
    except Exception as exc:
        reason = _provider_error_reason("sleep_fetch", exc)
        recovery_timer.finish(
            "skipped", f"atomic_import_aborted_by:{reason}",
            details={"provider_records": len(recovery)},
        )
        sleep_timer.finish("failed", reason)
        raise
    print(f"  sleep: {len(sleep)}")

    # Fetch all provider collections before opening the write transaction. A
    # later API/OAuth failure therefore cannot leave a partial morning import.
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        init_db(conn)
        conn.execute("BEGIN IMMEDIATE")
        upsert_workouts(conn, workouts)
        upsert_recovery(conn, recovery)
        upsert_sleep(conn, sleep)
        conn.commit()
        _record_runtime_fact_best_effort("last_successful_import")
    except Exception as exc:
        conn.rollback()
        reason = morning_observability.safe_exception_reason(
            exc, prefix="canonical_persistence"
        )
        recovery_timer.finish(
            "failed", reason, details={"provider_records": len(recovery)}
        )
        sleep_timer.finish(
            "failed", reason, details={"provider_records": len(sleep)}
        )
        raise
    finally:
        conn.close()

    status = morning_readiness.morning_data_status(
        pipeline_date, db_path=DB_PATH
    )
    if status["error"]:
        recovery_timer.finish(
            "failed", f"canonical_read:{status['error']}",
            details={"provider_records": len(recovery)},
        )
        sleep_timer.finish(
            "failed", f"canonical_read:{status['error']}",
            details={"provider_records": len(sleep)},
        )
        raise RuntimeError(f"canonical morning read failed: {status['error']}")
    recovery_timer.finish(
        "success" if status["recovery"] else "waiting",
        "current_morning_recovery_present"
        if status["recovery"] else "current_morning_recovery_absent",
        details={"provider_records": len(recovery)},
    )
    sleep_timer.finish(
        "success" if status["sleep"] else "waiting",
        "current_morning_sleep_present"
        if status["sleep"] else "current_morning_sleep_absent",
        details={"provider_records": len(sleep)},
    )
    print(f"\nГотово. Всё сохранено в {DB_PATH}")


if __name__ == "__main__":
    main()
