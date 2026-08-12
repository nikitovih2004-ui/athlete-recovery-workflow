"""Deterministic, evidence-only Telegram responses for Phase 2 reads."""
from __future__ import annotations

import strength_presentation


METRIC_LABELS = {
    "recovery_score": "Recovery",
    "hrv_rmssd": "HRV",
    "resting_hr": "пульс покоя",
    "sleep_hours": "сон",
    "sleep_performance": "Sleep Performance",
}

GENERAL_BOUNDARY = (
    "Я могу помочь записать факт или разобрать твои WHOOP-данные через "
    "безопасный вопрос о статусе, тренде, неделе или факторе."
)


def safe_general(reply, *, allow_bounded_agent=False):
    """Surface bounded-agent small talk, never ungrounded health/security prose."""
    if not allow_bounded_agent or not isinstance(reply, str):
        return GENERAL_BOUNDARY
    clean = " ".join(reply.strip().split())
    if not clean or len(clean) > 600:
        return GENERAL_BOUNDARY
    lowered = clean.casefold()
    blocked = (
        "system prompt", "системн" + "ый промпт", "api key", "токен",
        "парол", "sql", "диагноз", "назначаю", "принимай доз",
        "гарантированно улучш", "причина твоего",
        "hrv", "recovery", "восстанов", "пульс", "сон", "здоров",
        "магний", "добавк", "бад", "калори", "нагрузк", "трениров",
    )
    if any(marker in lowered for marker in blocked):
        return GENERAL_BOUNDARY
    if any(ord(char) < 32 and char not in "\t\n\r" for char in clean):
        return GENERAL_BOUNDARY
    return clean


def _fmt(value):
    if value is None:
        return "—"
    return f"{value:.2f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def metric_trend(snapshot):
    label = METRIC_LABELS.get(snapshot.get("metric"), snapshot.get("metric"))
    coverage = snapshot.get("coverage", {})
    summary = snapshot.get("summary", {})
    observed = coverage.get("observed_days", 0)
    expected = coverage.get("expected_days", snapshot.get("window_days"))
    series = [p.get("value") for p in snapshot.get("series", []) if p.get("value") is not None]
    direction = "недостаточно точек для направления"
    if len(series) >= 2:
        delta = series[-1] - series[0]
        direction = "растёт" if delta > 0 else "снижается" if delta < 0 else "без изменения"
    return (
        f"📈 {label}: {snapshot.get('start_date')}–{snapshot.get('end_date')}.\n"
        f"Среднее {_fmt(summary.get('mean'))}, диапазон "
        f"{_fmt(summary.get('minimum'))}–{_fmt(summary.get('maximum'))}.\n"
        f"Направление: {direction}. Данные: {observed}/{expected} дней. Это измеренный тренд, не медицинский вывод."
    )


def factor_observation(snapshot):
    states = snapshot.get("day_states", {})
    present = snapshot.get("cohorts", {}).get("present", {})
    absent = snapshot.get("cohorts", {}).get("absent", {})
    present_n = present.get("outcomes", {}).get("recovery_score", {}).get("sample_size", 0)
    absent_n = absent.get("outcomes", {}).get("recovery_score", {}).get("sample_size", 0)
    factor = snapshot.get("factor_key")
    period = f"{snapshot.get('start_date')}–{snapshot.get('end_date')}"
    if not snapshot.get("eligible"):
        return (
            f"🔎 {factor}, период {period}: пока недостаточно сопоставимых данных.\n"
            f"Есть outcomes: {present_n} при факторе и {absent_n} при явно отмеченном отсутствии "
            f"(нужно минимум {snapshot.get('minimum_cohort')} + {snapshot.get('minimum_cohort')}). "
            f"Не записано: {states.get('missing', 0)} дн.; unknown/conflict: "
            f"{states.get('unknown', 0) + states.get('conflict', 0)}. Выводов не делаю."
        )
    p = present["outcomes"]["recovery_score"].get("mean")
    a = absent["outcomes"]["recovery_score"].get("mean")
    delta = snapshot.get("mean_delta_present_minus_absent", {}).get("recovery_score")
    return (
        f"🔎 {factor}, период {period}: Recovery в среднем {_fmt(p)} при факторе "
        f"(n={present_n}) и {_fmt(a)} при явно отмеченном отсутствии (n={absent_n}); "
        f"разница {_fmt(delta)} п.п. Это наблюдаемое совпадение, не доказательство причинности."
    )


def render(intent, result):
    snapshot = (result or {}).get("data", {})
    if intent == "get_metric_trend":
        return metric_trend(snapshot)
    if intent == "get_factor_observation":
        return factor_observation(snapshot)
    if intent == "get_week_summary" and snapshot.get("snapshot") == "weekly_evidence.v2":
        current = snapshot["current_week"]
        metrics = current["metrics"]
        delta = snapshot["mean_delta_current_minus_previous"]
        base = (
            f"🗓 {current['action_week_start']}–{current['action_week_end']}: "
            f"Recovery {_fmt(metrics['recovery_score']['mean'])}% "
            f"(Δ {_fmt(delta['recovery_score'])}, n={metrics['recovery_score']['sample_size']}), "
            f"HRV {_fmt(metrics['hrv_rmssd']['mean'])} "
            f"(Δ {_fmt(delta['hrv_rmssd'])}). "
            f"Это сравнение измеренных периодов, не причинный вывод."
        )
        grouped, lines = strength_presentation.render_lines(
            current.get("strength_rows") or []
        )
        if not lines:
            return base
        return (
            base + f"\n\n🏋️ Силовая: {grouped['exercise_count']} упражнений / "
            f"{grouped['set_count']} подходов.\n" + "\n".join(lines)
        )
    return None
