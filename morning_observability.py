"""Durable, privacy-safe observability for the production morning pipeline."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import daily_log


TZ = ZoneInfo("Europe/Kyiv")
RUN_ID_ENV = "MORNING_PIPELINE_RUN_ID"
PIPELINE_DATE_ENV = "MORNING_PIPELINE_DATE"

STAGES = (
    "cron_started",
    "whoop_refresh_attempted",
    "whoop_refresh_result",
    "recovery_imported",
    "sleep_imported",
    "dashboard_rebuilt",
    "prompt_candidate_created",
    "prompt_delivered",
    "analysis_generated",
)
OUTCOMES = ("success", "failed", "waiting", "skipped")
_STAGE_SET = frozenset(STAGES)
_OUTCOME_SET = frozenset(OUTCOMES)
_SAFE_REASON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:=/-]{0,239}$")
_LABELS = {
    "cron_started": "cron",
    "whoop_refresh_attempted": "refresh attempted",
    "whoop_refresh_result": "refresh result",
    "recovery_imported": "Recovery imported",
    "sleep_imported": "Sleep imported",
    "dashboard_rebuilt": "Dashboard rebuilt",
    "prompt_candidate_created": "prompt candidate",
    "prompt_delivered": "prompt delivered",
    "analysis_generated": "analysis generated",
}
_SYMBOLS = {
    "success": "✓",
    "failed": "✗",
    "waiting": "○",
    "skipped": "—",
}


def _as_utc(value=None):
    result = value or dt.datetime.now(dt.timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=dt.timezone.utc)
    return result.astimezone(dt.timezone.utc)


def _as_date(value=None):
    if value is None:
        return dt.datetime.now(TZ).date()
    if isinstance(value, dt.datetime):
        return value.astimezone(TZ).date() if value.tzinfo else value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _safe_reason(value):
    text = " ".join(str(value or "").replace("\x00", "").split())
    if not text:
        raise ValueError("morning pipeline reason must be explicit")
    if not _SAFE_REASON.fullmatch(text):
        raise ValueError(
            "morning pipeline reason must be a privacy-safe category"
        )
    return text


def safe_exception_reason(exc, *, prefix=None):
    """Return an exact safe category without persisting exception text or secrets."""
    category = getattr(exc, "category", None) or type(exc).__name__
    status = getattr(exc, "status_code", None)
    parts = [str(prefix)] if prefix else []
    parts.append(str(category))
    if status is not None:
        parts.append(f"http_status={int(status)}")
    return ":".join(parts)


def new_run_id(kind="cron"):
    return f"{kind}:{uuid.uuid4().hex}"


def env_context():
    run_id = os.environ.get(RUN_ID_ENV, "").strip()
    pipeline_date = os.environ.get(PIPELINE_DATE_ENV, "").strip()
    return (
        run_id or new_run_id("standalone"),
        _as_date(pipeline_date) if pipeline_date else _as_date(),
    )


def _connect():
    conn = daily_log.connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS morning_pipeline_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            pipeline_date TEXT NOT NULL,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            details_json TEXT,
            CHECK (stage IN (
                'cron_started','whoop_refresh_attempted','whoop_refresh_result',
                'recovery_imported','sleep_imported','dashboard_rebuilt',
                'prompt_candidate_created','prompt_delivered','analysis_generated'
            )),
            CHECK (outcome IN ('success','failed','waiting','skipped')),
            CHECK (duration_ms >= 0)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_morning_pipeline_date_stage "
        "ON morning_pipeline_events(pipeline_date, stage, event_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_morning_pipeline_run "
        "ON morning_pipeline_events(run_id, event_id)"
    )
    conn.commit()
    return conn


def record_stage(
    pipeline_date,
    run_id,
    stage,
    outcome,
    reason,
    *,
    started_at=None,
    finished_at=None,
    duration_ms=0,
    details=None,
):
    if stage not in _STAGE_SET:
        raise ValueError(f"unknown morning pipeline stage: {stage}")
    if outcome not in _OUTCOME_SET:
        raise ValueError(f"unknown morning pipeline outcome: {outcome}")
    start = _as_utc(started_at)
    finish = _as_utc(finished_at or start)
    duration = max(0, int(duration_ms))
    detail_text = None
    if details is not None:
        detail_text = json.dumps(details, ensure_ascii=False, sort_keys=True)
        if len(detail_text.encode("utf-8")) > 4096:
            raise ValueError("morning pipeline details exceed 4096 bytes")
    conn = _connect()
    try:
        cursor = conn.execute(
            """INSERT INTO morning_pipeline_events
               (pipeline_date, run_id, stage, started_at, finished_at, outcome,
                reason, duration_ms, details_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _as_date(pipeline_date).isoformat(),
                str(run_id)[:128],
                stage,
                start.isoformat(timespec="milliseconds"),
                finish.isoformat(timespec="milliseconds"),
                outcome,
                _safe_reason(reason),
                duration,
                detail_text,
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


@dataclass
class StageTimer:
    pipeline_date: dt.date
    run_id: str
    stage: str
    started_at: dt.datetime
    started_monotonic: float

    @classmethod
    def start(cls, pipeline_date, run_id, stage):
        if stage not in _STAGE_SET:
            raise ValueError(f"unknown morning pipeline stage: {stage}")
        return cls(
            _as_date(pipeline_date),
            str(run_id),
            stage,
            _as_utc(),
            time.monotonic(),
        )

    def finish(self, outcome, reason, *, details=None):
        finished = _as_utc()
        return record_stage(
            self.pipeline_date,
            self.run_id,
            self.stage,
            outcome,
            reason,
            started_at=self.started_at,
            finished_at=finished,
            duration_ms=round((time.monotonic() - self.started_monotonic) * 1000),
            details=details,
        )


def events_for_period(start_date, end_date):
    start = _as_date(start_date).isoformat()
    end = _as_date(end_date).isoformat()
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT * FROM morning_pipeline_events
               WHERE pipeline_date BETWEEN ? AND ?
               ORDER BY pipeline_date, event_id""",
            (start, end),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json") or "null")
            except (TypeError, ValueError):
                item["details"] = None
                item.pop("details_json", None)
            result.append(item)
        return result
    finally:
        conn.close()


def events_for_run(run_id):
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT * FROM morning_pipeline_events
               WHERE run_id = ? ORDER BY event_id""",
            (str(run_id),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def stage_was_recorded(run_id, stage):
    if stage not in _STAGE_SET:
        raise ValueError(f"unknown morning pipeline stage: {stage}")
    conn = _connect()
    try:
        return conn.execute(
            """SELECT 1 FROM morning_pipeline_events
               WHERE run_id = ? AND stage = ? LIMIT 1""",
            (str(run_id), stage),
        ).fetchone() is not None
    finally:
        conn.close()


def latest_failure(run_id):
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT * FROM morning_pipeline_events
               WHERE run_id = ? AND outcome = 'failed'
               ORDER BY event_id DESC LIMIT 1""",
            (str(run_id),),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def timeline(days=7, end_date=None):
    if not 1 <= int(days) <= 90:
        raise ValueError("days must be between 1 and 90")
    end = _as_date(end_date)
    start = end - dt.timedelta(days=int(days) - 1)
    events = events_for_period(start, end)
    by_date = {}
    for offset in range(int(days)):
        day = start + dt.timedelta(days=offset)
        by_date[day.isoformat()] = {stage: [] for stage in STAGES}
    for event in events:
        if event["pipeline_date"] in by_date:
            by_date[event["pipeline_date"]][event["stage"]].append(event)
    return by_date


def _display_event(events):
    if not events:
        return None, []
    successes = [event for event in events if event["outcome"] == "success"]
    chosen = successes[-1] if successes else events[-1]
    failures = [event for event in events if event["outcome"] == "failed"]
    return chosen, failures


def render_timeline(days=7, end_date=None):
    lines = []
    for date_iso, stages in timeline(days=days, end_date=end_date).items():
        lines.append(date_iso)
        day_failures = []
        for stage in STAGES:
            chosen, failures = _display_event(stages[stage])
            label = _LABELS[stage]
            if chosen is None:
                lines.append(f"— {label} | not_observed | duration=0ms")
                continue
            local_time = dt.datetime.fromisoformat(chosen["finished_at"]).astimezone(TZ)
            lines.append(
                f"{_SYMBOLS[chosen['outcome']]} {label} | "
                f"{local_time:%H:%M:%S} | {chosen['outcome']} | "
                f"{chosen['reason']} | duration={chosen['duration_ms']}ms | "
                f"attempts={len(stages[stage])}"
            )
            if failures:
                latest = failures[-1]
                failed_at = dt.datetime.fromisoformat(latest["finished_at"]).astimezone(TZ)
                lines.append(
                    f"  ! latest failure {failed_at:%H:%M:%S}: "
                    f"{latest['reason']} ({latest['duration_ms']}ms)"
                )
                day_failures.append(latest)
        if day_failures:
            last = day_failures[-1]
            lines.append(
                f"PIPELINE FAILURE RECORDED: {last['stage']} — {last['reason']}"
            )
        lines.append("")
    import oauth_observability
    lines.extend(("", "WHOOP OAuth state", oauth_observability.render()))
    return "\n".join(lines).rstrip()


def main():
    parser = argparse.ArgumentParser(
        description="Print the complete production morning timeline."
    )
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--end-date")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.json:
        print(json.dumps(
            timeline(args.days, args.end_date), ensure_ascii=False, indent=2
        ))
    else:
        print(render_timeline(args.days, args.end_date))


if __name__ == "__main__":
    main()
