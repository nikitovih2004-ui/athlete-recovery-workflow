import datetime as dt
import hashlib
import json
import sqlite3
import unittest
from zoneinfo import ZoneInfo

import conversation_evidence as evidence


KYIV = ZoneInfo("Europe/Kyiv")


def _sleep_raw(hours, nap=False):
    return json.dumps({
        "nap": nap,
        "score": {"stage_summary": {
            "total_in_bed_time_milli": int(hours * 3_600_000),
            "total_awake_time_milli": 0,
        }},
    })


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE recovery (
                cycle_id INTEGER PRIMARY KEY, created_at TEXT,
                recovery_score REAL, hrv_rmssd REAL, resting_hr REAL
            );
            CREATE TABLE sleep (
                id TEXT PRIMARY KEY, end TEXT, performance_pct REAL, raw_json TEXT
            );
            CREATE TABLE workout_exercises (date TEXT, sets INTEGER, volume REAL);
            CREATE TABLE cardio_exercises (date TEXT, duration REAL);
            CREATE TABLE supplements_log (id INTEGER PRIMARY KEY, date TEXT, name TEXT, taken INTEGER);
            CREATE TABLE daily_factor_observations (
                observation_id TEXT PRIMARY KEY, context_date TEXT, factor_key TEXT,
                state INTEGER, confidence REAL, created_at TEXT
            );
        """)

    def tearDown(self):
        self.conn.close()

    def _recovery(self, cycle_id, local_day, score, hrv=None, rhr=None, hour=8):
        instant = dt.datetime.combine(local_day, dt.time(hour), tzinfo=KYIV).astimezone(dt.timezone.utc)
        self.conn.execute(
            "INSERT INTO recovery VALUES (?,?,?,?,?)",
            (cycle_id, instant.isoformat(), score, hrv, rhr),
        )

    def _sleep(self, row_id, local_day, hours, performance, hour=7, nap=False):
        instant = dt.datetime.combine(local_day, dt.time(hour), tzinfo=KYIV).astimezone(dt.timezone.utc)
        self.conn.execute(
            "INSERT INTO sleep VALUES (?,?,?,?)",
            (row_id, instant.isoformat(), performance, _sleep_raw(hours, nap)),
        )

    def _schema_hash(self):
        rows = self.conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        return hashlib.sha256(repr(rows).encode()).hexdigest()

    def test_metric_trend_exact_values_and_duplicate_selection(self):
        today = dt.date(2026, 7, 12)
        self._recovery(1, today - dt.timedelta(days=1), 40, 30, 60, hour=7)
        self._recovery(2, today - dt.timedelta(days=1), 80, 50, 55, hour=9)
        self._recovery(3, today, 70, 45, 57)
        snap = evidence.metric_trend(
            self.conn, "recovery_score", 7,
            dt.datetime(2026, 7, 12, 20, tzinfo=KYIV),
        )
        self.assertEqual(snap["coverage"], {"observed_days": 2, "expected_days": 7})
        self.assertEqual(snap["series"][-2:], [
            {"date": "2026-07-11", "value": 80.0},
            {"date": "2026-07-12", "value": 70.0},
        ])
        self.assertEqual(snap["summary"]["mean"], 75.0)

    def test_metric_trend_is_dst_safe_and_uses_kyiv_dates(self):
        # Kyiv changes from UTC+2 to UTC+3 on 2026-03-29.
        self.conn.execute(
            "INSERT INTO recovery VALUES (1, '2026-03-28T22:30:00+00:00', 61, 40, 58)"
        )
        self.conn.execute(
            "INSERT INTO recovery VALUES (2, '2026-03-29T21:30:00+00:00', 71, 44, 56)"
        )
        snap = evidence.metric_trend(
            self.conn, "recovery_score", 7,
            dt.datetime(2026, 3, 30, 12, tzinfo=KYIV),
        )
        observed = [point for point in snap["series"] if point["value"] is not None]
        self.assertEqual(observed, [
            {"date": "2026-03-29", "value": 61.0},
            {"date": "2026-03-30", "value": 71.0},
        ])

    def test_sleep_deduplicates_deterministically_and_ignores_naps(self):
        day = dt.date(2026, 7, 12)
        self._sleep("main-old", day, 7, 80, hour=6)
        self._sleep("nap", day, 1, 20, hour=8, nap=True)
        self._sleep("main-new", day, 8, 90, hour=9)
        now = dt.datetime(2026, 7, 12, 12, tzinfo=KYIV)
        hours = evidence.metric_trend(self.conn, "sleep_hours", 7, now)
        performance = evidence.metric_trend(self.conn, "sleep_performance", 7, now)
        self.assertEqual(hours["series"][-1]["value"], 8.0)
        self.assertEqual(performance["series"][-1]["value"], 90.0)

    def test_supplement_aliases_day_dedup_and_all_missingness_states(self):
        start = dt.date(2026, 4, 14)
        now = dt.datetime(2026, 7, 12, 12, tzinfo=KYIV)
        # Five present and five absent days have paired next-morning recovery.
        for i in range(5):
            p_day = start + dt.timedelta(days=i)
            a_day = start + dt.timedelta(days=10 + i)
            self.conn.execute("INSERT INTO supplements_log(date,name,taken) VALUES (?,?,1)", (p_day.isoformat(), "Магний"))
            self.conn.execute("INSERT INTO supplements_log(date,name,taken) VALUES (?,?,1)", (p_day.isoformat(), "MAGNESIUM"))
            self.conn.execute("INSERT INTO supplements_log(date,name,taken) VALUES (?,?,0)", (a_day.isoformat(), "magnesium"))
            self._recovery(100 + i, p_day + dt.timedelta(days=1), 80 + i)
            self._recovery(200 + i, a_day + dt.timedelta(days=1), 60 + i)
        unknown_day = start + dt.timedelta(days=20)
        conflict_day = start + dt.timedelta(days=21)
        self.conn.execute("INSERT INTO supplements_log(date,name,taken) VALUES (?,?,NULL)", (unknown_day.isoformat(), "магния"))
        self.conn.execute("INSERT INTO supplements_log(date,name,taken) VALUES (?,?,1)", (conflict_day.isoformat(), "магний"))
        self.conn.execute("INSERT INTO supplements_log(date,name,taken) VALUES (?,?,0)", (conflict_day.isoformat(), "magnesium"))

        snap = evidence.factor_observation(self.conn, "supplement", "магний", 90, now)
        self.assertTrue(snap["eligible"])
        self.assertEqual(snap["day_states"], {
            "present": 5, "absent": 5, "unknown": 1, "missing": 78, "conflict": 1,
        })
        self.assertEqual(snap["cohorts"]["present"]["factor_days"], 5)
        self.assertEqual(snap["mean_delta_present_minus_absent"]["recovery_score"], 20.0)

    def test_minimum_cohort_is_five_paired_outcomes_per_group(self):
        start = dt.date(2026, 7, 1)
        for i in range(5):
            self.conn.execute("INSERT INTO daily_factor_observations VALUES (?,?,?,?,?,?)", (str(i + 1), (start + dt.timedelta(days=i)).isoformat(), "alcohol", 1, 0.95, "t"))
            self.conn.execute("INSERT INTO daily_factor_observations VALUES (?,?,?,?,?,?)", (str(i + 11), (start + dt.timedelta(days=i + 5)).isoformat(), "алкоголь", 0, 0.95, "t"))
            self._recovery(i + 1, start + dt.timedelta(days=i + 1), 50)
            if i < 4:
                self._recovery(i + 20, start + dt.timedelta(days=i + 6), 70)
        snap = evidence.factor_observation(
            self.conn, "daily_factor", "алкоголь", 14,
            dt.datetime(2026, 7, 14, 12, tzinfo=KYIV),
        )
        self.assertFalse(snap["eligible"])
        self.assertIsNone(snap["mean_delta_present_minus_absent"]["recovery_score"])

    def test_unknown_supplement_names_use_the_same_canonical_key_when_read(self):
        start = dt.date(2026, 7, 1)
        for i in range(5):
            for state, offset, score in ((1, i, 80), (0, i + 5, 60)):
                day = start + dt.timedelta(days=offset)
                self.conn.execute(
                    "INSERT INTO supplements_log(date,name,taken) VALUES (?,?,?)",
                    (day.isoformat(), "Mystery Supplement", state),
                )
                self._recovery(300 + offset, day + dt.timedelta(days=1), score)

        snap = evidence.factor_observation(
            self.conn, "supplement", "mystery_supplement", 14,
            dt.datetime(2026, 7, 14, 12, tzinfo=KYIV),
        )

        self.assertEqual(snap["factor_key"], "mystery_supplement")
        self.assertTrue(snap["eligible"])
        self.assertEqual(snap["day_states"]["present"], 5)
        self.assertEqual(snap["day_states"]["absent"], 5)

    def test_each_metric_requires_its_own_five_by_five_paired_outcomes(self):
        start = dt.date(2026, 7, 1)
        for i in range(5):
            present_day = start + dt.timedelta(days=i)
            absent_day = start + dt.timedelta(days=i + 5)
            self.conn.execute(
                "INSERT INTO supplements_log(date,name,taken) VALUES (?,?,1)",
                (present_day.isoformat(), "creatine"),
            )
            self.conn.execute(
                "INSERT INTO supplements_log(date,name,taken) VALUES (?,?,0)",
                (absent_day.isoformat(), "creatine"),
            )
            self._recovery(400 + i, present_day + dt.timedelta(days=1), 80, 50)
            self._recovery(
                500 + i, absent_day + dt.timedelta(days=1), 60,
                40 if i < 4 else None,
            )

        snap = evidence.factor_observation(
            self.conn, "supplement", "creatine", 14,
            dt.datetime(2026, 7, 14, 12, tzinfo=KYIV),
        )

        self.assertTrue(snap["eligible"])
        self.assertEqual(
            snap["metric_eligibility"]["recovery_score"],
            {"eligible": True, "present_sample_size": 5, "absent_sample_size": 5},
        )
        self.assertEqual(
            snap["metric_eligibility"]["hrv_rmssd"],
            {"eligible": False, "present_sample_size": 5, "absent_sample_size": 4},
        )
        self.assertEqual(snap["mean_delta_present_minus_absent"]["recovery_score"], 20.0)
        self.assertIsNone(snap["mean_delta_present_minus_absent"]["hrv_rmssd"])

    def test_low_or_null_daily_factor_confidence_is_unknown(self):
        start = dt.date(2026, 7, 1)
        for i, confidence in enumerate((0.84, None, 0.85)):
            day = start + dt.timedelta(days=i)
            self.conn.execute(
                "INSERT INTO daily_factor_observations VALUES (?,?,?,?,?,?)",
                (f"low-{i}", day.isoformat(), "alcohol", 1, confidence, "t"),
            )
            self._recovery(600 + i, day + dt.timedelta(days=1), 80)

        snap = evidence.factor_observation(
            self.conn, "daily_factor", "alcohol", 7,
            dt.datetime(2026, 7, 7, 12, tzinfo=KYIV),
        )

        self.assertEqual(snap["day_states"]["unknown"], 2)
        self.assertEqual(snap["day_states"]["present"], 1)
        self.assertEqual(snap["cohorts"]["present"]["factor_days"], 1)

    def test_weekly_evidence_uses_action_week_and_next_day_outcomes(self):
        # 2026-07-13 is Monday: current completed action week is Jul 6..12.
        current = dt.date(2026, 7, 6)
        previous = dt.date(2026, 6, 29)
        cycle = 1
        for monday, base in ((previous, 50), (current, 70)):
            for i in range(7):
                outcome_day = monday + dt.timedelta(days=i + 1)
                self._recovery(cycle, outcome_day, base + i, 40 + i, 60 - i)
                self._sleep(f"s{cycle}", outcome_day, 7 + i / 10, 80 + i)
                cycle += 1
        self.conn.executemany(
            "INSERT INTO workout_exercises VALUES (?,?,?)",
            [("2026-07-06", 4, 1000), ("2026-07-06", 3, 800), ("2026-07-08", 5, 1200)],
        )
        self.conn.executemany(
            "INSERT INTO cardio_exercises VALUES (?,?)",
            [("2026-07-07", 30), ("2026-07-12", 45)],
        )
        snap = evidence.weekly_evidence(
            self.conn, dt.datetime(2026, 7, 13, 0, 30, tzinfo=KYIV)
        )
        self.assertEqual(snap["current_week"]["action_week_start"], "2026-07-06")
        self.assertEqual(snap["current_week"]["outcome_end"], "2026-07-13")
        self.assertEqual(snap["current_week"]["metrics"]["recovery_score"], {"mean": 73.0, "sample_size": 7})
        self.assertEqual(snap["mean_delta_current_minus_previous"]["recovery_score"], 20.0)
        self.assertEqual(snap["current_week"]["activity"], {
            "strength_days": 2, "strength_sets": 12, "strength_volume": 3000.0,
            "cardio_sessions": 2, "cardio_minutes": 75.0,
        })

    def test_weekly_includes_only_eligible_factor_observations(self):
        # Jul 13 minus 83 days: first day inside the fixed 84-day window.
        start = dt.date(2026, 4, 21)
        for i in range(5):
            for state, offset, score in ((1, i, 80), (0, i + 10, 60)):
                day = start + dt.timedelta(days=offset)
                self.conn.execute("INSERT INTO supplements_log(date,name,taken) VALUES (?,?,?)", (day.isoformat(), "creatine", state))
                self._recovery(500 + i + state * 20, day + dt.timedelta(days=1), score)
        snap = evidence.weekly_evidence(
            self.conn, dt.datetime(2026, 7, 13, 12, tzinfo=KYIV),
            factors=[("supplement", "creatine"), ("daily_factor", "high stress")],
        )
        self.assertEqual([item["factor_key"] for item in snap["factor_observations"]], ["creatine"])

    def test_invalid_allowlist_values_fail_closed(self):
        now = dt.datetime(2026, 7, 12, tzinfo=KYIV)
        with self.assertRaises(evidence.EvidenceInputError):
            evidence.metric_trend(self.conn, "raw_json", 7, now)
        with self.assertRaises(evidence.EvidenceInputError):
            evidence.metric_trend(self.conn, "recovery_score", 90, now)
        unknown = evidence.factor_observation(
            self.conn, "supplement", "Mystery Supplement", 30, now
        )
        self.assertEqual(unknown["factor_key"], "mystery_supplement")
        with self.assertRaises(evidence.EvidenceInputError):
            evidence.factor_observation(self.conn, "daily_factor", "mystery", 30, now)
        with self.assertRaises(evidence.EvidenceInputError):
            evidence.factor_observation(self.conn, "supplement", "magnesium", 91, now)

    def test_all_snapshots_leave_schema_unchanged(self):
        before = self._schema_hash()
        now = dt.datetime(2026, 7, 13, tzinfo=KYIV)
        evidence.metric_trend(self.conn, "hrv_rmssd", 28, now)
        evidence.factor_observation(self.conn, "supplement", "magnesium", 30, now)
        evidence.weekly_evidence(self.conn, now)
        after = self._schema_hash()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
