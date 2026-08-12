"""Pure, versioned read model shared by every reporting surface.

The module owns interpretation, not persistence.  It performs no DDL, never
commits caller work, and keeps WHOOP physiology and manually logged detail as
separate facets unless an explicit link exists in a future schema.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import math
import re
import sqlite3
import statistics
import unicodedata
from collections import defaultdict

import time_semantics as TS


CONTRACT_VERSION = "canonical_read_model.v1"
MIN_FACTOR_CONFIDENCE = 0.85
MIN_FACTOR_COHORT = 5
METRICS = (
    "recovery_score", "hrv_rmssd", "resting_hr",
    "sleep_hours", "sleep_performance",
)
BASELINE_VALID_DAYS = 28
MIN_BASELINE_VALID_DAYS = 14
DAILY_METRIC_SPECS = {
    "recovery": {"source_field": "recovery_score", "unit": "%", "minimum": 0, "maximum": 100},
    "hrv": {"source_field": "hrv_rmssd", "unit": "ms", "minimum": 0, "maximum": None},
    "rhr": {"source_field": "resting_hr", "unit": "bpm", "minimum": 0, "maximum": None},
    "sleep_duration": {"source_field": "sleep_hours", "unit": "h", "minimum": 0, "maximum": 24},
    "sleep_performance": {"source_field": "sleep_performance", "unit": "%", "minimum": 0, "maximum": 100},
}
DEFAULT_FACTORS = (
    ("supplement", "creatine"),
    ("supplement", "magnesium"),
    ("daily_factor", "alcohol"),
    ("daily_factor", "late_caffeine"),
    ("daily_factor", "late_meal"),
    ("daily_factor", "high_stress"),
)

_SUPPLEMENT_ALIASES = {
    "creatine": {"creatine", "креатин", "креатина", "creatine monohydrate", "креатин моногидрат"},
    "magnesium": {"magnesium", "магний", "магния", "mg"},
    "omega_3": {"omega 3", "omega-3", "omega3", "омега 3", "омега-3", "омега3"},
    "vitamin_d": {"vitamin d", "vit d", "витамин d", "витамин д", "д3", "d3"},
}
_DAILY_FACTOR_ALIASES = {
    "alcohol": {"alcohol", "алкоголь"},
    "late_caffeine": {"late caffeine", "поздний кофеин", "кофеин поздно"},
    "late_meal": {"late meal", "late food", "поздняя еда", "поздний ужин"},
    "high_stress": {"high stress", "сильный стресс", "высокий стресс"},
}


class ReadModelInputError(ValueError):
    pass


def _valid_daily_metric_value(value, spec):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if value <= spec["minimum"]:
        return False
    maximum = spec.get("maximum")
    return maximum is None or value <= maximum


def daily_metric_context(outcomes, outcome_date):
    """Canonical current/previous-28-valid-days metric contract.

    ``outcomes`` must be the date-keyed result of ``ReadModel.outcomes``.
    The current outcome date is always excluded from every baseline.
    """
    outcome_day = (
        outcome_date if isinstance(outcome_date, dt.date)
        else dt.date.fromisoformat(str(outcome_date))
    )
    current = outcomes.get(outcome_day, {})
    result = []
    for metric, spec in DAILY_METRIC_SPECS.items():
        source_field = spec["source_field"]
        observations = []
        excluded_invalid = 0
        for day, row in sorted(outcomes.items()):
            if day >= outcome_day:
                continue
            value = row.get(source_field)
            if _valid_daily_metric_value(value, spec):
                observations.append((day, float(value)))
            elif value is not None:
                excluded_invalid += 1
        observations = observations[-BASELINE_VALID_DAYS:]
        current_value = current.get(source_field)
        current_value = (
            float(current_value)
            if _valid_daily_metric_value(current_value, spec) else None
        )
        count = len(observations)
        sufficient = count >= MIN_BASELINE_VALID_DAYS
        result.append({
            "metric": metric,
            "source_field": source_field,
            "current_value": current_value,
            "unit": spec["unit"],
            "baseline_value": round(
                statistics.mean(value for _, value in observations), 1
            ) if sufficient else None,
            "baseline_method": "mean",
            "period_start": observations[0][0].isoformat() if observations else None,
            "period_end": observations[-1][0].isoformat() if observations else None,
            "valid_observations": count,
            "required_observations": MIN_BASELINE_VALID_DAYS,
            "target_observations": BASELINE_VALID_DAYS,
            "excluded_invalid_values": excluded_invalid,
            "current_outcome_date": outcome_day.isoformat(),
            "comparison_status": "sufficient" if sufficient else "insufficient",
        })
    return result


def _normalize(value):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).split())


def _alias_index(mapping):
    return {
        _normalize(alias): canonical
        for canonical, aliases in mapping.items()
        for alias in {canonical, *aliases}
    }


_SUPPLEMENT_INDEX = _alias_index(_SUPPLEMENT_ALIASES)
_DAILY_FACTOR_INDEX = _alias_index(_DAILY_FACTOR_ALIASES)


def normalize_factor_key(factor_type, value):
    normalized = _normalize(value)
    if not normalized or len(normalized) > 120:
        raise ReadModelInputError("factor key is outside the bounded contract")
    if factor_type == "supplement":
        return _SUPPLEMENT_INDEX.get(normalized, normalized.replace(" ", "_"))
    if factor_type == "daily_factor":
        canonical = _DAILY_FACTOR_INDEX.get(normalized)
        if canonical is None:
            raise ReadModelInputError("daily factor is outside the static allowlist")
        return canonical
    raise ReadModelInputError("factor type must be supplement or daily_factor")


_DOSE_RE = re.compile(
    r"\s*(?P<value>\d+(?:[.,]\d+)?)\s*"
    r"(?P<unit>мкг|µg|μg|ug|mcg|мг|mg|гр|г|gr|gram|grams|g)\s*",
    re.IGNORECASE,
)
_UNIT_GRAMS = {
    "г": 1.0, "гр": 1.0, "g": 1.0, "gr": 1.0, "gram": 1.0, "grams": 1.0,
    "мг": 0.001, "mg": 0.001,
    "мкг": 0.000001, "µg": 0.000001, "μg": 0.000001,
    "ug": 0.000001, "mcg": 0.000001,
}


def parse_mass_dose(value):
    """Return normalized mass without guessing units from a bare number."""
    if value is None or not str(value).strip():
        return {"value": None, "unit": None, "grams": None, "parse_status": "missing"}
    match = _DOSE_RE.fullmatch(str(value))
    if not match:
        return {"value": None, "unit": None, "grams": None, "parse_status": "unsupported"}
    try:
        number = float(match.group("value").replace(",", "."))
    except ValueError:
        return {"value": None, "unit": None, "grams": None, "parse_status": "invalid"}
    unit = match.group("unit").casefold()
    grams = number * _UNIT_GRAMS[unit]
    if not math.isfinite(grams) or grams < 0:
        return {"value": None, "unit": unit, "grams": None, "parse_status": "invalid"}
    return {"value": number, "unit": unit, "grams": grams, "parse_status": "converted"}


def _dict_rows(cursor):
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _table_columns(conn, table):
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def _rows(conn, table, columns, *, where="", params=(), order=""):
    existing = _table_columns(conn, table)
    if not existing:
        return []
    selected = [name for name in columns if name in existing]
    if not selected:
        return []
    sql = f"SELECT {', '.join(selected)} FROM {table}"
    if where:
        sql += f" WHERE {where}"
    if order:
        safe_parts = []
        for part in order.split(","):
            identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", part)
            referenced = [name for name in identifiers if name.lower() not in {
                "datetime", "asc", "desc", "nulls", "first", "last",
            }]
            if referenced and all(name in existing for name in referenced):
                safe_parts.append(part.strip())
        if safe_parts:
            sql += f" ORDER BY {', '.join(safe_parts)}"
    return _dict_rows(conn.execute(sql, params))


@contextlib.contextmanager
def snapshot_transaction(conn):
    """Give all reads one SQLite snapshot without committing or running DDL."""
    owns = not conn.in_transaction
    if owns:
        conn.execute("BEGIN")
    try:
        yield CanonicalReadModel(conn)
    finally:
        if owns and conn.in_transaction:
            conn.rollback()


def _mean(values):
    values = [float(value) for value in values if value is not None]
    return round(statistics.mean(values), 2) if values else None


def _median(values):
    values = [float(value) for value in values if value is not None]
    return round(statistics.median(values), 2) if values else None


def _sleep_values(raw_json):
    try:
        raw = json.loads(raw_json or "{}")
    except (TypeError, ValueError):
        return None, False
    if raw.get("nap"):
        return None, True
    stages = ((raw.get("score") or {}).get("stage_summary") or {})
    in_bed = stages.get("total_in_bed_time_milli")
    awake = stages.get("total_awake_time_milli")
    if not isinstance(in_bed, (int, float)) or not isinstance(awake, (int, float)):
        return None, False
    asleep = in_bed - awake
    return (round(asleep / 3_600_000, 2) if asleep >= 0 else None), False


def _source_event_key(source_key, kind):
    if not isinstance(source_key, str) or not source_key:
        return None
    marker = f":{kind}:"
    if marker in source_key:
        return source_key.split(marker, 1)[0]
    suffix = f":{kind}"
    return source_key[:-len(suffix)] if source_key.endswith(suffix) else source_key


class CanonicalReadModel:
    def __init__(self, conn):
        self.conn = conn
        self.as_of = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    def metadata(self, start, end):
        return {
            "contract_version": CONTRACT_VERSION,
            "analysis_timezone": TS.ANALYSIS_TZ_NAME,
            "as_of": self.as_of,
            "period": {"start": TS.as_date(start).isoformat(), "end": TS.as_date(end).isoformat()},
            "pagination": {"truncated": False, "has_more": False, "next_cursor": None},
        }

    def latest_outcome_date(self):
        """Latest available health outcome date, computed with shared timezone semantics."""
        candidates = []
        for table, column in (("recovery", "created_at"), ("sleep", "end")):
            if column not in _table_columns(self.conn, table):
                continue
            row = self.conn.execute(
                f"SELECT {column} FROM {table} ORDER BY datetime({column}) DESC LIMIT 1"
            ).fetchone()
            if row:
                day = TS.analysis_date_for_instant(row[0])
                if day:
                    candidates.append(day)
        return max(candidates) if candidates else None

    def available_action_bounds(self, max_days=365):
        """Bound the union date spine without loading detail rows."""
        if max_days is not None and (
            not isinstance(max_days, int) or isinstance(max_days, bool) or max_days < 1
        ):
            raise ValueError("max_days must be a positive integer")
        candidates = []
        for table, column in (
            ("workout_exercises", "date"), ("cardio_exercises", "date"),
            ("supplements_log", "date"), ("daily_context_entries", "context_date"),
            ("daily_log", "date"),
            ("daily_factor_observations", "context_date"),
        ):
            if column not in _table_columns(self.conn, table):
                continue
            active = " WHERE deleted_at IS NULL" if "deleted_at" in _table_columns(self.conn, table) else ""
            row = self.conn.execute(
                f"SELECT MIN({column}), MAX({column}) FROM {table}{active}"
            ).fetchone()
            for value in row or ():
                try:
                    candidates.append(TS.as_date(value))
                except (TypeError, ValueError):
                    pass
        latest_outcome = self.latest_outcome_date()
        if latest_outcome:
            candidates.append(TS.outcome_to_action_date(latest_outcome))
        whoop_bounds = []
        if "start" in _table_columns(self.conn, "workouts"):
            rows = self.conn.execute(
                "SELECT start FROM workouts ORDER BY datetime(start) ASC LIMIT 1"
            ).fetchall()
            rows += self.conn.execute(
                "SELECT start FROM workouts ORDER BY datetime(start) DESC LIMIT 1"
            ).fetchall()
            whoop_bounds = [TS.analysis_date_for_instant(row[0]) for row in rows]
            candidates.extend(day for day in whoop_bounds if day)
        if not candidates:
            return None, None
        end = max(candidates)
        natural_start = min(candidates)
        if max_days is None:
            return natural_start, end
        return max(natural_start, end - dt.timedelta(days=max_days - 1)), end

    def outcomes(self, start, end):
        start_day, end_day = TS.as_date(start), TS.as_date(end)
        utc_start, utc_end = TS.utc_bounds_for_analysis_dates(
            start_day, end_day + dt.timedelta(days=1)
        )
        recovery = _rows(
            self.conn, "recovery",
            ("cycle_id", "created_at", "recovery_score", "hrv_rmssd", "resting_hr",
             "spo2", "skin_temp", "raw_json"),
            where="datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)",
            params=(utc_start, utc_end), order="datetime(created_at), cycle_id",
        )
        rec_chosen = {}
        for row in recovery:
            instant = TS.parse_instant(row.get("created_at"))
            if instant is None:
                continue
            day = instant.astimezone(TS.ANALYSIS_TZ).date()
            if not start_day <= day <= end_day:
                continue
            key = (instant, str(row.get("cycle_id") or ""))
            try:
                raw = json.loads(row.get("raw_json") or "{}")
            except (TypeError, ValueError):
                raw = {}
            value = {
                "recovery_score": row.get("recovery_score"),
                "hrv_rmssd": row.get("hrv_rmssd"),
                "resting_hr": row.get("resting_hr"),
                "spo2": row.get("spo2"),
                "skin_temp": row.get("skin_temp"),
                "recovery_provider_score": raw.get("score") or {},
            }
            if day not in rec_chosen or key > rec_chosen[day][0]:
                rec_chosen[day] = (key, value)

        sleep = _rows(
            self.conn, "sleep", ("id", "start", "end", "performance_pct",
                                 "efficiency_pct", "respiratory_rate", "raw_json"),
            where="datetime(end) >= datetime(?) AND datetime(end) < datetime(?)",
            params=(utc_start, utc_end), order="datetime(end), id",
        )
        sleep_chosen = {}
        for row in sleep:
            instant = TS.parse_instant(row.get("end"))
            if instant is None:
                continue
            day = instant.astimezone(TS.ANALYSIS_TZ).date()
            if not start_day <= day <= end_day:
                continue
            hours, nap = _sleep_values(row.get("raw_json"))
            if nap:
                continue
            try:
                raw = json.loads(row.get("raw_json") or "{}")
            except (TypeError, ValueError):
                raw = {}
            stages = (raw.get("score") or {}).get("stage_summary") or {}
            key = (instant, str(row.get("id") or ""))
            value = {"sleep_hours": hours, "sleep_performance": row.get("performance_pct"),
                     "sleep_efficiency": row.get("efficiency_pct"),
                     "respiratory_rate": row.get("respiratory_rate"),
                     "sleep_disturbances": stages.get("disturbance_count"),
                     "sleep_stages_ms": {
                         "awake": stages.get("total_awake_time_milli"),
                         "rem": stages.get("total_rem_sleep_time_milli"),
                         "light": stages.get("total_light_sleep_time_milli"),
                         "deep": stages.get("total_slow_wave_sleep_time_milli"),
                     },
                     "sleep_provider_score": raw.get("score") or {}}
            if day not in sleep_chosen or key > sleep_chosen[day][0]:
                sleep_chosen[day] = (key, value)
        return {
            day: {**rec_chosen.get(day, (None, {}))[1], **sleep_chosen.get(day, (None, {}))[1]}
            for day in sorted(set(rec_chosen) | set(sleep_chosen))
        }

    def whoop_sessions(self, start, end):
        start_day, end_day = TS.as_date(start), TS.as_date(end)
        utc_start, utc_end = TS.utc_bounds_for_analysis_dates(start_day, end_day + dt.timedelta(days=1))
        rows = _rows(
            self.conn, "workouts",
            ("id", "start", "end", "sport_name", "strain", "avg_hr", "max_hr",
             "kilojoule", "distance_meter", "raw_json"),
            where="datetime(start) >= datetime(?) AND datetime(start) < datetime(?)",
            params=(utc_start, utc_end), order="datetime(start), id",
        )
        out = []
        for row in rows:
            day = TS.analysis_date_for_instant(row.get("start"))
            if day is None or not start_day <= day <= end_day:
                continue
            start_instant, end_instant = TS.parse_instant(row.get("start")), TS.parse_instant(row.get("end"))
            duration = None
            if start_instant and end_instant:
                duration = round((end_instant - start_instant).total_seconds() / 60, 1)
            out.append({**row, "analysis_date": day.isoformat(), "duration_minutes": duration,
                        "source": "whoop", "link_status": "unlinked"})
        return out

    def _dashboard_bounds(self):
        candidates = []
        for table, column in (("workouts", "start"), ("recovery", "created_at"),
                              ("sleep", "end")):
            if column not in _table_columns(self.conn, table):
                continue
            for direction in ("ASC", "DESC"):
                row = self.conn.execute(
                    f"SELECT {column} FROM {table} "
                    f"WHERE {column} IS NOT NULL ORDER BY datetime({column}) {direction} LIMIT 1"
                ).fetchone()
                day = TS.analysis_date_for_instant(row[0]) if row else None
                if day is not None:
                    candidates.append(day)
        for table, column in (
            ("workout_exercises", "date"), ("cardio_exercises", "date"),
            ("supplements_log", "date"), ("daily_context_entries", "context_date"),
            ("daily_log", "date"),
        ):
            columns = _table_columns(self.conn, table)
            if column not in columns:
                continue
            active = " WHERE deleted_at IS NULL" if "deleted_at" in columns else ""
            row = self.conn.execute(
                f"SELECT MIN({column}), MAX({column}) FROM {table}{active}"
            ).fetchone()
            for value in row or ():
                try:
                    candidates.append(TS.as_date(value))
                except (TypeError, ValueError):
                    pass
        return (min(candidates), max(candidates)) if candidates else (None, None)

    def dashboard_whoop_facets(self, start, end):
        """WHOOP dashboard rows with one Europe/Kyiv calendar contract."""
        start_day, end_day = TS.as_date(start), TS.as_date(end)
        sessions = self.whoop_sessions(start_day, end_day)
        workouts = [{
            "id": row.get("id"), "date": row["analysis_date"],
            "start_local": (
                TS.parse_instant(row.get("start")).astimezone(TS.ANALYSIS_TZ).isoformat()
                if TS.parse_instant(row.get("start")) else None
            ),
            "sport": row.get("sport_name"), "strain": row.get("strain"),
            "avg_hr": row.get("avg_hr"), "max_hr": row.get("max_hr"),
            "kilojoule": row.get("kilojoule"), "distance_m": row.get("distance_meter"),
            "duration_min": row.get("duration_minutes"),
        } for row in sessions]

        utc_start, utc_end = TS.utc_bounds_for_analysis_dates(
            start_day, end_day + dt.timedelta(days=1)
        )
        recovery_rows = _rows(
            self.conn, "recovery",
            ("cycle_id", "created_at", "recovery_score", "resting_hr", "hrv_rmssd",
             "spo2", "skin_temp"),
            where="datetime(created_at) >= datetime(?) AND datetime(created_at) < datetime(?)",
            params=(utc_start, utc_end), order="datetime(created_at), cycle_id",
        )
        recovery_by_day = {}
        for row in recovery_rows:
            instant = TS.parse_instant(row.get("created_at"))
            if instant is None or row.get("recovery_score") is None:
                continue
            day = instant.astimezone(TS.ANALYSIS_TZ).date()
            if not start_day <= day <= end_day:
                continue
            key = (instant, str(row.get("cycle_id") or ""))
            value = {
                "date": day.isoformat(), "score": row.get("recovery_score"),
                "resting_hr": row.get("resting_hr"), "hrv": row.get("hrv_rmssd"),
                "spo2": row.get("spo2"), "skin_temp": row.get("skin_temp"),
            }
            if day not in recovery_by_day or key > recovery_by_day[day][0]:
                recovery_by_day[day] = (key, value)

        sleep_rows = _rows(
            self.conn, "sleep",
            ("id", "start", "end", "performance_pct", "efficiency_pct",
             "respiratory_rate", "raw_json"),
            where="datetime(end) >= datetime(?) AND datetime(end) < datetime(?)",
            params=(utc_start, utc_end), order="datetime(end), id",
        )
        sleep_by_day = {}
        for row in sleep_rows:
            end_instant = TS.parse_instant(row.get("end"))
            if end_instant is None:
                continue
            try:
                raw = json.loads(row.get("raw_json") or "{}")
            except (TypeError, ValueError):
                raw = {}
            if raw.get("nap"):
                continue
            day = end_instant.astimezone(TS.ANALYSIS_TZ).date()
            if not start_day <= day <= end_day:
                continue
            score = raw.get("score") or {}
            stages = score.get("stage_summary") or {}
            in_bed = stages.get("total_in_bed_time_milli")
            awake = stages.get("total_awake_time_milli")
            hours = None
            if isinstance(in_bed, (int, float)) and isinstance(awake, (int, float)) \
                    and in_bed >= awake:
                hours = round((in_bed - awake) / 3_600_000, 2)
            start_instant = TS.parse_instant(row.get("start"))
            key = (end_instant, str(row.get("id") or ""))
            value = {
                "wake_date": day.isoformat(), "hours": hours,
                "performance": row.get("performance_pct"),
                "efficiency": row.get("efficiency_pct"),
                "disturbances": stages.get("disturbance_count"),
                "resp_rate": row.get("respiratory_rate"),
                "stages_ms": {
                    "awake": stages.get("total_awake_time_milli", 0),
                    "rem": stages.get("total_rem_sleep_time_milli", 0),
                    "light": stages.get("total_light_sleep_time_milli", 0),
                    "deep": stages.get("total_slow_wave_sleep_time_milli", 0),
                },
                "bed_local": start_instant.astimezone(TS.ANALYSIS_TZ).isoformat()
                if start_instant else None,
                "wake_local": end_instant.astimezone(TS.ANALYSIS_TZ).isoformat(),
            }
            if day not in sleep_by_day or key > sleep_by_day[day][0]:
                sleep_by_day[day] = (key, value)
        return {
            **self.metadata(start_day, end_day),
            "workouts": workouts,
            "recovery": [recovery_by_day[day][1] for day in sorted(recovery_by_day)],
            "sleep": [sleep_by_day[day][1] for day in sorted(sleep_by_day)],
            "provenance": {"source": "WHOOP", "calendar_timezone": TS.ANALYSIS_TZ_NAME},
        }

    def dashboard_snapshot(self):
        """One SQLite snapshot for WHOOP physiology and every manual facet."""
        start, end = self._dashboard_bounds()
        if start is None:
            return None
        return {
            "snapshot": "dashboard_source.v1", "contract_version": CONTRACT_VERSION,
            "as_of": self.as_of, "analysis_timezone": TS.ANALYSIS_TZ_NAME,
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "whoop": self.dashboard_whoop_facets(start, end),
            "canonical_range": self.range_snapshot(start, end),
        }

    def manual_strength(self, start, end):
        start_iso, end_iso = TS.as_date(start).isoformat(), TS.as_date(end).isoformat()
        where = "date >= ? AND date <= ?"
        if "deleted_at" in _table_columns(self.conn, "workout_exercises"):
            where += " AND deleted_at IS NULL"
        rows = _rows(
            self.conn, "workout_exercises",
            ("id", "date", "exercise_name", "weight", "sets", "reps", "volume", "source_key"),
            where=where, params=(start_iso, end_iso), order="date, id",
        )
        out, known_keys, unknown_rows = [], set(), 0
        for row in rows:
            try:
                TS.as_date(row.get("date"))
            except (TypeError, ValueError):
                continue
            session_key = _source_event_key(row.get("source_key"), "workout")
            if session_key:
                known_keys.add(session_key)
            else:
                unknown_rows += 1
            sets = row.get("sets")
            set_count = int(sets) if isinstance(sets, (int, float)) and not isinstance(sets, bool) else None
            out.append({**row, "analysis_date": row.get("date"), "set_count": set_count,
                        "manual_session_key": session_key, "source": "manual",
                        "identity_quality": "source_event" if session_key else "unknown"})
        return out, {
            "known_session_count": len(known_keys),
            "unknown_identity_rows": unknown_rows,
            "session_count_complete": unknown_rows == 0,
        }

    def manual_cardio(self, start, end):
        start_iso, end_iso = TS.as_date(start).isoformat(), TS.as_date(end).isoformat()
        where = "date >= ? AND date <= ?"
        if "deleted_at" in _table_columns(self.conn, "cardio_exercises"):
            where += " AND deleted_at IS NULL"
        rows = _rows(
            self.conn, "cardio_exercises",
            ("id", "date", "time", "type", "duration", "distance", "avg_hr", "calories",
             "strain", "max_hr", "steps",
             "hr_zone_0_duration", "hr_zone_1_duration", "hr_zone_2_duration",
             "hr_zone_3_duration", "hr_zone_4_duration", "hr_zone_5_duration", "source_key"),
            where=where, params=(start_iso, end_iso), order="date, time, id",
        )
        return [{**row, "analysis_date": row.get("date"), "source": "manual",
                 "link_status": "unlinked", "time_semantics": "captured_or_unspecified"}
                for row in rows]

    def supplements(self, start, end):
        start_iso, end_iso = TS.as_date(start).isoformat(), TS.as_date(end).isoformat()
        where = "date >= ? AND date <= ?"
        if "deleted_at" in _table_columns(self.conn, "supplements_log"):
            where += " AND deleted_at IS NULL"
        rows = _rows(
            self.conn, "supplements_log",
            ("id", "date", "time", "name", "dosage", "taken", "source_key"),
            where=where, params=(start_iso, end_iso), order="date, time, id",
        )
        out = []
        for row in rows:
            try:
                canonical = normalize_factor_key("supplement", row.get("name"))
            except ReadModelInputError:
                canonical = None
            taken = row.get("taken")
            state = "taken" if taken == 1 else "not_taken" if taken == 0 else "unknown"
            dose = parse_mass_dose(row.get("dosage"))
            out.append({**row, "analysis_date": row.get("date"), "canonical_name": canonical,
                        "taken_state": state, "normalized_dose": dose, "source": "manual"})
        return out

    def contexts(self, start, end):
        start_iso, end_iso = TS.as_date(start).isoformat(), TS.as_date(end).isoformat()
        entry_columns = _table_columns(self.conn, "daily_context_entries")
        if {"context_date", "notes", "status"}.issubset(entry_columns):
            entries = _rows(
                self.conn, "daily_context_entries",
                ("entry_id", "context_date", "notes", "label", "source_key",
                 "origin_action_id", "revision", "status", "created_at", "updated_at"),
                where="context_date >= ? AND context_date <= ? AND status = 'active'",
                params=(start_iso, end_iso), order="context_date, revision, created_at, entry_id",
            )
            state_rows = _rows(
                self.conn, "daily_context_projection_state",
                ("context_date", "projection_hash", "revision", "updated_at"),
                where="context_date >= ? AND context_date <= ?", params=(start_iso, end_iso),
                order="context_date",
            )
            states = {row["context_date"]: row for row in state_rows}
            grouped = defaultdict(list)
            for row in entries:
                grouped[row["context_date"]].append(row)
            contexts = []
            for date_iso in sorted(grouped):
                rendered = []
                for row in grouped[date_iso]:
                    body = str(row.get("notes") or "").strip()
                    if body:
                        rendered.append(f"{row['label']}: {body}" if row.get("label") else body)
                state = states.get(date_iso, {})
                contexts.append({
                    "date": date_iso,
                    "notes": "\n\n".join(rendered) or None,
                    "updated_at": state.get("updated_at") or max(
                        (row.get("updated_at") for row in grouped[date_iso] if row.get("updated_at")),
                        default=None,
                    ),
                    "projection_hash": state.get("projection_hash"),
                    "projection_revision": state.get("revision"),
                    "entry_count": len(grouped[date_iso]),
                    "source": "daily_context_entries",
                })
            return contexts
        return [
            {**row, "source": "daily_log_legacy_projection"}
            for row in _rows(
                self.conn, "daily_log", ("date", "notes", "updated_at"),
                where="date >= ? AND date <= ?", params=(start_iso, end_iso), order="date",
            )
        ]

    def factor_rows(self, start, end):
        start_iso, end_iso = TS.as_date(start).isoformat(), TS.as_date(end).isoformat()
        columns = _table_columns(self.conn, "daily_factor_observations")
        where = "context_date >= ? AND context_date <= ?"
        if "is_current" in columns:
            where += " AND is_current = 1"
        rows = _rows(
            self.conn, "daily_factor_observations",
            ("observation_id", "context_date", "factor_key", "state", "confidence", "created_at",
             "updated_at", "job_id", "projection_hash", "projection_revision", "is_current"),
            where=where, params=(start_iso, end_iso),
            order="context_date, observation_id",
        )
        if "projection_hash" not in columns:
            return rows
        states = {
            row["context_date"]: row
            for row in _rows(
                self.conn, "daily_context_projection_state",
                ("context_date", "projection_hash", "revision"),
                where="context_date >= ? AND context_date <= ?", params=(start_iso, end_iso),
                order="context_date",
            )
        }
        current = []
        for row in rows:
            state = states.get(row.get("context_date"))
            # Projection-aware observations are valid only when the canonical
            # projection state exists and matches exactly.  A missing state is
            # not permission to accept an orphan observation.
            if state is None:
                continue
            if (
                row.get("projection_hash") != state.get("projection_hash")
                or row.get("projection_revision") != state.get("revision")
            ):
                continue
            current.append(row)
        return current

    def factor_completeness(self, start, end):
        start_iso, end_iso = TS.as_date(start).isoformat(), TS.as_date(end).isoformat()
        job_columns = _table_columns(self.conn, "factor_extraction_jobs")
        if not job_columns:
            return {
                "jobs_table_present": False,
                "status_counts": {},
                "current_projection_days": 0,
                "succeeded_current_projection_days": 0,
                "disabled_current_projection_days": 0,
                "incomplete_current_projection_days": 0,
            }
        jobs = _rows(
            self.conn, "factor_extraction_jobs",
            ("job_id", "context_date", "projection_hash", "projection_revision", "status",
             "attempt_count", "last_error_code", "updated_at"),
            where="context_date >= ? AND context_date <= ?", params=(start_iso, end_iso),
            order="context_date, projection_revision, created_at, job_id",
        )
        states = {
            row["context_date"]: row
            for row in _rows(
                self.conn, "daily_context_projection_state",
                ("context_date", "projection_hash", "revision"),
                where="context_date >= ? AND context_date <= ?", params=(start_iso, end_iso),
                order="context_date",
            )
        }
        status_counts = defaultdict(int)
        current_jobs = []
        for row in jobs:
            status_counts[str(row.get("status") or "unknown")] += 1
            state = states.get(row.get("context_date"))
            if state and row.get("projection_hash") == state.get("projection_hash") \
                    and row.get("projection_revision") == state.get("revision"):
                current_jobs.append(row)
        statuses_by_day = {
            day: {row.get("status") for row in current_jobs if row.get("context_date") == day}
            for day in states
        }
        succeeded_days = {day for day, statuses in statuses_by_day.items() if "succeeded" in statuses}
        disabled_days = {
            day for day, statuses in statuses_by_day.items()
            if statuses and statuses <= {"disabled"}
        }
        current_days = set(states)
        incomplete_days = current_days - succeeded_days - disabled_days
        return {
            "jobs_table_present": True,
            "status_counts": dict(sorted(status_counts.items())),
            "current_projection_days": len(current_days),
            "succeeded_current_projection_days": len(succeeded_days),
            "disabled_current_projection_days": len(disabled_days),
            "incomplete_current_projection_days": len(incomplete_days),
            "current_projection_job_status": {
                day: sorted(statuses_by_day[day])
                for day in sorted(current_days)
            },
        }

    def activity_snapshot(self, start, end):
        whoop = self.whoop_sessions(start, end)
        strength, identity = self.manual_strength(start, end)
        cardio = self.manual_cardio(start, end)
        set_values = [row["set_count"] for row in strength if row["set_count"] is not None]
        return {
            **self.metadata(start, end),
            "facets": {"whoop_sessions": whoop, "manual_strength": strength, "manual_cardio": cardio},
            "summary": {
                "whoop_session_count": len(whoop),
                "whoop_total_strain": round(sum(float(row.get("strain") or 0) for row in whoop), 2),
                "manual_strength_days": len({row.get("date") for row in strength}),
                "manual_strength_sets": sum(set_values),
                "manual_strength_volume": round(sum(float(row.get("volume") or 0) for row in strength), 2),
                "manual_cardio_sessions": len(cardio),
                "manual_cardio_minutes": round(sum(float(row.get("duration") or 0) for row in cardio), 2),
                "combined_physical_session_count": None,
            },
            "identity": {**identity, "whoop_manual_links_present": False,
                         "combined_total_available": False},
            "provenance": {"physiology": "workouts", "strength_detail": "workout_exercises",
                           "manual_cardio": "cardio_exercises"},
        }

    def supplement_snapshot(self, start, end):
        events = self.supplements(start, end)
        taken = [row for row in events if row["taken_state"] == "taken"]
        convertible = [row for row in taken if row["normalized_dose"]["parse_status"] == "converted"]
        return {
            **self.metadata(start, end), "events": events,
            "summary": {
                "event_count": len(events), "taken_count": len(taken),
                "not_taken_count": sum(row["taken_state"] == "not_taken" for row in events),
                "unknown_count": sum(row["taken_state"] == "unknown" for row in events),
                "convertible_taken_count": len(convertible),
                "taken_mass_grams": round(sum(row["normalized_dose"]["grams"] for row in convertible), 6),
            },
            "provenance": {"events": "supplements_log", "missing_is_absent": False},
            "completeness": {"dose_coverage": {"convertible": len(convertible), "taken": len(taken)}},
        }

    def range_snapshot(self, start, end):
        start_day, end_day = TS.as_date(start), TS.as_date(end)
        activity = self.activity_snapshot(start_day, end_day)
        supplements = self.supplement_snapshot(start_day, end_day)
        contexts = {row["date"]: row for row in self.contexts(start_day, end_day)}
        factor_rows = self.factor_rows(start_day, end_day)
        factors_by_day = defaultdict(list)
        for row in factor_rows:
            factors_by_day[row.get("context_date")].append(row)
        outcomes = self.outcomes(TS.action_to_outcome_date(start_day),
                                 TS.action_to_outcome_date(end_day))
        whoop_by_day, strength_by_day, cardio_by_day, supplement_by_day = (
            defaultdict(list) for _ in range(4)
        )
        for row in activity["facets"]["whoop_sessions"]:
            whoop_by_day[row["analysis_date"]].append(row)
        for row in activity["facets"]["manual_strength"]:
            strength_by_day[row["analysis_date"]].append(row)
        for row in activity["facets"]["manual_cardio"]:
            cardio_by_day[row["analysis_date"]].append(row)
        for row in supplements["events"]:
            supplement_by_day[row["analysis_date"]].append(row)
        days = []
        for day in TS.date_range(start_day, end_day):
            iso, outcome_day = day.isoformat(), TS.action_to_outcome_date(day)
            days.append({
                "action_date": iso, "outcome_date": outcome_day.isoformat(),
                "activities": {"whoop_sessions": whoop_by_day[iso],
                               "manual_strength": strength_by_day[iso],
                               "manual_cardio": cardio_by_day[iso]},
                "supplements": supplement_by_day[iso], "context": contexts.get(iso),
                "daily_factors": factors_by_day[iso], "next_morning": outcomes.get(outcome_day, {}),
            })
        return {
            **self.metadata(start_day, end_day), "snapshot": "canonical_range.v1", "days": days,
            "activity": activity, "supplements": supplements,
            "provenance": {
                "date_spine": "union_action_dates_with_explicit_D_plus_1_outcomes",
                "daily_context": (
                    "daily_context_entries"
                    if _table_columns(self.conn, "daily_context_entries")
                    else "daily_log_legacy_projection"
                ),
                "daily_factors": "current_projection_only",
            },
            "completeness": {
                "daily_factor_table_present": bool(_table_columns(self.conn, "daily_factor_observations")),
                "factor_extraction": self.factor_completeness(start_day, end_day),
            },
        }

    def metric_trend(self, metric, days, local_now):
        if metric not in METRICS:
            raise ReadModelInputError("metric is outside the static allowlist")
        if days not in {7, 14, 28, 56, 84}:
            raise ReadModelInputError("days must be one of 7, 14, 28, 56, 84")
        end = TS.as_analysis_datetime(local_now).date()
        start = end - dt.timedelta(days=days - 1)
        outcomes = self.outcomes(start, end)
        series = [{"date": day.isoformat(), "value": outcomes.get(day, {}).get(metric)}
                  for day in TS.date_range(start, end)]
        observed = [point["value"] for point in series if point["value"] is not None]
        return {
            "snapshot": "metric_trend.v1", "contract_version": CONTRACT_VERSION,
            "metric": metric, "window_days": days, "start_date": start.isoformat(),
            "end_date": end.isoformat(), "series": series,
            "coverage": {"observed_days": len(observed), "expected_days": days},
            "summary": {"mean": _mean(observed), "median": _median(observed),
                        "minimum": min(observed) if observed else None,
                        "maximum": max(observed) if observed else None},
            "provenance": {"outcomes": "recovery/sleep", "analysis_timezone": TS.ANALYSIS_TZ_NAME},
        }

    def _factor_states(self, factor_type, canonical, start, end):
        states = defaultdict(set)
        if factor_type == "supplement":
            for row in self.supplements(start, end):
                if row.get("canonical_name") != canonical:
                    continue
                value = row.get("taken")
                states[TS.as_date(row["date"])].add(1 if value == 1 else 0 if value == 0 else None)
        else:
            for row in self.factor_rows(start, end):
                if _DAILY_FACTOR_INDEX.get(_normalize(row.get("factor_key"))) != canonical:
                    continue
                confidence = row.get("confidence")
                value = row.get("state")
                if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or confidence < MIN_FACTOR_CONFIDENCE:
                    states[TS.as_date(row["context_date"])].add(None)
                else:
                    states[TS.as_date(row["context_date"])].add(1 if value == 1 else 0 if value == 0 else None)
        return states

    @staticmethod
    def _state(values):
        if not values:
            return "missing"
        if 1 in values and 0 in values:
            return "conflict"
        if 1 in values:
            return "present"
        if 0 in values:
            return "absent"
        return "unknown"

    def factor_observation(self, factor_type, factor_key, days, local_now):
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 90:
            raise ReadModelInputError("factor days must be 1..90")
        canonical = normalize_factor_key(factor_type, factor_key)
        end = TS.as_analysis_datetime(local_now).date()
        start = end - dt.timedelta(days=days - 1)
        raw = self._factor_states(factor_type, canonical, start, end)
        outcomes = self.outcomes(start + dt.timedelta(days=1), end + dt.timedelta(days=1))
        by_state = {name: [] for name in ("present", "absent", "unknown", "missing", "conflict")}
        for day in TS.date_range(start, end):
            by_state[self._state(raw.get(day, set()))].append(day)
        cohorts = {}
        for name in ("present", "absent"):
            metric_values = {}
            for metric in METRICS:
                values = [outcomes.get(day + dt.timedelta(days=1), {}).get(metric) for day in by_state[name]]
                values = [value for value in values if value is not None]
                metric_values[metric] = {"sample_size": len(values), "mean": _mean(values), "median": _median(values)}
            cohorts[name] = {"factor_days": len(by_state[name]), "outcomes": metric_values}
        metric_eligibility, deltas = {}, {}
        for metric in METRICS:
            p, a = cohorts["present"]["outcomes"][metric], cohorts["absent"]["outcomes"][metric]
            eligible = p["sample_size"] >= MIN_FACTOR_COHORT and a["sample_size"] >= MIN_FACTOR_COHORT
            metric_eligibility[metric] = {"eligible": eligible,
                                          "present_sample_size": p["sample_size"],
                                          "absent_sample_size": a["sample_size"]}
            deltas[metric] = round(p["mean"] - a["mean"], 2) if eligible and p["mean"] is not None and a["mean"] is not None else None
        eligible = metric_eligibility["recovery_score"]["eligible"]
        return {
            "snapshot": "factor_observation.v1", "contract_version": CONTRACT_VERSION,
            "factor_type": factor_type, "factor_key": canonical, "window_days": days,
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "day_states": {name: len(values) for name, values in by_state.items()},
            "cohorts": cohorts, "eligible": eligible, "metric_eligibility": metric_eligibility,
            "minimum_cohort": MIN_FACTOR_COHORT, "mean_delta_present_minus_absent": deltas,
            "semantics": f"observational_only; factor day D maps to {TS.ANALYSIS_TZ_NAME} outcome day D+1",
            "completeness": {"missing_is_absent": False,
                             "factor_table_present": bool(_table_columns(self.conn, "daily_factor_observations"))},
        }

    def weekly_snapshot(self, local_now, factors=DEFAULT_FACTORS):
        today = TS.as_analysis_datetime(local_now).date()
        this_monday = today - dt.timedelta(days=today.weekday())
        current_start, previous_start = this_monday - dt.timedelta(days=7), this_monday - dt.timedelta(days=14)

        def week(monday):
            end = monday + dt.timedelta(days=6)
            range_snap = self.range_snapshot(monday, end)
            outcomes = self.outcomes(monday + dt.timedelta(days=1), end + dt.timedelta(days=1))
            metrics = {}
            for metric in METRICS:
                values = [outcomes.get(monday + dt.timedelta(days=i + 1), {}).get(metric) for i in range(7)]
                values = [value for value in values if value is not None]
                metrics[metric] = {"mean": _mean(values), "sample_size": len(values)}
            activity = range_snap["activity"]["summary"]
            context_days = sum(
                bool((item.get("context") or {}).get("notes"))
                for item in range_snap["days"]
            )
            supplement_days = len({
                item["action_date"] for item in range_snap["days"]
                if item.get("supplements")
            })
            taken_supplement_days = len({
                item["action_date"] for item in range_snap["days"]
                if any(event.get("taken_state") == "taken" for event in item.get("supplements", []))
            })
            return {
                "action_week_start": monday.isoformat(), "action_week_end": end.isoformat(),
                "outcome_start": (monday + dt.timedelta(days=1)).isoformat(),
                "outcome_end": (end + dt.timedelta(days=1)).isoformat(),
                "metrics": metrics,
                "coverage": {metric: value["sample_size"] for metric, value in metrics.items()},
                "activity": {
                    "strength_days": activity["manual_strength_days"],
                    "strength_sets": activity["manual_strength_sets"],
                    "strength_volume": activity["manual_strength_volume"],
                    "cardio_sessions": activity["manual_cardio_sessions"],
                    "cardio_minutes": activity["manual_cardio_minutes"],
                },
                "strength_rows": [
                    {key: row.get(key) for key in
                     ("id", "date", "exercise_name", "weight", "sets", "reps")}
                    for day in range_snap["days"]
                    for row in (day.get("activities") or {}).get("manual_strength", [])
                ],
                "activity_facets": {
                    "whoop_sessions": activity["whoop_session_count"],
                    "whoop_total_strain": activity["whoop_total_strain"],
                    "manual_session_count_known": range_snap["activity"]["identity"]["known_session_count"],
                    "combined_physical_session_count": None,
                },
                "logged": {
                    "daily_context_coverage_days": context_days,
                    "supplement_days": supplement_days,
                    "taken_supplement_days": taken_supplement_days,
                },
                "provenance": range_snap["activity"]["provenance"],
                "completeness": range_snap["completeness"],
            }

        current, previous = week(current_start), week(previous_start)
        deltas = {}
        for metric in METRICS:
            cur, prev = current["metrics"][metric]["mean"], previous["metrics"][metric]["mean"]
            deltas[metric] = round(cur - prev, 2) if cur is not None and prev is not None else None
        observations = []
        for factor_type, factor_key in factors or ():
            observation = self.factor_observation(factor_type, factor_key, 84, local_now)
            if observation["eligible"]:
                observations.append(observation)
        return {
            "snapshot": "weekly_evidence.v2", "contract_version": CONTRACT_VERSION,
            "period": "last_completed_week", "current_week": current, "previous_week": previous,
            "mean_delta_current_minus_previous": deltas, "factor_observations": observations,
            "provenance": {"read_model": CONTRACT_VERSION, "analysis_timezone": TS.ANALYSIS_TZ_NAME},
            "completeness": {
                "pagination": {"truncated": False, "has_more": False},
                "current_week_factor_extraction": current["completeness"]["factor_extraction"],
                "previous_week_factor_extraction": previous["completeness"]["factor_extraction"],
            },
        }


def range_snapshot(conn, start, end):
    with snapshot_transaction(conn) as model:
        return model.range_snapshot(start, end)


def metric_trend(conn, metric, days, local_now):
    with snapshot_transaction(conn) as model:
        return model.metric_trend(metric, days, local_now)


def factor_observation(conn, factor_type, factor_key, days, local_now):
    with snapshot_transaction(conn) as model:
        return model.factor_observation(factor_type, factor_key, days, local_now)


def weekly_snapshot(conn, local_now, factors=DEFAULT_FACTORS):
    with snapshot_transaction(conn) as model:
        return model.weekly_snapshot(local_now, factors=factors)
