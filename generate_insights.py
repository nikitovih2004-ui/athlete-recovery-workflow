"""
Использует Gemini API для генерации глубоких физиологических инсайтов
на основе базы данных Whoop (recovery, sleep) и тренировок/БАДов.
"""
import os
import sys
import json
import argparse
import sqlite3
import datetime as dt
import requests

import canonical_read_model as CRM
import gemini_transport

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "whoop.db")
INSIGHTS_MD = os.path.join(HERE, "data", "lifestyle_insights.md")
BASELINE_VALID_DAYS = CRM.BASELINE_VALID_DAYS
MIN_BASELINE_VALID_DAYS = CRM.MIN_BASELINE_VALID_DAYS
BASELINE_LOOKBACK_DAYS = 365

METRIC_SPECS = CRM.DAILY_METRIC_SPECS

# Загружаем переменные окружения
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, ".env"))
except Exception:
    pass

API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip()
FALLBACK_MODELS = [
    item.strip() for item in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash").split(",")
    if item.strip()
]

ANALYSIS_FUNCTION = {
    "name": "return_daily_analysis",
    "description": "Return the completed grounded daily analysis as Markdown.",
    "parameters": {
        "type": "object",
        "properties": {"analysis_markdown": {"type": "string"}},
        "required": ["analysis_markdown"],
    },
}

RU_DOW = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def zone(r):
    if r is None:
        return "нет"
    if r >= 67:
        return "зелёная"
    if r >= 34:
        return "жёлтая"
    return "красная"


def outcome_date_for_context(date_iso):
    """Map local context date D to the WHOOP outcome date D+1."""
    return (dt.date.fromisoformat(date_iso) + dt.timedelta(days=1)).isoformat()


def build_metric_context(outcomes, outcome_date):
    """Compatibility wrapper around the canonical metric contract."""
    return CRM.daily_metric_context(outcomes, outcome_date)


def metric_context_json(metric_context):
    return json.dumps(
        {"schema": "daily_metric_context.v1", "metrics": metric_context},
        ensure_ascii=False,
        sort_keys=True,
    )


def whoop_stored_context(day):
    """Build compact physiological context without event/account identifiers."""
    sessions = []
    for row in (day.get("activities") or {}).get("whoop_sessions", []):
        try:
            raw = json.loads(row.get("raw_json") or "{}")
        except (TypeError, ValueError):
            raw = {}
        sessions.append({
            "sport_name": row.get("sport_name"),
            "start": row.get("start"),
            "end": row.get("end"),
            "duration_minutes": row.get("duration_minutes"),
            "strain": row.get("strain"),
            "average_heart_rate_bpm": row.get("avg_hr"),
            "max_heart_rate_bpm": row.get("max_hr"),
            "energy_kilojoule": row.get("kilojoule"),
            "distance_meter": row.get("distance_meter"),
            "provider_score": raw.get("score") or {},
        })
    return {
        "schema": "whoop_stored_context.v1",
        "action_date": day.get("action_date"),
        "outcome_date": day.get("outcome_date"),
        "workouts": sessions,
        "next_morning_recovery_and_sleep": dict(day.get("next_morning") or {}),
    }


def _model_candidates(primary_model):
    seen = set()
    for candidate in [primary_model, *FALLBACK_MODELS]:
        if candidate and candidate not in seen:
            seen.add(candidate)
            yield candidate

def build_llm_prompt(raw_report):
    metric_contract = (
        "METRIC CONTRACT:\n"
        "- Structured metric values and units are authoritative. Never infer or swap units.\n"
        "- Recovery and sleep performance use %. HRV uses ms. Resting HR uses bpm. Sleep duration uses h.\n"
        "- Compare only when comparison_status is sufficient.\n"
        "- Every comparison must say: average over N valid days from DATE to DATE.\n"
        "- In Russian output use the exact complete form: "
        "'среднее по N валидным дням с DATE по DATE'; do not omit 'валидным'.\n"
        "- Never write only 'historical average' or 'average value'.\n"
        "- If comparison_status is insufficient, say exactly: "
        "'Недостаточно данных для устойчивого среднего: доступно N дней из требуемых 14.'\n"
        "- Never obtain metrics from dashboard HTML, generated summaries, caches, or prose.\n\n"
        "- Keep the final Russian analysis compact and below 1800 characters.\n"
    )
    return metric_contract + (
        "Сформируй наблюдательный отчёт по WHOOP, structured activity и daily context.\n\n"
        "ПРАВИЛА ИСТОЧНИКОВ:\n"
        "- Structured workouts, cardio и supplements — канонические факты.\n"
        "- Daily context — дополнительное narrative-описание факторов дня.\n"
        "- Если тренировка или добавка упомянута и в structured activity, и в daily context, "
        "не считай её второй отдельной записью.\n"
        "- Контекст даты D сопоставляй только с Recovery и Sleep локального утра D+1.\n"
        "- Legacy daily_log rows интерпретируй по тем же правилам.\n\n"
        "ОГРАНИЧЕНИЯ:\n"
        "- Не ставь диагнозов и не утверждай причинность по наблюдательным данным.\n"
        "- Используй формулировки «возможная связь», «совпадает по времени» и "
        "«недостаточно данных для вывода».\n"
        "- Не считай напоминание о добавке фактом приёма; учитывай только structured запись.\n"
        "- Для устойчивых закономерностей требуй несколько сопоставимых наблюдений.\n\n"
        "Для каждого дня отдели канонические structured facts, narrative context и показатели "
        "следующего утра. Заверши краткими наблюдениями и уровнем уверенности.\n\n"
        f"Данные для анализа:\n{raw_report}"
    )


