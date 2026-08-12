"""
Утренняя сводка: смотрит сон прошлой ночи и recovery на сегодня и говорит,
как тренироваться сегодня — в полную силу / умеренно / отдых, и почему.

Опирается на твои личные базовые уровни (средний recovery, HRV, пульс покоя, сон)
и на паттерны из дашборда:
  * сон — твой сильнейший рычаг recovery;
  * исторически ты тренируешься примерно одинаково независимо от recovery ("автопилот");
  * зелёные дни часто проходят вполсилы — их жалко терять.

    python daily_summary.py

Печатает обычную сводку в консоль и сохраняет её в data/daily_summary.txt
(этот файл читает всплывающее окно show_summary.ps1 — простой текст, без markdown).

Дополнительно сохраняет Telegram-версию (с *жирным* и списками, Markdown для
Bot API) в data/daily_summary_tg.md — её использует morning_check.py при отправке.
Анализ и данные в обеих версиях идентичны, отличается только форматирование текста.
"""
import sqlite3
import json
import datetime as dt
import statistics as st
import os
import sys
from collections import defaultdict

import daily_log
import canonical_read_model as CRM

# На Windows консоль/лог по умолчанию cp1251 и падают на эмодзи — форсируем UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "whoop.db")
OUT_TXT = os.path.join(HERE, "data", "daily_summary.txt")
OUT_MD = os.path.join(HERE, "data", "daily_summary_tg.md")


def parse_iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def local_dt(utc_dt, offset_str):
    sign = 1 if offset_str[0] == "+" else -1
    return utc_dt + sign * dt.timedelta(hours=int(offset_str[1:3]), minutes=int(offset_str[4:6]))


def pearson(pairs):
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if len(pairs) < 3:
        return None
    xa = [p[0] for p in pairs]
    ya = [p[1] for p in pairs]
    mx, my = st.mean(xa), st.mean(ya)
    den = (sum((a - mx) ** 2 for a in xa) * sum((b - my) ** 2 for b in ya)) ** 0.5
    return round(sum((a - mx) * (b - my) for a, b in pairs) / den, 2) if den else None


def load(conn):
    with CRM.snapshot_transaction(conn) as model:
        end = model.latest_outcome_date()
        if end is None:
            return [], [], {}
        # Daily readiness uses an explicit one-year history rather than an
        # unbounded table scan.  The snapshot reports no silently truncated rows.
        start = end - dt.timedelta(days=364)
        outcomes = model.outcomes(start, end)
        whoop = model.whoop_sessions(start, end)
    recovery = [
        {"date": day, "score": values.get("recovery_score"),
         "hrv": values.get("hrv_rmssd"), "rhr": values.get("resting_hr")}
        for day, values in outcomes.items() if values.get("recovery_score") is not None
    ]
    sleep = [
        {"wake_date": day, "hours": values.get("sleep_hours"),
         "performance": values.get("sleep_performance"),
         "resp_rate": values.get("respiratory_rate")}
        for day, values in outcomes.items() if values.get("sleep_hours") is not None
    ]
    load_by_date = defaultdict(float)
    for row in whoop:
        if row.get("strain") is not None:
            load_by_date[dt.date.fromisoformat(row["analysis_date"])] += row["strain"]
    return recovery, sleep, dict(load_by_date)


