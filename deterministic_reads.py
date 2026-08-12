"""Bounded Russian read planner used before and after an unavailable model.

It recognizes only read-only questions and emits fixed allowlisted arguments.
It never parses mutations, SQL, or user-selected identifiers.
"""
from __future__ import annotations

import datetime as dt
import re

import conversation_contract as C


def _text(value):
    return re.sub(r"\s+", " ", (value or "").casefold()).strip()


def _window(text):
    if any(x in text for x in ("месяц", "мес.", "28 дней")):
        return 28
    if any(x in text for x in ("две недели", "14 дней")):
        return 14
    if any(x in text for x in ("квартал", "три месяца", "84 дня")):
        return 84
    return 7


def plan(message, local_now, session_state=None):
    """Return ``(intent, validated_args)`` or ``None`` for non-read text."""
    text = _text(message)
    if not text:
        return None
    if "вчера" in text and any(x in text for x in ("что было", "что было", "покажи", "итог", "данные")):
        return C.INTENT_GET_DAY_SNAPSHOT, {"date": (local_now.date() - dt.timedelta(days=1)).isoformat()}
    if any(x in text for x in ("накоплен", "сколько дней", "покрытие", "какие данные", "есть данные")):
        return C.INTENT_GET_DATA_COVERAGE, {}
    if any(x in text for x in ("бад", "бадов", "добавк", "supplement")) and any(x in text for x in ("какие", "запис", "сейчас", "список", "что принимал")):
        return C.INTENT_GET_SUPPLEMENT_RECORDS, {}
    metric = None
    if "hrv" in text or "вср" in text:
        metric = "hrv_rmssd"
    elif "recovery" in text or "восстанов" in text:
        metric = "recovery_score"
    elif any(x in text for x in ("rhr", "пульс покоя", "пульс в покое")):
        metric = "resting_hr"
    elif "сон" in text:
        metric = "sleep_hours"
    if metric and any(x in text for x in ("тренд", "динамик", "за неделю", "за месяц", "месяц", "недел")):
        return C.INTENT_GET_METRIC_TREND, {"metric": metric, "window_days": _window(text)}
    # A short period-only follow-up inherits only a previous typed metric query.
    last = (session_state or {}).get("last_query", {})
    if ("месяц" in text or "недел" in text) and last.get("metric") in C.TREND_METRICS:
        return C.INTENT_GET_METRIC_TREND, {"metric": last["metric"], "window_days": _window(text)}
    return None
