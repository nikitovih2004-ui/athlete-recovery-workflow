"""Weekly Analysis v2 rendered from one shared deterministic evidence snapshot."""
from __future__ import annotations

import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

import conversation_evidence
import daily_log
import grounded_responder

KYIV = ZoneInfo("Europe/Kyiv")
DEFAULT_FACTORS = (
    ("supplement", "creatine"),
    ("supplement", "magnesium"),
    ("daily_factor", "alcohol"),
    ("daily_factor", "late_caffeine"),
    ("daily_factor", "late_meal"),
    ("daily_factor", "high_stress"),
)


def _fmt(value):
    if value is None:
        return "—"
    return f"{value:.1f}".rstrip("0").rstrip(".")


def build_snapshot(conn, week_ending):
    week_end = (
        week_ending if isinstance(week_ending, dt.date)
        else dt.date.fromisoformat(str(week_ending))
    )
    if week_end.weekday() != 6:
        raise ValueError("weekly analysis must end on Sunday")
    following_monday = week_end + dt.timedelta(days=1)
    local_now = dt.datetime.combine(following_monday, dt.time(12), tzinfo=KYIV)
    return conversation_evidence.weekly_evidence(
        conn, local_now, factors=DEFAULT_FACTORS
    )


def render(snapshot):
    current = snapshot["current_week"]
    previous = snapshot["previous_week"]
    deltas = snapshot["mean_delta_current_minus_previous"]
    metrics = current["metrics"]
    activity = current["activity"]
    lines = [
        "📅 Weekly Analysis v2",
        f"Действия: {current['action_week_start']}–{current['action_week_end']}; "
        f"WHOOP outcomes: {current['outcome_start']}–{current['outcome_end']}.",
        "",
        "Что изменилось к предыдущей неделе:",
        f"• Recovery {_fmt(metrics['recovery_score']['mean'])}% "
        f"(Δ {_fmt(deltas['recovery_score'])}, n={metrics['recovery_score']['sample_size']})",
        f"• HRV {_fmt(metrics['hrv_rmssd']['mean'])} мс "
        f"(Δ {_fmt(deltas['hrv_rmssd'])}, n={metrics['hrv_rmssd']['sample_size']})",
        f"• Пульс покоя {_fmt(metrics['resting_hr']['mean'])} "
        f"(Δ {_fmt(deltas['resting_hr'])}, n={metrics['resting_hr']['sample_size']})",
        f"• Сон {_fmt(metrics['sleep_hours']['mean'])} ч "
        f"(Δ {_fmt(deltas['sleep_hours'])}, n={metrics['sleep_hours']['sample_size']})",
        "",
        f"Нагрузка: силовые {activity['strength_days']} дн. / "
        f"{activity['strength_sets']} подходов / объём {_fmt(activity['strength_volume'])} кг; "
        f"кардио {activity['cardio_sessions']} сесс. / {_fmt(activity['cardio_minutes'])} мин.",
    ]
    observations = snapshot.get("factor_observations", [])
    if observations:
        lines.extend(["", "Наблюдение:", grounded_responder.factor_observation(observations[0])])
        experiment = "Продолжай явно отмечать этот фактор ещё неделю, не меняя несколько условий одновременно."
    else:
        lines.extend([
            "",
            "Факторы: пока нет групп минимум 5+5 с outcomes следующего утра; причинных выводов не делаю.",
        ])
        experiment = "Эксперимент недели: выбери один фактор и явно отмечай и наличие, и отсутствие."
    lines.extend(["", experiment])
    return "\n".join(lines)


def create_report(week_ending):
    conn = sqlite3.connect(daily_log.DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        return render(build_snapshot(conn, week_ending))
    finally:
        conn.close()
