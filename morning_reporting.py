"""Shared helpers for producing and delivering durable morning reports."""
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import canonical_read_model as CRM

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
DB_PATH = DATA_DIR / "whoop.db"
TELEGRAM_MESSAGE_LIMIT = 3900


def _first(mapping, *names):
    for name in names:
        value = (mapping or {}).get(name)
        if value is not None:
            return value
    return None


def _number(value, digits=1):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    rounded = round(number, digits)
    return str(int(rounded)) if rounded.is_integer() else str(rounded)


def _hours_from_ms(value):
    try:
        return round(float(value) / 3_600_000, 2)
    except (TypeError, ValueError):
        return None


def _clock(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).strftime("%H:%M")
    except ValueError:
        return None


def load_morning_whoop_metrics(recovery_date, db_path=None):
    """Return a compact, identifier-free view of the WHOOP data used that morning."""
    recovery_day = dt.date.fromisoformat(str(recovery_date))
    action_day = recovery_day - dt.timedelta(days=1)
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        with CRM.snapshot_transaction(conn) as model:
            outcome = model.outcomes(recovery_day, recovery_day).get(
                recovery_day, {}
            )
            facets = model.dashboard_whoop_facets(recovery_day, recovery_day)
            sessions = model.whoop_sessions(action_day, action_day)
    finally:
        conn.close()

    recovery_score = outcome.get("recovery_provider_score") or {}
    sleep_score = outcome.get("sleep_provider_score") or {}
    sleep_stages = sleep_score.get("stage_summary") or {}
    sleep_needed = sleep_score.get("sleep_needed") or {}
    sleep_facet = (
        (facets.get("sleep") or [{}])[-1] if facets.get("sleep") else {}
    )

    workouts = []
    zone_names = (
        "zone_zero_milli", "zone_one_milli", "zone_two_milli",
        "zone_three_milli", "zone_four_milli", "zone_five_milli",
    )
    for session in sessions:
        try:
            provider = (
                json.loads(session.get("raw_json") or "{}").get("score") or {}
            )
        except (TypeError, ValueError):
            provider = {}
        zones = (
            provider.get("zone_duration")
            or provider.get("zone_durations")
            or {}
        )
        workouts.append({
            "sport": session.get("sport_name"),
            "start": _clock(session.get("start")),
            "duration_min": session.get("duration_minutes"),
            "strain": _first(session, "strain") or provider.get("strain"),
            "avg_hr_bpm": (
                _first(session, "avg_hr")
                or provider.get("average_heart_rate")
            ),
            "max_hr_bpm": (
                _first(session, "max_hr") or provider.get("max_heart_rate")
            ),
            "energy_kj": (
                _first(session, "kilojoule") or provider.get("kilojoule")
            ),
            "distance_m": (
                _first(session, "distance_meter")
                or provider.get("distance_meter")
            ),
            "altitude_gain_m": provider.get("altitude_gain_meter"),
            "altitude_change_m": provider.get("altitude_change_meter"),
            "percent_recorded": provider.get("percent_recorded"),
            "zones_ms": {
                str(index): _first(
                    zones, f"zone_{index}_milli", zone_names[index]
                )
                for index in range(6)
            },
        })

    return {
        "recovery_date": recovery_day.isoformat(),
        "action_date": action_day.isoformat(),
        "workouts": workouts,
        "recovery": {
            "score_pct": outcome.get("recovery_score"),
            "hrv_ms": outcome.get("hrv_rmssd"),
            "resting_hr_bpm": outcome.get("resting_hr"),
            "spo2_pct": outcome.get("spo2"),
            "skin_temp_c": outcome.get("skin_temp"),
            "user_calibrating": recovery_score.get("user_calibrating"),
        },
        "sleep": {
            "bed": _clock(sleep_facet.get("bed_local")),
            "wake": _clock(sleep_facet.get("wake_local")),
            "duration_h": outcome.get("sleep_hours"),
            "performance_pct": outcome.get("sleep_performance"),
            "efficiency_pct": outcome.get("sleep_efficiency"),
            "consistency_pct": sleep_score.get(
                "sleep_consistency_percentage"
            ),
            "respiratory_rate": outcome.get("respiratory_rate"),
            "disturbances": outcome.get("sleep_disturbances"),
            "cycles": sleep_stages.get("sleep_cycle_count"),
            "stages_h": {
                "awake": _hours_from_ms(
                    sleep_stages.get("total_awake_time_milli")
                ),
                "light": _hours_from_ms(
                    sleep_stages.get("total_light_sleep_time_milli")
                ),
                "rem": _hours_from_ms(
                    sleep_stages.get("total_rem_sleep_time_milli")
                ),
                "deep": _hours_from_ms(
                    sleep_stages.get("total_slow_wave_sleep_time_milli")
                ),
                "no_data": _hours_from_ms(
                    sleep_stages.get("total_no_data_time_milli")
                ),
            },
            "need_h": {
                "baseline": _hours_from_ms(
                    sleep_needed.get("baseline_milli")
                ),
                "sleep_debt": _hours_from_ms(
                    sleep_needed.get("need_from_sleep_debt_milli")
                ),
                "recent_strain": _hours_from_ms(
                    sleep_needed.get("need_from_recent_strain_milli")
                ),
                "recent_nap": _hours_from_ms(
                    sleep_needed.get("need_from_recent_nap_milli")
                ),
            },
        },
    }


def _items(mapping, labels, *, digits=1):
    result = []
    for key, label, suffix in labels:
        rendered = _number((mapping or {}).get(key), digits=digits)
        if rendered is not None:
            result.append(f"{label} {rendered}{suffix}")
    return result


