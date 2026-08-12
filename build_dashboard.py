"""
Пересобирает dashboard.html из свежих данных data/whoop.db.
Запускать сколько угодно раз — каждый раз читает актуальную базу и заново считает всю аналитику.

    python build_dashboard.py

Обычно вызывается автоматически из open_dashboard.cmd (пересчёт при каждом открытии)
и из run_fetch.cmd (после утренней загрузки данных).
"""
import sqlite3
import json
import datetime as dt
import statistics as st
import os
import sys
import tempfile
from collections import defaultdict

import workouts_db
import canonical_read_model as CRM
import dashboard_contract

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "whoop.db")
TEMPLATE = os.path.join(HERE, "dashboard_template.html")
OUT = os.path.join(HERE, "dashboard.html")
UI_DIR = os.path.join(HERE, "dashboard_ui")


def write_dashboard_atomic(html_text, output_path=None):
    """Publish a complete dashboard in one filesystem operation.

    Tailscale Serve reads ``dashboard.html`` directly.  Replacing the file only
    after the complete document has been flushed prevents a client from ever
    receiving a partially-written page during a scheduled rebuild.
    """
    output_path = output_path or OUT
    output_dir = os.path.dirname(os.path.abspath(output_path))
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=output_dir,
            prefix=".dashboard-", suffix=".tmp", delete=False,
        ) as target:
            temp_path = target.name
            target.write(html_text)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, output_path)
        temp_path = None
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def load_dashboard_assets(extension):
    """Load dashboard source fragments in deterministic filename order."""
    if not os.path.isdir(UI_DIR):
        return ""
    chunks = []
    for name in sorted(os.listdir(UI_DIR)):
        if not name.endswith(extension):
            continue
        path = os.path.join(UI_DIR, name)
        with open(path, encoding="utf-8") as source:
            chunks.append(f"/* source: dashboard_ui/{name} */\n{source.read().rstrip()}\n")
    return "\n".join(chunks)


# ---------------- helpers ----------------
def pearson(xs, ys):
    pairs = [(a, b) for a, b in zip(xs, ys) if a is not None and b is not None]
    if len(pairs) < 3:
        return None
    xa = [p[0] for p in pairs]
    ya = [p[1] for p in pairs]
    mx, my = st.mean(xa), st.mean(ya)
    num = sum((a - mx) * (b - my) for a, b in pairs)
    den = (sum((a - mx) ** 2 for a in xa) * sum((b - my) ** 2 for b in ya)) ** 0.5
    return (round(num / den, 3), len(pairs)) if den else None


def zone(r):
    if r is None:
        return None
    if r >= 67:
        return "green"
    if r >= 34:
        return "yellow"
    return "red"


