"""Evidence-led weekly Telegram report for the completed Monday-to-Sunday period."""
import datetime as dt
import statistics as st
import sqlite3

import daily_log
import canonical_read_model as CRM
import phase2_flags
import weekly_analysis_v2


def _as_date(value):
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def _mean(values):
    values = [float(value) for value in values if value is not None]
    return round(st.mean(values), 1) if values else None


def _fmt(value, digits=1):
    if value is None:
        return "—"
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _metric_line(label, current, previous, unit, higher_is_better=True):
    if current is None:
        return f"• {label}: недостаточно данных"
    line = f"• {label}: {_fmt(current)}{unit}"
    if previous is None:
        return line
    delta = round(current - previous, 1)
    if delta == 0:
        return f"{line} (без изменений к прошлой неделе)"
    direction_is_good = delta > 0 if higher_is_better else delta < 0
    arrow = "↑" if delta > 0 else "↓"
    tone = "лучше" if direction_is_good else "хуже"
    return f"{line} ({arrow} {_fmt(abs(delta))}{unit}, {tone} к прошлой неделе)"


def _week_metrics(recovery, sleep, week_start, week_end):
    # Each context date D is evaluated against the WHOOP morning D+1.
    outcome_dates = [week_start + dt.timedelta(days=offset) for offset in range(1, 8)]
    rec_rows = [recovery.get(day.isoformat()) for day in outcome_dates]
    sleep_rows = [sleep.get(day.isoformat()) for day in outcome_dates]
    return {
        "coverage": sum(1 for row in rec_rows if row is not None),
        "recovery": _mean(row["score"] for row in rec_rows if row),
        "hrv": _mean(row["hrv"] for row in rec_rows if row),
        "rhr": _mean(row["rhr"] for row in rec_rows if row),
        "sleep": _mean(row["hours"] for row in sleep_rows if row),
    }


def ensure_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_reports (
            week_ending TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def was_sent(week_ending):
    conn = daily_log.connect()
    try:
        ensure_table(conn)
        row = conn.execute(
            "SELECT 1 FROM weekly_reports WHERE week_ending = ?", (_as_date(week_ending).isoformat(),)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def mark_sent(week_ending):
    conn = daily_log.connect()
    try:
        ensure_table(conn)
        conn.execute(
            "INSERT OR IGNORE INTO weekly_reports (week_ending, sent_at) VALUES (?, ?)",
            (_as_date(week_ending).isoformat(), dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def is_weekly_context(evening_date):
    """The Monday check-in refers to Sunday evening and closes the full week."""
    return _as_date(evening_date).weekday() == 6


def create_report(week_ending):
    """Build a concise report from measured facts; it never invents causal effects."""
    if phase2_flags.weekly_v2_enabled():
        return weekly_analysis_v2.create_report(week_ending)
    week_end = _as_date(week_ending)
    week_start = week_end - dt.timedelta(days=6)
    previous_start = week_start - dt.timedelta(days=7)
    previous_end = week_end - dt.timedelta(days=7)

    conn = sqlite3.connect(daily_log.DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        following_monday = week_end + dt.timedelta(days=1)
        local_now = dt.datetime.combine(
            following_monday, dt.time(12), tzinfo=CRM.TS.ANALYSIS_TZ
        )
        shared = CRM.weekly_snapshot(conn, local_now, factors=())
    finally:
        conn.close()

    def legacy_metrics(week):
        metrics = week["metrics"]
        return {
            "coverage": metrics["recovery_score"]["sample_size"],
            "recovery": metrics["recovery_score"]["mean"],
            "hrv": metrics["hrv_rmssd"]["mean"],
            "rhr": metrics["resting_hr"]["mean"],
            "sleep": metrics["sleep_hours"]["mean"],
        }

    current_week, previous_week = shared["current_week"], shared["previous_week"]
    current, previous = legacy_metrics(current_week), legacy_metrics(previous_week)
    logged_contexts = current_week["logged"]["daily_context_coverage_days"]
    strength_days = current_week["activity"]["strength_days"]
    strength_sets = current_week["activity"]["strength_sets"]
    strength_volume = current_week["activity"]["strength_volume"]
    cardio_sessions = current_week["activity"]["cardio_sessions"]
    cardio_minutes = current_week["activity"]["cardio_minutes"]

    lines = [
        "📅 Еженедельный отчёт Джарвиса",
        f"Период действий: {week_start:%d.%m}–{week_end:%d.%m}; результаты WHOOP — утра {week_start + dt.timedelta(days=1):%d.%m}–{week_end + dt.timedelta(days=1):%d.%m}.",
        "",
        "Состояние и динамика:",
        _metric_line("Recovery", current["recovery"], previous["recovery"], "%"),
        _metric_line("HRV", current["hrv"], previous["hrv"], " мс"),
        _metric_line("Пульс покоя", current["rhr"], previous["rhr"], " уд/мин", higher_is_better=False),
        _metric_line("Сон", current["sleep"], previous["sleep"], " ч"),
        "",
        f"Данные: WHOOP-утра {current['coverage']}/7; daily context {logged_contexts}/7.",
        f"Нагрузка: силовые — {strength_days} дн., {strength_sets} подходов, объём {_fmt(strength_volume, 0)} кг; кардио — {cardio_sessions} сесс., {_fmt(cardio_minutes, 0)} мин.",
        "",
    ]

    if current["coverage"] < 5 or logged_contexts < 5:
        lines.append(
            "Причинных выводов не делаю: для оценки возможных связей нужно больше полных пар «день D → локальное утро D+1»."
        )
        lines.append("Фокус недели: заполняй контекст после каждого утреннего вопроса.")
    else:
        lines.append(
            "Наблюдение: данные уже пригодны для поиска совпадений по времени, но 10–15 сопоставимых записей всё ещё недостаточно для причинного вывода."
        )
        lines.append(
            "Эксперимент недели: выбери один стабильный фактор сна на 7 дней (например, время последнего кофеина) и записывай его одинаково подробно."
        )
    return "\n".join(lines)
