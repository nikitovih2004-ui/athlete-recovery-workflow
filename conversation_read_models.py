"""Pure, bounded, side-effect-free read snapshots for the two read tools.

These never create tables or write anything. They read a fixed, small window and
return plain JSON-safe dicts. No causal/medical prose — just numbers the
responder can describe. Timezone handling is Kyiv-aware, matching daily_summary.

    today_status.v1  : target day recovery + a bounded 28-day baseline + today's
                       logged-activity counts.
    week_summary.v1  : last completed Mon–Sun week vs the previous week, with
                       coverage + activity counts.
"""
from __future__ import annotations

import datetime as dt
import json

import canonical_read_model as CRM
import time_semantics as TS
import strength_presentation

BASELINE_DAYS = CRM.BASELINE_VALID_DAYS


def today_status(conn, local_now):
    """Snapshot for get_today_status."""
    target = local_now.date()
    target_iso = target.isoformat()
    window_start = target - dt.timedelta(days=365)
    with CRM.snapshot_transaction(conn) as model:
        outcomes = model.outcomes(window_start, target)
        metric_context = {
            item["metric"]: item
            for item in CRM.daily_metric_context(outcomes, target)
        }
        today = outcomes.get(target, {})
        activity = model.activity_snapshot(target, target)
        supplements = model.supplement_snapshot(target, target)
        factor_completeness = model.factor_completeness(target, target)
    today_rec = None
    if today.get("recovery_score") is not None:
        today_rec = {
            "score": today.get("recovery_score"),
            "hrv": today.get("hrv_rmssd"),
            "rhr": today.get("resting_hr"),
        }
    recovery_baseline = metric_context["recovery"]
    return {
        "snapshot": "today_status.v1", "contract_version": CRM.CONTRACT_VERSION,
        "date": target_iso, "recovery": today_rec,
        "baseline_28d": {
            "recovery_score": recovery_baseline["baseline_value"],
            "hrv": metric_context["hrv"]["baseline_value"],
            "rhr": metric_context["rhr"]["baseline_value"],
            "sample_size": recovery_baseline["valid_observations"],
            "period_start": recovery_baseline["period_start"],
            "period_end": recovery_baseline["period_end"],
            "method": recovery_baseline["baseline_method"],
            "metric_contract": metric_context,
        },
        "logged_today": {
            "strength_sets": activity["summary"]["manual_strength_sets"],
            "cardio_sessions": activity["summary"]["manual_cardio_sessions"],
            "supplements": supplements["summary"]["taken_count"],
            "supplements_not_taken": supplements["summary"]["not_taken_count"],
        },
        "activity_facets": activity["summary"],
        "provenance": {"read_model": CRM.CONTRACT_VERSION},
        "completeness": {"factor_extraction": factor_completeness},
    }


def week_summary(conn, local_now):
    """Snapshot for get_week_summary (last completed week vs previous)."""
    canonical = CRM.weekly_snapshot(conn, local_now, factors=())

    def adapt(week):
        metrics, activity, logged = week["metrics"], week["activity"], week["logged"]
        return {
            "week_start": week["action_week_start"], "week_end": week["action_week_end"],
            "recovery_avg": metrics["recovery_score"]["mean"],
            "hrv_avg": metrics["hrv_rmssd"]["mean"],
            "rhr_avg": metrics["resting_hr"]["mean"],
            "recovery_sample_size": metrics["recovery_score"]["sample_size"],
            "workout_days": activity["strength_days"],
            "cardio_days": activity["cardio_sessions"],
            "supplement_days": logged["supplement_days"],
            "daily_context_coverage_days": logged["daily_context_coverage_days"],
            "strength_sets": activity["strength_sets"],
            "strength_rows": strength_presentation.safe_rows(
                week.get("strength_rows") or []
            ),
            "provenance": week["provenance"],
            "completeness": week["completeness"],
        }
    return {
        "snapshot": "week_summary.v1", "contract_version": CRM.CONTRACT_VERSION,
        "period": "last_completed_week",
        "current_week": adapt(canonical["current_week"]),
        "previous_week": adapt(canonical["previous_week"]),
        "completeness": canonical["completeness"],
    }


def day_snapshot(conn, date_iso):
    """One action-day canonical snapshot; explicit, read-only and bounded."""
    snap = CRM.range_snapshot(conn, date_iso, date_iso)
    day = snap["days"][0]
    activities = day.get("activities") or {}
    # Never copy context notes, source keys, or event metadata into the
    # conversation audit result. The responder needs only these aggregates.
    safe_cardio = []
    for row in activities.get("manual_cardio") or []:
        safe_cardio.append({
            "id": row.get("id"), "date": row.get("date"),
            "activity_type": row.get("type"),
            "duration_minutes": row.get("duration"),
            "strain": row.get("strain"), "steps": row.get("steps"),
            "calories_kcal": row.get("calories"),
            "avg_hr_bpm": row.get("avg_hr"),
            "zones": {
                str(index): row.get(f"hr_zone_{index}_duration")
                for index in range(6)
            },
        })
    safe_strength = strength_presentation.safe_rows(
        activities.get("manual_strength") or []
    )
    safe_day = {
        "next_morning": {key: (day.get("next_morning") or {}).get(key)
                           for key in ("recovery_score", "hrv_rmssd", "resting_hr")},
        "activities": {
            "manual_strength": safe_strength,
            "manual_cardio": safe_cardio,
        },
        "supplements": [None] * len(day.get("supplements") or []),
        "has_daily_context": bool(day.get("context")),
    }
    return {"snapshot": "day_snapshot.v1", "date": date_iso, "day": safe_day,
            "provenance": {"read_model": CRM.CONTRACT_VERSION}, "completeness": snap["completeness"]}


def data_coverage(conn, local_now):
    """Inventory only public canonical facets; never raw notes or messages."""
    end = TS.as_analysis_datetime(local_now).date()
    start = end - dt.timedelta(days=89)
    snap = CRM.range_snapshot(conn, start, end)
    days = snap["days"]
    outcome_days = [d["outcome_date"] for d in days if d.get("next_morning", {}).get("recovery_score") is not None]
    action_days = [d["action_date"] for d in days if any((d.get("activities") or {}).values()) or d.get("supplements") or d.get("context")]
    return {"snapshot": "data_coverage.v1", "window_start": start.isoformat(), "window_end": end.isoformat(),
            "recovery_days": len(outcome_days), "action_days": len(action_days),
            "recovery_first": min(outcome_days) if outcome_days else None, "recovery_last": max(outcome_days) if outcome_days else None,
            "activity": snap["activity"]["summary"], "supplements": snap["supplements"]["summary"],
            "context_days": sum(bool(d.get("context")) for d in days)}


def supplement_records(conn, local_now):
    end = TS.as_analysis_datetime(local_now).date()
    start = end - dt.timedelta(days=89)
    with CRM.snapshot_transaction(conn) as model:
        data = model.supplement_snapshot(start, end)
    events = [{k: row.get(k) for k in ("analysis_date", "name", "canonical_name", "dose_text", "taken_state")}
              for row in data.get("events", [])[-20:]]
    return {"snapshot": "supplement_records.v1", "start_date": start.isoformat(), "end_date": end.isoformat(),
            "events": events, "summary": data.get("summary", {}), "provenance": data.get("provenance", {})}
