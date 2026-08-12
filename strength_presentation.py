"""Pure presentation helpers for persisted canonical strength rows."""
from __future__ import annotations

from collections import OrderedDict


def _set_count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1
    count = int(value)
    return count if count > 0 else 1


def format_number(value):
    """Human-readable bounded decimal without `.0` or trailing zeroes."""
    if value is None or isinstance(value, bool):
        return None
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.8f}".rstrip("0").rstrip(".")


def group_rows(rows):
    """Group persisted rows by exercise, preserving first/set row order."""
    groups = OrderedDict()
    set_count = 0
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        name = row.get("exercise_name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        group = groups.setdefault(name, {"exercise_name": name, "sets": []})
        repetitions = row.get("reps")
        weight = format_number(row.get("weight"))
        rendered = (
            f"{weight}×{int(repetitions)}"
            if weight is not None and isinstance(repetitions, (int, float))
               and not isinstance(repetitions, bool)
            else f"{int(repetitions)} повт."
            if isinstance(repetitions, (int, float)) and not isinstance(repetitions, bool)
            else "подход"
        )
        count = _set_count(row.get("sets"))
        group["sets"].extend([rendered] * count)
        set_count += count
    return {
        "exercise_count": len(groups),
        "set_count": set_count,
        "groups": list(groups.values()),
    }


def render_lines(rows):
    grouped = group_rows(rows)
    return grouped, [
        f"{item['exercise_name']}: {', '.join(item['sets'])}"
        for item in grouped["groups"]
    ]


def safe_rows(rows):
    """Strip source/audit metadata while retaining persisted display fields."""
    return [
        {key: row.get(key) for key in
         ("id", "date", "exercise_name", "weight", "sets", "reps")}
        for row in (rows or ()) if isinstance(row, dict)
    ]
