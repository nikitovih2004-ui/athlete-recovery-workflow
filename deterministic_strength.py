"""Narrow, explicit Russian strength grammar for provider-failure fallback."""
from __future__ import annotations

import datetime as dt
import re

_SET = re.compile(
    r"(?P<w>\d+(?:[.,]\d+)?)\s*(?:x|\u0445|\u00d7)\s*(?P<r>\d+)",
    re.I,
)
_SEPARATORS = re.compile(r"^[\s,;/]+$")
_YESTERDAY = frozenset({"вчера", "за вчера", "тренировка вчера", "силовая вчера"})
_TODAY = frozenset({"сегодня", "за сегодня", "тренировка сегодня", "силовая сегодня", "силовая"})
_UNSAFE_FACT_MARKERS = re.compile(
    r"\b(?:завтра|послезавтра|план|планирую|буду|хочу|не\s+делал|не\s+выполнял|"
    r"пропустил|пример|шаблон)\b",
    re.I,
)
_DRAFT_ACTION = re.compile(
    r"\b(?:запиши|начни|создай|открой|start|begin|log)\w*\b", re.I,
)
_DRAFT_STRENGTH = re.compile(r"\b(?:силов\w*|strength\w*)\b", re.I)
_DRAFT_FUTURE = re.compile(r"\b(?:завтра|потом|позже|tomorrow|later)\b", re.I)
_COMMIT_DIRECTIVE = re.compile(
    r"^(?:запиши|сохрани|добавь)(?:\s+(?:эту|это|тренировку|силовую))*[.!]?$",
    re.I,
)
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}
_DATE_METADATA = re.compile(
    r"(?:^|[\s,;—-]+)(?:силовая\s+)?тренировка\s+за\s+"
    r"(?P<day>\d{1,2})\s+(?P<month>"
    + "|".join(_MONTHS)
    + r")(?:\s+(?P<year>\d{4}))?[.!]?\s*$",
    re.I,
)
_INCOMPLETE_SET = re.compile(
    r"(?P<w>\d+(?:[.,]\d+)?)\s*(?:x|х|×)\s*(?!\d)", re.I,
)


def draft_start_date(text, local_now):
    """Resolve an explicit empty strength-draft command without model choice.

    This deliberately excludes messages containing sets, weights, multi-line
    workout data, explicit future intent, or an absolute numeric date. Those
    continue through the bounded semantic router.
    """
    source = (text or "").strip()
    if not source or "\n" in source or ":" in source or _SET.search(source):
        return None
    if any(char.isdigit() for char in source) or _DRAFT_FUTURE.search(source):
        return None
    if not (_DRAFT_ACTION.search(source) and _DRAFT_STRENGTH.search(source)):
        return None
    lowered = source.casefold()
    if "вчера" in lowered or "yesterday" in lowered:
        return (local_now.date() - dt.timedelta(days=1)).isoformat()
    if "сегодня" in lowered or "today" in lowered:
        return local_now.date().isoformat()
    return local_now.date().isoformat()


def _line_entries(line):
    if ":" not in line:
        return None, False
    name, body = line.split(":", 1)
    name = name.strip()
    if not name:
        return None, False
    matches = list(_SET.finditer(body))
    if not matches:
        return None, False
    remainder = _SET.sub("", body)
    complete = not remainder or bool(_SEPARATORS.fullmatch(remainder))
    return [
        {
            "exercise_name": name,
            "weight_kg": float(match["w"].replace(",", ".")),
            "sets": 1,
            "reps": int(match["r"]),
        }
        for match in matches
    ], complete


def _strip_date_metadata(line, local_now):
    """Remove an unambiguous trailing workout date and return its date_ref."""
    match = _DATE_METADATA.search(line)
    if not match:
        return line, None
    if local_now is None:
        return line, None
    year = int(match["year"]) if match["year"] else local_now.year
    try:
        value = dt.date(
            year, _MONTHS[match["month"].casefold()], int(match["day"])
        )
    except ValueError:
        return line, {"kind": "ambiguous", "value": None}
    # A missing year refers to the most recent occurrence, never a future date.
    if not match["year"] and value > local_now.date():
        value = value.replace(year=year - 1)
    return line[:match.start()].rstrip(" ,;—-"), {
        "kind": "absolute", "value": value.isoformat(),
    }


def parse(text, local_now=None):
    """Parse only explicit completed sets; ambiguity is returned as incomplete."""
    source = text or ""
    lines = [
        raw.strip(" -\u2022\t")
        for raw in source.splitlines()
        if raw.strip()
    ]
    if not lines:
        return None
    unsafe = bool(_UNSAFE_FACT_MARKERS.search(source))
    entries = []
    incomplete = unsafe
    date_ref = {"kind": "today", "value": None}
    for line in lines:
        line, metadata_date_ref = _strip_date_metadata(line, local_now)
        if metadata_date_ref:
            date_ref = metadata_date_ref
            if not line:
                continue
        lowered = line.casefold().strip(" .:")
        if lowered in _YESTERDAY:
            date_ref = {"kind": "yesterday", "value": None}
            continue
        if lowered in _TODAY:
            continue
        # A trailing imperative such as "Запиши эту" confirms the explicit
        # payload; it is not an unparsed exercise and must not make a complete
        # one-message workout ambiguous.
        if _COMMIT_DIRECTIVE.fullmatch(line.strip()):
            continue
        parsed, complete = _line_entries(line)
        if parsed is None:
            incomplete = True
            continue
        entries.extend(parsed)
        incomplete = incomplete or not complete
    if not entries:
        return None
    return {
        "entries": entries,
        "incomplete": incomplete,
        "date_ref": date_ref,
        "fact_status": "unknown" if unsafe else "completed",
    }


def parse_draft_update(text):
    """Parse an exercise update for an already-open draft.

    This is deliberately narrower than the semantic router: every non-empty
    line must be an exercise line, and at most one unfinished ``weight×`` set
    is accepted so the draft can ask for repetitions without losing context.
    """
    source = (text or "").strip()
    if not source:
        return None
    exercises = []
    incomplete_count = 0
    for raw in source.splitlines():
        line = raw.strip(" -\u2022\t")
        if not line or ":" not in line:
            return None
        name, body = line.split(":", 1)
        name = name.strip()
        if not name:
            return None
        complete_matches = list(_SET.finditer(body))
        incomplete_matches = list(_INCOMPLETE_SET.finditer(body))
        spans = [(match.start(), match.end()) for match in complete_matches]
        incomplete_matches = [
            match for match in incomplete_matches
            if not any(start <= match.start() < end for start, end in spans)
        ]
        if len(incomplete_matches) > 1:
            return None
        remainder = _SET.sub("", body)
        remainder = _INCOMPLETE_SET.sub("", remainder)
        if remainder and not _SEPARATORS.fullmatch(remainder):
            return None
        sets = [
            {"weight_kg": float(match["w"].replace(",", ".")),
             "reps": int(match["r"])}
            for match in complete_matches
        ]
        for match in incomplete_matches:
            sets.append({"weight_kg": float(match["w"].replace(",", ".")),
                         "reps": None})
            incomplete_count += 1
        if not sets or incomplete_count > 1:
            return None
        exercises.append({"exercise_name": name, "sets": sets,
                          "side": None, "note": None})
    return exercises or None