def build_facts():
    """Считает всю аналитику и возвращает структурированный dict (facts) —
    ни одна из строк тут не форматируется под конкретный вывод (текст/markdown),
    это делают render_plain()/render_markdown() ниже. Возвращает None, если
    в базе вообще нет recovery (тогда caller должен вывести сообщение об этом)."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    recovery, sleep, load_by_date = load(conn)
    conn.close()
    if not recovery:
        return None

    today = dt.date.today()
    rec = recovery[-1]                       # свежий recovery = «сегодня утром»
    sleep_by_wake = {s["wake_date"]: s for s in sleep}
    slp = sleep_by_wake.get(rec["date"]) or (sleep[-1] if sleep else None)

    # личные базовые уровни (по всей истории)
    outcomes = {
        row["date"]: {
            "recovery_score": row["score"],
            "hrv_rmssd": row["hrv"],
            "resting_hr": row["rhr"],
        }
        for row in recovery
    }
    for row in sleep:
        outcomes.setdefault(row["wake_date"], {}).update({
            "sleep_hours": row["hours"],
            "sleep_performance": row["performance"],
        })
    metric_context = {
        item["metric"]: item
        for item in CRM.daily_metric_context(outcomes, rec["date"])
    }
    base_rec = metric_context["recovery"]["baseline_value"]
    base_hrv = metric_context["hrv"]["baseline_value"]
    base_rhr = metric_context["rhr"]["baseline_value"]
    base_sleep = metric_context["sleep_duration"]["baseline_value"]

    # паттерны для «почему»
    corr_sleep_rec = pearson([(s["hours"], next((r["score"] for r in recovery if r["date"] == s["wake_date"]), None))
                              for s in sleep])
    train_days = [(r["score"], load_by_date.get(r["date"])) for r in recovery if load_by_date.get(r["date"], 0) > 0]
    corr_rec_load = pearson(train_days)

    r = rec["score"]
    hrv, rhr = rec["hrv"], rec["rhr"]
    sleep_h = slp["hours"] if slp else None
    sleep_perf = slp["performance"] if slp else None
    resp_rate = slp["resp_rate"] if slp else None

    reasons = []
    # --- базовый уровень по recovery ---
    if r >= 67:
        level, cautious = "FULL", False
        reasons.append(f"Recovery {r} — зелёная зона (≥67). Тело восстановилось и готово к нагрузке.")
    elif r >= 50:
        level, cautious = "MODERATE", False
        reasons.append(f"Recovery {r} — верх жёлтой зоны (34–66). Не красный свет, но и не повод жать на максимум.")
    elif r >= 34:
        level, cautious = "MODERATE", True
        reasons.append(f"Recovery {r} — низ жёлтой зоны. Организм ещё недовосстановлен.")
    else:
        level, cautious = "REST", True
        reasons.append(f"Recovery {r} — красная зона (<34). Восстановление на нуле.")
    if base_rec is not None:
        reasons.append(f"Твоя средняя за период — {base_rec:.0f}; сегодня "
                       f"{'выше' if r > base_rec else 'ниже' if r < base_rec else 'на уровне'} нормы.")

    # --- сон как модификатор (главный рычаг) ---
    if sleep_h is not None:
        extra = f" (норма ~{base_sleep} ч)" if base_sleep else ""
        if sleep_h < 5:
            reasons.append(f"Сон всего {sleep_h} ч{extra} — критически мало, это главный ограничитель на сегодня.")
            if level == "FULL":
                level, cautious = "MODERATE", True
            elif level == "MODERATE":
                cautious = True
        elif sleep_h < 6:
            reasons.append(f"Сон {sleep_h} ч{extra} — недобор.")
            if level == "FULL":
                level, cautious = "MODERATE", True
        elif sleep_h >= 7.5:
            reasons.append(f"Сон {sleep_h} ч{extra} — хорошо выспался, это в плюс.")
        else:
            reasons.append(f"Сон {sleep_h} ч{extra} — в пределах нормы.")
        if sleep_perf is not None and sleep_perf < 70:
            reasons.append(f"Качество сна {sleep_perf}% — ниже хорошего уровня.")

    # --- HRV и пульс покоя как подтверждение ---
    if hrv and base_hrv:
        if hrv < base_hrv * 0.85:
            reasons.append(f"HRV {hrv:.0f} мс заметно ниже твоего базового ({base_hrv:.0f}) — признак усталости или стресса.")
            if not cautious and level == "FULL":
                level = "MODERATE"
                cautious = True
        elif hrv > base_hrv * 1.1:
            reasons.append(f"HRV {hrv:.0f} мс выше базового ({base_hrv:.0f}) — хороший сигнал восстановления.")
    if rhr and base_rhr and rhr > base_rhr + 4:
        reasons.append(f"Пульс покоя {rhr} выше обычного ({base_rhr:.0f}) — тело ещё не полностью отошло.")

    # --- вчерашний вечерний лог (свободный текст) как контекст ---
    #     recovery за rec["date"] сформировался после вечера rec["date"]-1.
    below_norm = base_rec is not None and r < base_rec
    try:
        conn2 = daily_log.connect()
        evening = (rec["date"] - dt.timedelta(days=1)).isoformat()
        elog = daily_log.get_log(conn2, evening)
        conn2.close()
    except Exception:
        elog = None
    if elog and (elog.get("notes") or "").strip():
        note = " ".join(elog["notes"].split())          # схлопываем переносы строк
        if len(note) > 240:
            note = note[:237].rstrip() + "…"
        tail = (" Если recovery ниже обычного — возможно, дело в этом."
                if below_norm else "")
        reasons.append(f"Твоя вчерашняя запись за вечер: «{note}».{tail}")

    # --- вывод паттернов из дашборда ---
    tips = []
    if corr_sleep_rec is not None and corr_sleep_rec >= 0.4:
        tips.append(f"В твоих данных recovery сильнее всего зависит от сна (связь r={corr_sleep_rec}). "
                    f"Хочешь стабильно высокий recovery — начни с режима сна.")
    if corr_rec_load is not None and abs(corr_rec_load) < 0.25:
        tips.append(f"Исторически ты нагружаешься почти одинаково независимо от recovery (r={corr_rec_load}) — "
                    f"«на автопилоте». Сегодня осознанно подстрой усилие под рекомендацию выше.")
    if level == "FULL":
        tips.append("Не слей зелёный день: по данным дашборда часть высоких recovery-дней проходит вполсилы. "
                    "Сегодня — время для ключевой тяжёлой сессии или личного рекорда.")
    if level == "REST" or (level == "MODERATE" and cautious):
        tips.append("Тяжёлая тренировка на низком восстановлении — та самая ситуация, что дашборд помечает "
                    "как риск перетренированности. Сегодня лучше недо-, чем пере-.")

    # --- финальная формулировка ---
    if level == "FULL":
        headline = "🟢 ТРЕНИРОВАТЬСЯ В ПОЛНУЮ СИЛУ"
        action = "Ключевая силовая или интервалы, можно целиться в рекорд. Высокий strain сегодня оправдан."
    elif level == "MODERATE" and not cautious:
        headline = "🟡 УМЕРЕННАЯ ТРЕНИРОВКА"
        action = "Основная работа без максималок: держи strain средним, оставь запас."
    elif level == "MODERATE" and cautious:
        headline = "🟠 ЛЕГКО–УМЕРЕННО, С ОСТОРОЖНОСТЬЮ"
        action = "Техника, объём вместо интенсивности, лёгкое кардио. Никаких PR и максималок сегодня."
    else:
        headline = "🔴 ОТДЫХ ИЛИ ЛЁГКОЕ ВОССТАНОВЛЕНИЕ"
        action = "Прогулка, растяжка, сон. Силовую и интервалы — не сегодня; форсировать нельзя."

    stale = ""
    if rec["date"] < today:
        stale = (f"Свежий recovery — за {rec['date'].isoformat()}, не за сегодня "
                 f"(возможно, утренняя загрузка ещё не отработала).")

    return {
        "today": today, "headline": headline, "action": action,
        "r": r, "hrv": hrv, "rhr": rhr,
        "sleep_h": sleep_h, "sleep_perf": sleep_perf, "resp_rate": resp_rate,
        "stale": stale, "reasons": reasons, "tips": tips,
    }


def render_plain(facts):
    """Обычный текст без markdown — читает всплывающее окно show_summary.ps1 на ПК."""
    f = facts
    L = []
    L.append("=" * 60)
    L.append(f"WHOOP — план на {f['today'].isoformat()}")
    L.append("=" * 60)
    L.append("")
    L.append(f"  {f['headline']}")
    L.append("")
    L.append(f"  Recovery сегодня: {f['r']}    "
             + (f"HRV {f['hrv']:.0f} мс · " if f['hrv'] else "")
             + (f"пульс покоя {f['rhr']}" if f['rhr'] else ""))
    if f['sleep_h'] is not None:
        L.append(f"  Сон прошлой ночью: {f['sleep_h']} ч"
                 + (f" · качество {f['sleep_perf']}%" if f['sleep_perf'] is not None else "")
                 + (f" · дыхание {f['resp_rate']:.1f}/мин" if f['resp_rate'] is not None else ""))
    L.append("")
    L.append(f"  ▶ Что делать: {f['action']}")
    if f['stale']:
        L.append(f"\n⚠ {f['stale']}")
    L.append("")
    L.append("  Почему:")
    for x in f['reasons']:
        L.append(f"    • {x}")
    if f['tips']:
        L.append("")
        L.append("  Твои паттерны:")
        for x in f['tips']:
            L.append(f"    → {x}")
    L.append("=" * 60)
    return "\n".join(L)


def escape_md(s):
    """Экранирует спецсимволы Telegram legacy Markdown (_, *, `, [) в произвольном
    тексте — реплики/заметки могут содержать что угодно, а без экранирования
    Telegram либо ломает разметку, либо API вовсе отказывается парсить сообщение."""
    s = str(s)
    for ch in ("\\", "_", "*", "`", "["):
        s = s.replace(ch, "\\" + ch)
    return s


def load_key_observations():
    import re
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "lifestyle_insights.md")
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        
        idx = content.find("### Ключевые наблюдения")
        if idx == -1:
            return ""
        
        obs = content[idx:]
        next_idx = obs.find("---", len("### Ключевые наблюдения"))
        if next_idx == -1:
            next_idx = obs.find("### Физиологический", len("### Ключевые наблюдения"))
            
        if next_idx != -1:
            obs = obs[:next_idx]
            
        text = obs.strip()
        # Превращаем заголовки в жирный текст для совместимости с Telegram Markdown
        text = re.sub(r"^###\s+(.*)$", r"*\1*", text, flags=re.M)
        text = re.sub(r"^####\s+(.*)$", r"*\1*", text, flags=re.M)
        return text
    except Exception:
        return ""


def render_markdown(facts):
    """Telegram Markdown (parse_mode='Markdown'): жирный вердикт, список показателей
    с эмодзи-иконками, буллеты вместо сплошного текста. Данные и анализ те же,
    что и в render_plain() — отличается только форматирование."""
    f = facts
    L = []
    L.append(f"*WHOOP — план на {f['today'].isoformat()}*")
    L.append("")
    L.append(f"*{f['headline']}*")
    L.append("")

    # --- блок показателей: список с иконками, не сплошным предложением ---
    stats = [f"💪 Recovery: *{f['r']}*"]
    if f['hrv']:
        stats.append(f"💓 HRV: *{f['hrv']:.0f} мс*")
    if f['rhr']:
        stats.append(f"❤️ Пульс покоя: *{f['rhr']}*")
    if f['sleep_h'] is not None:
        s = f"😴 Сон: *{f['sleep_h']} ч*"
        if f['sleep_perf'] is not None:
            s += f" (качество {f['sleep_perf']}%)"
        stats.append(s)
    if f['resp_rate'] is not None:
        stats.append(f"🫁 Дыхание: *{f['resp_rate']:.1f}/мин*")
    L.extend(stats)

    if f['stale']:
        L.append("")
        L.append(f"⚠️ {escape_md(f['stale'])}")

    L.append("")
    L.append("*▶ Что делать:*")
    L.append(f['action'])

    if f['reasons']:
        L.append("")
        L.append("*Почему:*")
        for x in f['reasons']:
            L.append(f"• {escape_md(x)}")

    if f['tips']:
        L.append("")
        L.append("*Твои паттерны:*")
        for x in f['tips']:
            L.append(f"▪️ {escape_md(x)}")

    obs = load_key_observations()
    if obs:
        L.append("")
        L.append(obs)

    L.append("")
    L.append("*📊 Лимиты Gemini API на сегодня:*")
    L.append("Использовано: ~1–2 запроса")
    L.append("Доступно: ~1498 из 1500 (99.8% лимита свободно)")

    return "\n".join(L)


def main():
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"Не найдена база: {DB_PATH}. Сначала запусти fetch_data.py.")
    facts = build_facts()
    if facts is None:
        text = "Нет данных recovery в базе. Запусти fetch_data.py."
        print(text)
        try:
            with open(OUT_TXT, "w", encoding="utf-8") as fp:
                fp.write(text + "\n")
        except OSError:
            pass
        return

    text = render_plain(facts)
    print(text)
    try:
        with open(OUT_TXT, "w", encoding="utf-8") as fp:
            fp.write(text + "\n")
    except OSError:
        pass

    md = render_markdown(facts)
    try:
        with open(OUT_MD, "w", encoding="utf-8") as fp:
            fp.write(md + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
