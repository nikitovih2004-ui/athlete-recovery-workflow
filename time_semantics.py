"""Shared, explicit calendar semantics for canonical read models.

All persisted WHOOP instants are interpreted as instants and projected onto the
configured analysis timezone.  Manual ``date`` values are already local calendar
dates and are therefore validated, never reinterpreted as UTC timestamps.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo


ANALYSIS_TZ_NAME = "Europe/Kyiv"
ANALYSIS_TZ = ZoneInfo(ANALYSIS_TZ_NAME)
UTC = dt.timezone.utc


def as_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def parse_instant(value) -> dt.datetime | None:
    """Parse an ISO instant; legacy naive values are explicitly treated as UTC."""
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def as_analysis_datetime(value) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        raise TypeError("value must be datetime")
    if value.tzinfo is None:
        return value.replace(tzinfo=ANALYSIS_TZ)
    return value.astimezone(ANALYSIS_TZ)


def analysis_date_for_instant(value) -> dt.date | None:
    parsed = parse_instant(value)
    return parsed.astimezone(ANALYSIS_TZ).date() if parsed else None


def action_to_outcome_date(value) -> dt.date:
    return as_date(value) + dt.timedelta(days=1)


def outcome_to_action_date(value) -> dt.date:
    return as_date(value) - dt.timedelta(days=1)


def date_range(start, end):
    """Yield an inclusive, validated local-calendar date range."""
    start_day, end_day = as_date(start), as_date(end)
    if end_day < start_day:
        raise ValueError("end date precedes start date")
    for offset in range((end_day - start_day).days + 1):
        yield start_day + dt.timedelta(days=offset)


def utc_bounds_for_analysis_dates(start, end_exclusive) -> tuple[str, str]:
    """UTC ISO bounds for a half-open analysis-timezone calendar interval."""
    start_day, end_day = as_date(start), as_date(end_exclusive)
    if end_day < start_day:
        raise ValueError("end date precedes start date")
    start_dt = dt.datetime.combine(start_day, dt.time.min, tzinfo=ANALYSIS_TZ)
    end_dt = dt.datetime.combine(end_day, dt.time.min, tzinfo=ANALYSIS_TZ)
    return start_dt.astimezone(UTC).isoformat(), end_dt.astimezone(UTC).isoformat()