def format_morning_whoop_metrics(metrics):
    """Render every available normalized category, without IDs or raw JSON."""
    lines = [f"📊 WHOOP — данные на {metrics['recovery_date']}"]
    workouts = metrics.get("workouts") or []
    lines.append(f"\n🏋️ Тренировки за {metrics['action_date']}")
    if not workouts:
        lines.append("• Нет записанных тренировок")
    for index, workout in enumerate(workouts, 1):
        title = workout.get("sport") or "Тренировка"
        if workout.get("start"):
            title += f" · {workout['start']}"
        details = _items(workout, [
            ("duration_min", "длительность", " мин"),
            ("strain", "strain", ""),
            ("avg_hr_bpm", "ср. пульс", " bpm"),
            ("max_hr_bpm", "макс.", " bpm"),
            ("energy_kj", "энергия", " кДж"),
            ("distance_m", "дистанция", " м"),
            ("altitude_gain_m", "набор", " м"),
            ("altitude_change_m", "перепад", " м"),
            ("percent_recorded", "записано", "%"),
        ])
        lines.append(
            f"• {index}. {title}"
            + (f": {', '.join(details)}" if details else "")
        )
        zone_items = []
        for zone, milliseconds in (workout.get("zones_ms") or {}).items():
            hours = _hours_from_ms(milliseconds)
            if hours is not None:
                zone_items.append(f"Z{zone} {round(hours * 60, 1)} мин")
        if zone_items:
            lines.append("  Зоны: " + ", ".join(zone_items))

    recovery = metrics.get("recovery") or {}
    lines.append("\n💚 Восстановление")
    recovery_items = _items(recovery, [
        ("score_pct", "Recovery", "%"),
        ("hrv_ms", "HRV", " мс"),
        ("resting_hr_bpm", "пульс покоя", " bpm"),
        ("spo2_pct", "SpO₂", "%"),
        ("skin_temp_c", "температура кожи", " °C"),
    ])
    if recovery.get("user_calibrating") is not None:
        recovery_items.append(
            "калибровка активна"
            if recovery["user_calibrating"]
            else "калибровка завершена"
        )
    lines.append(
        "• " + (
            ", ".join(recovery_items)
            if recovery_items else "Нет доступных значений"
        )
    )

    sleep = metrics.get("sleep") or {}
    lines.append("\n😴 Сон")
    timing = []
    if sleep.get("bed"):
        timing.append(f"отбой {sleep['bed']}")
    if sleep.get("wake"):
        timing.append(f"подъём {sleep['wake']}")
    sleep_items = timing + _items(sleep, [
        ("duration_h", "сон", " ч"),
        ("performance_pct", "performance", "%"),
        ("efficiency_pct", "эффективность", "%"),
        ("consistency_pct", "стабильность", "%"),
        ("respiratory_rate", "дыхание", "/мин"),
        ("disturbances", "пробуждения", ""),
        ("cycles", "циклы", ""),
    ])
    lines.append(
        "• " + (
            ", ".join(sleep_items)
            if sleep_items else "Нет доступных значений"
        )
    )
    stage_items = _items(sleep.get("stages_h"), [
        ("awake", "бодрствование", " ч"),
        ("light", "лёгкий", " ч"),
        ("rem", "REM", " ч"),
        ("deep", "глубокий", " ч"),
        ("no_data", "без данных", " ч"),
    ], digits=2)
    if stage_items:
        lines.append("• Стадии: " + ", ".join(stage_items))
    need_items = _items(sleep.get("need_h"), [
        ("baseline", "база", " ч"),
        ("sleep_debt", "долг сна", " ч"),
        ("recent_strain", "нагрузка", " ч"),
        ("recent_nap", "дневной сон", " ч"),
    ], digits=2)
    if need_items:
        lines.append("• Потребность во сне: " + ", ".join(need_items))
    return "\n".join(lines).strip()


def compose_morning_result(
    recovery_date, analysis, db_path=None, limit=TELEGRAM_MESSAGE_LIMIT
):
    """Build the one and only final morning Telegram payload."""
    metrics_text = format_morning_whoop_metrics(
        load_morning_whoop_metrics(recovery_date, db_path=db_path)
    )
    heading = "\n\n🧠 Персональный разбор\n"
    available = limit - len(metrics_text) - len(heading)
    if available < 120:
        raise RuntimeError("whoop_metrics_exceed_single_message_budget")
    analysis = (analysis or "").strip()
    if len(analysis) > available:
        suffix = "\n\n…Разбор сокращён до лимита Telegram."
        cut = max(1, available - len(suffix))
        boundary = max(
            analysis.rfind("\n", 0, cut),
            analysis.rfind(" ", 0, cut),
        )
        if boundary >= cut // 2:
            cut = boundary
        analysis = analysis[:cut].rstrip() + suffix
    payload = metrics_text + heading + analysis
    if len(payload) > limit:
        raise RuntimeError("morning_result_exceeds_single_message_budget")
    return payload




def daily_insights_path(context_date):
    return DATA_DIR / f"daily_insights_{context_date}.md"


def generate_daily_analysis(context_date, timeout=120, context_only=False):
    """Generate one D -> local D+1 analysis from structured facts plus narrative context."""
    output = daily_insights_path(context_date)
    output.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [
            sys.executable,
            str(HERE / "generate_insights.py"),
            "--date",
            str(context_date),
            "--output",
            str(output),
            *(["--context-only"] if context_only else []),
        ],
        cwd=HERE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"analysis_generation_failed:{result.returncode}")
    if not output.exists():
        raise RuntimeError("analysis_output_missing")
    text = output.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise RuntimeError("ежедневный анализ пуст")
    return text


def split_telegram_text(text, limit=3900):
    """Split on a paragraph or line boundary without truncating a report."""
    remaining = (text or "").strip()
    if not remaining:
        return []
    chunks = []
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