# ---------------- analytics ----------------
def build_analytics(workouts, recovery, sleep, date_bounds=None):
    W, R, S = workouts, recovery, sleep
    d = dt.date.fromisoformat
    rec_by_date = {r["date"]: r for r in R}
    sleep_by_wake = {s["wake_date"]: s for s in S}

    day_workouts = defaultdict(list)
    for w in W:
        day_workouts[w["date"]].append(w)

    source_dates = [w["date"] for w in W] + list(rec_by_date) + list(sleep_by_wake)
    if date_bounds:
        source_dates.extend(value for value in date_bounds if value)
    all_dates = sorted(set(source_dates))
    if not all_dates:
        return {
            "summary": {
                "n_workouts": 0, "total_strain": 0, "avg_strain": None,
                "total_hours": 0, "date_start": None, "date_end": None,
                "n_days": 0, "n_train_days": 0, "avg_recovery": None,
                "n_green": 0, "n_yellow": 0, "n_red": 0, "total_dist_km": 0,
            },
            "days": [], "weekly": [], "sports": [], "records": [],
            "sport_series": {},
            "corr": {"rec_vs_load": None, "sleep_vs_load": None, "sleep_vs_recovery": None},
            "overtrain_periods": [], "red_hard_days": [], "undertrained": [],
            "latest_sleep": None, "sleep_nights": [], "generated_at": None,
        }
    d0, d1 = d(all_dates[0]), d(all_dates[-1])
    days = []
    cur = d0
    while cur <= d1:
        ds = cur.isoformat()
        ws = day_workouts.get(ds, [])
        load = sum(x["strain"] for x in ws)
        dur = sum(x["duration_min"] for x in ws)
        rec = rec_by_date.get(ds)
        slp = sleep_by_wake.get(ds)
        days.append({
            "date": ds, "dow": cur.strftime("%a"),
            "load": round(load, 2), "workout_count": len(ws),
            "duration_min": round(dur, 1),
            "max_strain": round(max([x["strain"] for x in ws]), 2) if ws else 0,
            "sports": list(dict.fromkeys(x["sport"] for x in ws)),
            "recovery": rec["score"] if rec else None,
            "resting_hr": rec["resting_hr"] if rec else None,
            "hrv": round(rec["hrv"], 1) if rec else None,
            "sleep_h": slp["hours"] if slp else None,
            "sleep_perf": slp["performance"] if slp else None,
        })
        cur += dt.timedelta(days=1)
    for x in days:
        x["zone"] = zone(x["recovery"])
        x["flag"] = (x["recovery"] is not None and x["recovery"] < 50 and x["load"] >= 8.0)

    # weekly (Mon-start)
    def week_key(ds):
        dd = d(ds)
        return (dd - dt.timedelta(days=dd.weekday())).isoformat()
    wk = defaultdict(lambda: {"load": 0, "count": 0, "dur": 0, "recs": []})
    for day in days:
        k = week_key(day["date"])
        wk[k]["load"] += day["load"]
        wk[k]["count"] += day["workout_count"]
        wk[k]["dur"] += day["duration_min"]
        if day["recovery"] is not None:
            wk[k]["recs"].append(day["recovery"])
    weekly = []
    for k in sorted(wk):
        v = wk[k]
        weekly.append({
            "week_start": k, "load": round(v["load"], 1), "count": v["count"],
            "dur_h": round(v["dur"] / 60, 1),
            "avg_recovery": round(st.mean(v["recs"]), 0) if v["recs"] else None,
        })

    # sports summary
    sport_stats = defaultdict(lambda: {"n": 0, "strain": 0, "dur": 0, "hr": [], "dist": 0})
    for w in W:
        s = sport_stats[w["sport"]]
        s["n"] += 1
        s["strain"] += w["strain"]
        s["dur"] += w["duration_min"]
        if w["avg_hr"]:
            s["hr"].append(w["avg_hr"])
        if w["distance_m"]:
            s["dist"] += w["distance_m"]
    sports = []
    for name, s in sport_stats.items():
        sports.append({
            "sport": name, "n": s["n"], "total_strain": round(s["strain"], 1),
            "avg_strain": round(s["strain"] / s["n"], 1),
            "total_dur_h": round(s["dur"] / 60, 1),
            "avg_hr": round(st.mean(s["hr"]), 0) if s["hr"] else None,
            "total_dist_km": round(s["dist"] / 1000, 2) if s["dist"] else 0,
        })
    sports.sort(key=lambda x: -x["total_strain"])

    # per-sport series + records
    sport_series = {}
    records = []
    for name, s in sport_stats.items():
        ws = sorted([w for w in W if w["sport"] == name], key=lambda x: x["start_local"])
        sport_series[name] = [{
            "date": w["date"], "strain": round(w["strain"], 2),
            "avg_hr": w["avg_hr"], "max_hr": w["max_hr"],
            "duration_min": w["duration_min"], "dist_m": w["distance_m"],
        } for w in ws]
        top_strain = max(ws, key=lambda x: x["strain"])
        longest = max(ws, key=lambda x: x["duration_min"])
        highest_hr = max(ws, key=lambda x: (x["max_hr"] or 0))
        records.append({
            "sport": name, "n": len(ws),
            "max_strain": round(top_strain["strain"], 1), "max_strain_date": top_strain["date"],
            "longest_min": longest["duration_min"], "longest_date": longest["date"],
            "max_hr": highest_hr["max_hr"], "max_hr_date": highest_hr["date"],
        })
    records.sort(key=lambda x: -x["max_strain"])

    # correlations
    train_days = [x for x in days if x["workout_count"] > 0]
    corr = {
        "rec_vs_load": pearson([x["recovery"] for x in train_days], [x["load"] for x in train_days]),
        "sleep_vs_load": pearson([x["sleep_h"] for x in train_days], [x["load"] for x in train_days]),
        "sleep_vs_recovery": pearson([x["sleep_h"] for x in days], [x["recovery"] for x in days]),
    }

    # overtraining runs (>=2 consecutive flagged days)
    runs, run = [], []
    for x in days:
        if x["flag"]:
            run.append(x)
        else:
            if len(run) >= 2:
                runs.append(run[:])
            run = []
    if len(run) >= 2:
        runs.append(run)
    overtrain_periods = [{
        "start": r[0]["date"], "end": r[-1]["date"], "n": len(r),
        "days": [{"date": x["date"], "recovery": x["recovery"], "load": x["load"]} for x in r],
    } for r in runs]

    red_hard = [{"date": x["date"], "recovery": x["recovery"], "load": x["load"], "sports": x["sports"]}
                for x in days if x["recovery"] is not None and x["recovery"] < 50 and x["load"] >= 8.0]
    undertrained = [{"date": x["date"], "recovery": x["recovery"], "load": x["load"],
                     "workout_count": x["workout_count"], "sports": x["sports"]}
                    for x in days if x["recovery"] is not None and x["recovery"] >= 70 and x["load"] < 6.0]

    strains = [w["strain"] for w in W]
    recs = [x["recovery"] for x in days if x["recovery"] is not None]
    summary = {
        "n_workouts": len(W), "total_strain": round(sum(strains), 1),
        "avg_strain": round(st.mean(strains), 1) if strains else None,
        "total_hours": round(sum(w["duration_min"] for w in W) / 60, 1),
        "date_start": all_dates[0], "date_end": all_dates[-1],
        "n_days": (d1 - d0).days + 1, "n_train_days": len(train_days),
        "avg_recovery": round(st.mean(recs), 0) if recs else None,
        "n_green": sum(1 for x in days if x["zone"] == "green"),
        "n_yellow": sum(1 for x in days if x["zone"] == "yellow"),
        "n_red": sum(1 for x in days if x["zone"] == "red"),
        "total_dist_km": round(sum(w["distance_m"] or 0 for w in W) / 1000, 1),
    }

    # все ночи со стадиями (для истории архитектуры сна на «Обзоре»):
    # глубокий/REM/light в часах + время отбоя для аннотации о консистентности
    sleep_nights = [{
        "date": s["wake_date"],
        "deep_h": round(s["stages_ms"]["deep"] / 3.6e6, 2),
        "rem_h": round(s["stages_ms"]["rem"] / 3.6e6, 2),
        "light_h": round(s["stages_ms"]["light"] / 3.6e6, 2),
        "awake_h": round(s["stages_ms"]["awake"] / 3.6e6, 2),
        "hours": s["hours"],
        "perf": s["performance"],
        "bed_local": s["bed_local"],
    } for s in S]

    # последняя ночь целиком (для таймлайн-полосы стадий сна на «Обзоре»)
    latest_sleep = None
    if S:
        ls = S[-1]
        latest_sleep = {
            "wake_date": ls["wake_date"], "hours": ls["hours"],
            "performance": ls["performance"], "efficiency": ls["efficiency"],
            "disturbances": ls["disturbances"], "resp_rate": ls["resp_rate"],
            "stages_ms": ls["stages_ms"],
            "bed_local": ls["bed_local"], "wake_local": ls["wake_local"],
        }

    return {
        "summary": summary, "days": days, "weekly": weekly, "sports": sports,
        "records": records, "sport_series": sport_series, "corr": corr,
        "overtrain_periods": overtrain_periods, "red_hard_days": red_hard,
        "undertrained": undertrained, "latest_sleep": latest_sleep,
        "sleep_nights": sleep_nights,
        "generated_at": all_dates[-1],
    }