def generate_llm_insights(raw_report, api_key, model):
    prompt = build_llm_prompt(raw_report)

    if os.environ.get("GEMINI_TRANSPORT", "direct").strip().lower() == "relay":
        def validate(result):
            if not isinstance(result, dict) or result.get("name") != ANALYSIS_FUNCTION["name"]:
                raise gemini_transport.RelayTransportError("malformed_provider_result")
            args = result.get("args")
            text = args.get("analysis_markdown") if isinstance(args, dict) else None
            if not isinstance(text, str) or not text.strip() or len(text) > 60_000:
                raise gemini_transport.RelayTransportError("malformed_provider_result")
            return text.strip()

        result, _metadata = gemini_transport.relay_call_with_fallback(
            text=prompt,
            models=list(_model_candidates(model)),
            function_declarations=[ANALYSIS_FUNCTION],
            allowed_function_names={ANALYSIS_FUNCTION["name"]},
            timeout=90,
            result_validator=validate,
        )
        return result
    
    contents = [{"parts": [{"text": prompt}]}]
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    errors = []
    for model_name in _model_candidates(model):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
        generation_config = {"maxOutputTokens": 8192}
        if model_name.startswith("gemini-3"):
            generation_config["thinkingConfig"] = {"thinkingLevel": "low"}
        else:
            generation_config["temperature"] = 0.4
        try:
            resp = requests.post(
                url,
                json={
                    "contents": contents,
                    "generationConfig": generation_config,
                },
                headers=headers,
                timeout=90,
            )
            resp.raise_for_status()
            data = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
            return next(
                part["text"] for part in parts
                if isinstance(part.get("text"), str) and part["text"].strip()
            )
        except Exception as exc:
            errors.append(f"{model_name}: {type(exc).__name__}: {exc}")
    raise RuntimeError("; ".join(errors))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="сгенерировать разбор одного daily context, сохраняя историческую базовую линию",
    )
    ap.add_argument("--output", help="путь для файла сгенерированного разбора")
    ap.add_argument(
        "--context-only", action="store_true",
        help="generate a context/activity-only report when next-morning WHOOP is unavailable",
    )
    args = ap.parse_args()
    selected_date = None
    if args.date:
        try:
            selected_date = dt.date.fromisoformat(args.date).isoformat()
        except ValueError:
            raise SystemExit("--date должен иметь формат YYYY-MM-DD")

    if not os.path.exists(DB_PATH):
        raise SystemExit(f"Не найдена база: {DB_PATH}. Сначала запусти fetch_data.py.")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    with CRM.snapshot_transaction(conn) as model:
        if selected_date:
            start = end = dt.date.fromisoformat(selected_date)
        else:
            start, end = model.available_action_bounds(max_days=None)
        canonical = model.range_snapshot(start, end) if start and end else None
        metric_outcome_date = end + dt.timedelta(days=1) if end else None
        baseline_start = (
            metric_outcome_date - dt.timedelta(days=BASELINE_LOOKBACK_DAYS)
            if metric_outcome_date else None
        )
        canonical_outcomes = (
            model.outcomes(baseline_start, metric_outcome_date)
            if baseline_start and metric_outcome_date else {}
        )
    conn.close()

    days = canonical["days"] if canonical else []
    analysis_days = [day for day in days if selected_date or day.get("context")
                     or any(day["activities"].values()) or day.get("supplements")]
    logs = {
        day["action_date"]: (day.get("context") or {
            "date": day["action_date"], "notes": None, "updated_at": None,
        })
        for day in analysis_days
    }
    recovery, sleep = {}, {}
    workouts_list, supps_list, cardio_list = [], [], []
    for day in days:
        outcome = day.get("next_morning") or {}
        outcome_date = day["outcome_date"]
        if outcome.get("recovery_score") is not None:
            recovery[outcome_date] = {
                "score": outcome.get("recovery_score"), "hrv": outcome.get("hrv_rmssd"),
                "rhr": outcome.get("resting_hr"),
            }
        if outcome.get("sleep_hours") is not None:
            sleep[outcome_date] = {
                "hours": outcome.get("sleep_hours"),
                "performance": outcome.get("sleep_performance"),
            }
        workouts_list.extend(day["activities"]["manual_strength"])
        cardio_list.extend(day["activities"]["manual_cardio"])
        supps_list.extend(day.get("supplements") or [])
    output_path = args.output or (
        os.path.join(HERE, "data", f"daily_insights_{selected_date}.md")
        if selected_date else INSIGHTS_MD
    )

    # Группируем тренировки, БАДы и кардио по датам
    from collections import defaultdict
    workouts_by_date = defaultdict(list)
    for w in workouts_list:
        workouts_by_date[w["date"]].append(w)
        
    supps_by_date = defaultdict(list)
    for s in supps_list:
        supps_by_date[s["date"]].append(s)

    cardio_by_date = defaultdict(list)
    for c in cardio_list:
        cardio_by_date[c["date"]].append(c)

    lines = []
    def add_line(text=""):
        print(text)
        lines.append(text)

    add_line("=" * 74)
    add_line("ФАКТОРЫ ДНЯ — данные для анализа (дата D → локальное утро D+1)")
    add_line("=" * 74)

    if not logs:
        add_line("\nВ выбранном bounded-периоде нет daily context или structured activity.")
        return

    # Typed baseline diagnostics use the same canonical contract sent to the model.
    diagnostic_context = {
        item["metric"]: item for item in build_metric_context(canonical_outcomes, metric_outcome_date)
    }
    base_rec = diagnostic_context["recovery"]["baseline_value"]
    base_hrv = diagnostic_context["hrv"]["baseline_value"]
    base_rhr = diagnostic_context["rhr"]["baseline_value"]
    base_sleep = diagnostic_context["sleep_duration"]["baseline_value"]
    add_line("\nБазовые уровни: среднее по последним 28 валидным дням до текущего утра:")
    add_line(f"  recovery ~{base_rec} · HRV ~{base_hrv} мс · пульс покоя ~{base_rhr} · сон ~{base_sleep} ч")

    metric_context = build_metric_context(canonical_outcomes, metric_outcome_date)
    llm_lines = [
        "STRUCTURED_METRIC_CONTEXT_JSON:",
        metric_context_json(metric_context),
        "\nDaily canonical facts:",
    ]

    linked = 0
    add_line(f"\nЗаписей в логе: {len(logs)}")
    add_line("-" * 74)
    for date_iso in sorted(logs):
        row = logs[date_iso]
        note = (row.get("notes") or "").strip()
        try:
            d = dt.date.fromisoformat(date_iso)
            nxt = outcome_date_for_context(date_iso)
            dow = RU_DOW[d.weekday()]
        except ValueError:
            continue
        rec = recovery.get(nxt)
        slp = sleep.get(nxt)
        if rec:
            linked += 1

        add_line(f"\n● День {date_iso} ({dow})")
        llm_lines.append(f"\nДень {date_iso} ({dow}):")
        canonical_day = next(
            (day for day in days if day.get("action_date") == date_iso), None
        )
        if canonical_day:
            llm_lines.extend([
                "- WHOOP_STORED_CONTEXT_JSON (authoritative imported categories):",
                json.dumps(whoop_stored_context(canonical_day), ensure_ascii=False,
                           sort_keys=True),
            ])
        
        # Интегрируем силовые тренировки за этот день
        day_workouts = workouts_by_date.get(date_iso, [])
        if day_workouts:
            llm_lines.append("- Выполненные силовые тренировки:")
            for w in day_workouts:
                weight_str = f" {w['weight']}кг" if w['weight'] else ""
                llm_lines.append(f"  • {w['exercise_name']}:{weight_str} {w['sets']}х{w['reps']} (объем {w['volume']}кг)")
                add_line(f"    [Силовая] {w['exercise_name']}:{weight_str} {w['sets']}х{w['reps']}")
                
        # Интегрируем кардио-тренировки за этот день
        day_cardio = cardio_by_date.get(date_iso, [])
        if day_cardio:
            llm_lines.append("- Выполненные кардио-тренировки:")
            for c in day_cardio:
                dist_str = f" {c['distance']}км" if c['distance'] else ""
                hr_str = f" ЧСС {c['avg_hr']}" if c['avg_hr'] else ""
                cal_str = f" {c['calories']}ккал" if c['calories'] else ""
                llm_lines.append(f"  • {c['type']}: {c['duration']}мин{dist_str}{hr_str}{cal_str}")
                add_line(f"    [Кардио] {c['type']}: {c['duration']}мин{dist_str}")

        # Structured supplement rows are canonical; reminder text is never a row.
        day_supps = supps_by_date.get(date_iso, [])
        if day_supps:
            llm_lines.append("- Structured supplements (канонические записи):")
            for s in day_supps:
                dos_str = f" ({s['dosage']})" if s['dosage'] else ""
                status = "принято" if s.get("taken") == 1 else "не принято" if s.get("taken") == 0 else "статус неизвестен"
                llm_lines.append(f"  • {s['name']}{dos_str} в {s['time']} — {status}")
                add_line(f"    [Добавка: {status}] {s['name']}{dos_str} в {s['time']}")

        if note:
            llm_lines.append(f"- Daily context (narrative, не дублировать structured activity): {note}")
            add_line("    [Daily context recorded; raw text redacted from process output]")
        else:
            if not day_workouts and not day_supps and not day_cardio:
                add_line("    (пустая запись)")
                llm_lines.append("- Daily context: (нет записи)")
        
        if rec:
            hrv = f"{rec['hrv']:.0f}" if rec["hrv"] is not None else "—"
            add_line(f"    → Утро {nxt}: recovery {rec['score']} ({zone(rec['score'])}), "
                      f"HRV {hrv} мс, пульс покоя {rec['rhr']}")
            llm_lines.append(f"- Утро следующего дня {nxt}: восстановление {rec['score']}% ({zone(rec['score'])} зона), HRV {hrv} мс, пульс покоя {rec['rhr']} уд/мин")
        else:
            add_line(f"    → Утро {nxt}: recovery ещё нет в базе")
        if slp:
            perf = f", качество {slp['performance']}%" if slp["performance"] is not None else ""
            add_line(f"       сон: {slp['hours']} ч{perf}")
            perf_llm = f", качество сна {slp['performance']}%" if slp["performance"] is not None else ""
            llm_lines.append(f"- Сон: {slp['hours']} ч{perf_llm}")

    add_line("\n" + "-" * 74)
    add_line(f"Записей, у которых есть recovery следующего утра: {linked} из {len(logs)}")
    exists = os.path.exists(output_path)
    add_line(f"Файл инсайтов: {output_path} — {'существует, будет перезаписан' if exists else 'пока нет'}")
    add_line("=" * 74)

    llm_report = "\n".join(llm_lines)
    if args.context_only:
        llm_report = (
            "CONTEXT-ONLY FALLBACK: WHOOP Recovery, HRV, resting HR and Sleep for the "
            "required next morning are unavailable. Do not state or infer any WHOOP value, "
            "do not compare outcomes, and explicitly label the report as context-only.\n\n"
            + llm_report
        )

    relay_mode = os.environ.get("GEMINI_TRANSPORT", "direct").strip().lower() == "relay"
    if not relay_mode and not API_KEY:
        print("\n[Внимание] GEMINI_API_KEY не задан в .env. Генерация ИИ-инсайтов пропущена.")
        print("Для автоматического анализа добавьте ключ в .env.")
        return

    print(f"\nОтправляю запрос через Gemini transport (модель {MODEL})...")
    try:
        insights = generate_llm_insights(llm_report, API_KEY, MODEL)
        if insights.strip().startswith("```markdown"):
            insights = insights.strip().split("\n", 1)[1]
            if insights.endswith("```"):
                insights = insights[:-3]
        elif insights.strip().startswith("```"):
            insights = insights.strip().split("\n", 1)[1]
            if insights.endswith("```"):
                insights = insights[:-3]
        
        insights = insights.strip()
        
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(insights)
        print(f"[Успех] Новые инсайты сгенерированы и сохранены в {output_path}")

        # Автоматическая пересборка дашборда
        print("Пересобираю дашборд...")
        import subprocess
        PY = sys.executable
        r = subprocess.run([PY, os.path.join(HERE, "build_dashboard.py")],
                           cwd=HERE, capture_output=True, text=True, encoding="utf-8")
        if r.returncode == 0:
            print("  [OK] Дашборд успешно обновлен.")
        else:
            print(f"  [Ошибка] Не удалось обновить дашборд: {r.stderr}")

    except Exception as e:
        print(f"[Ошибка] Не удалось получить ИИ-инсайты: {type(e).__name__}")


if __name__ == "__main__":
    main()