# Символы, которые могли бы преждевременно завершить окружающий <script>
# (`</script>`) или сломать разбор JS-строкового литерала (U+2028/U+2029
# — line/paragraph separator, валидны в JSON, но исторически проблемны
# внутри JS-строк). Экранируется весь payload целиком в одном месте —
# отдельные Activity-поля вручную не экранируются.
_INLINE_SCRIPT_ESCAPES = (
    ("<", "\\u003c"),
    (">", "\\u003e"),
    ("&", "\\u0026"),
    (" ", "\\u2028"),
    (" ", "\\u2029"),
)


def dumps_for_inline_script(data):
    """JSON-сериализация, безопасная для вставки внутрь <script>...</script>."""
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for char, escaped in _INLINE_SCRIPT_ESCAPES:
        raw = raw.replace(char, escaped)
    return raw


def main():
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"Не найдена база: {DB_PATH}. Сначала запусти fetch_data.py.")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        with CRM.snapshot_transaction(conn) as model:
            source_snapshot = model.dashboard_snapshot()
    finally:
        conn.close()
    if source_snapshot is None:
        raise SystemExit("В базе нет WHOOP-данных — нечего строить.")
    workouts = source_snapshot["whoop"]["workouts"]
    recovery = source_snapshot["whoop"]["recovery"]
    sleep = source_snapshot["whoop"]["sleep"]
    canonical_period = source_snapshot.get("period") or source_snapshot["canonical_range"].get("period") or {}
    analytics = build_analytics(
        workouts, recovery, sleep,
        (canonical_period.get("start"), canonical_period.get("end")),
    )
    canonical = source_snapshot["canonical_range"]

    # Preserve the established dashboard payload while sourcing every manual
    # facet from the same complete, date-bounded snapshot used by analytics.
    manual_workouts = [
        {key: row.get(key)
         for key in ("id", "date", "exercise_name", "weight", "sets", "reps", "volume")}
        for row in reversed(canonical["activity"]["facets"]["manual_strength"])
    ]
    cardio_exercises = [
        {key: row.get(key) for key in (
            "date", "time", "type", "duration", "distance", "avg_hr", "calories",
            "hr_zone_0_duration", "hr_zone_1_duration", "hr_zone_2_duration",
            "hr_zone_3_duration", "hr_zone_4_duration", "hr_zone_5_duration",
        )}
        for row in reversed(canonical["activity"]["facets"]["manual_cardio"])
    ]
    supplements_log = [
        {key: row.get(key) for key in ("date", "time", "name", "dosage", "taken")}
        for row in reversed(canonical["supplements"]["events"])
    ]
    # The Lifestyle sheet renders measured values from this same canonical
    # snapshot.  A generated Markdown file must never gate or initialize it.
    analytics["lifestyle"] = {
        "present": True,
        "source": "canonical_read_model",
    }
    analytics["manual_workouts"] = manual_workouts
    analytics["supplements_log"] = supplements_log
    analytics["cardio_exercises"] = cardio_exercises
    analytics["canonical_read_model"] = {
        "contract_version": canonical["contract_version"],
        "period": canonical["period"],
        "pagination": canonical["pagination"],
        "activity_summary": canonical["activity"]["summary"],
        "activity_identity": canonical["activity"]["identity"],
        "supplement_summary": canonical["supplements"]["summary"],
        "completeness": canonical["completeness"],
    }

    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    design_css = load_dashboard_assets(".css")
    design_js = load_dashboard_assets(".js")
    dashboard_contract.validate_template(
        tpl, design_css=design_css, design_js=design_js
    )
    js = dumps_for_inline_script(analytics)
    html = tpl.replace("/*__DATA__*/{}", js)
    html = html.replace("/*__DESIGN_CSS__*/", design_css)
    html = html.replace("/*__DESIGN_JS__*/", design_js)
    if "/*__DATA__*/" in html:
        raise SystemExit("Не удалось внедрить данные в шаблон (плейсхолдер не найден).")
    if "/*__DESIGN_" in html:
        raise SystemExit("Dashboard design asset placeholder was not replaced.")
    dashboard_contract.validate_artifact(html)
    write_dashboard_atomic(html)
    s = analytics["summary"]
    print(f"dashboard.html пересобран из {os.path.basename(DB_PATH)}: "
          f"{s['n_workouts']} трен., {s['n_days']} дней "
          f"({s['date_start']}—{s['date_end']}), обновлено {analytics['generated_at']}")


if __name__ == "__main__":
    main()
