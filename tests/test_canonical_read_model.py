import datetime as dt
import sqlite3
import unittest

import canonical_read_model as crm
import conversation_evidence
import conversation_read_models
import weekly_analysis_v2


def database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE recovery(cycle_id TEXT, created_at TEXT, recovery_score REAL,
          hrv_rmssd REAL, resting_hr REAL);
        CREATE TABLE sleep(id TEXT, end TEXT, performance_pct REAL,
          respiratory_rate REAL, raw_json TEXT);
        CREATE TABLE workouts(id TEXT, start TEXT, end TEXT, sport_name TEXT,
          strain REAL, avg_hr REAL, max_hr REAL, kilojoule REAL, distance_meter REAL,
          raw_json TEXT);
        CREATE TABLE workout_exercises(id INTEGER, date TEXT, exercise_name TEXT,
          weight REAL, sets INTEGER, reps INTEGER, volume REAL, source_key TEXT,
          deleted_at TEXT);
        CREATE TABLE cardio_exercises(id INTEGER, date TEXT, time TEXT, type TEXT,
          duration REAL, distance REAL, avg_hr REAL, calories REAL, source_key TEXT,
          deleted_at TEXT);
        CREATE TABLE supplements_log(id INTEGER, date TEXT, time TEXT, name TEXT,
          dosage TEXT, taken INTEGER, source_key TEXT, deleted_at TEXT);
        CREATE TABLE daily_log(date TEXT PRIMARY KEY, notes TEXT, updated_at TEXT);
        CREATE TABLE daily_context_entries(entry_id TEXT, context_date TEXT, notes TEXT,
          label TEXT, source_key TEXT, origin_action_id TEXT, revision INTEGER,
          status TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE daily_context_projection_state(context_date TEXT,
          projection_hash TEXT, revision INTEGER, updated_at TEXT);
        CREATE TABLE daily_factor_observations(observation_id TEXT, context_date TEXT,
          factor_key TEXT, state INTEGER, confidence REAL, created_at TEXT,
          updated_at TEXT, job_id TEXT, projection_hash TEXT,
          projection_revision INTEGER, is_current INTEGER);
        CREATE TABLE factor_extraction_jobs(job_id TEXT, context_date TEXT,
          projection_hash TEXT, projection_revision INTEGER, status TEXT,
          attempt_count INTEGER, last_error_code TEXT, updated_at TEXT,
          created_at TEXT);
        """
    )
    return conn


class CanonicalReadModelTests(unittest.TestCase):
    def setUp(self):
        self.conn = database()

    def tearDown(self):
        self.conn.close()

    def test_facets_sets_taken_dose_and_soft_delete_contract(self):
        self.conn.execute("INSERT INTO workouts VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "w", "2026-07-06T07:00:00Z", "2026-07-06T08:00:00Z", "Weightlifting",
            8, 120, 160, 100, 0, "{}"))
        self.conn.executemany("INSERT INTO workout_exercises VALUES (?,?,?,?,?,?,?,?,?)", [
            (1, "2026-07-06", "Squat", 100, 3, 5, 1500, "a:workout:0", None),
            (2, "2026-07-06", "Deleted", 100, 9, 5, 4500, "b:workout:0", "x"),
        ])
        self.conn.executemany("INSERT INTO supplements_log VALUES (?,?,?,?,?,?,?,?)", [
            (1, "2026-07-06", None, "creatine", "5 g", 1, "s1", None),
            (2, "2026-07-06", None, "creatine", "5000 mg", 1, "s2", None),
            (3, "2026-07-06", None, "creatine", "5", 0, "s3", None),
            (4, "2026-07-06", None, "creatine", "5 g", 1, "s4", "x"),
        ])
        snap = crm.range_snapshot(self.conn, "2026-07-06", "2026-07-06")
        self.assertEqual(snap["activity"]["summary"]["manual_strength_sets"], 3)
        self.assertEqual(snap["activity"]["summary"]["whoop_session_count"], 1)
        self.assertIsNone(snap["activity"]["summary"]["combined_physical_session_count"])
        self.assertEqual(snap["supplements"]["summary"]["taken_count"], 2)
        self.assertEqual(snap["supplements"]["summary"]["taken_mass_grams"], 10)
        self.assertEqual(snap["supplements"]["events"][2]["normalized_dose"]["parse_status"], "unsupported")
        self.assertFalse(snap["pagination"]["truncated"])

    def test_dose_parser_requires_the_entire_value_and_explicit_unit(self):
        self.assertEqual(crm.parse_mass_dose("5 g")["grams"], 5)
        self.assertEqual(crm.parse_mass_dose("5000мг")["grams"], 5)
        self.assertEqual(crm.parse_mass_dose("dose 5 mg")["parse_status"], "unsupported")
        self.assertEqual(crm.parse_mass_dose("5 mg daily")["parse_status"], "unsupported")
        self.assertEqual(crm.parse_mass_dose("5")["parse_status"], "unsupported")

    def test_dashboard_bounds_include_manual_days_outside_whoop_coverage(self):
        self.conn.execute("INSERT INTO workouts VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "w", "2026-07-07T07:00:00Z", "2026-07-07T08:00:00Z", "Run",
            5, 120, 150, 100, 1000, "{}",
        ))
        self.conn.execute(
            "INSERT INTO workout_exercises VALUES (?,?,?,?,?,?,?,?,?)",
            (1, "2026-07-08", "Squat", 100, 3, 5, 1500, "manual:1", None),
        )
        with crm.snapshot_transaction(self.conn) as model:
            snapshot = model.dashboard_snapshot()
        self.assertEqual(snapshot["period"]["end"], "2026-07-08")
        self.assertEqual(
            len(snapshot["canonical_range"]["activity"]["facets"]["manual_strength"]), 1,
        )

    def test_unbounded_history_reports_the_natural_start(self):
        self.conn.executemany(
            "INSERT INTO daily_log VALUES (?,?,?)",
            [("2020-01-01", "old", "x"), ("2026-07-08", "new", "x")],
        )
        model = crm.CanonicalReadModel(self.conn)
        self.assertEqual(
            model.available_action_bounds(max_days=None),
            (dt.date(2020, 1, 1), dt.date(2026, 7, 8)),
        )

    def test_union_spine_d_plus_one_and_immutable_context_projection(self):
        self.conn.execute("INSERT INTO recovery VALUES (?,?,?,?,?)",
                          ("r", "2026-07-08T05:00:00Z", 77, 55, 49))
        self.conn.execute("INSERT INTO daily_log VALUES (?,?,?)",
                          ("2026-07-07", "stale projection", "x"))
        self.conn.executemany("INSERT INTO daily_context_entries VALUES (?,?,?,?,?,?,?,?,?,?)", [
            ("e1", "2026-07-07", "calm", "Stress", "a", None, 1, "active", "1", "1"),
            ("e2", "2026-07-07", "old", None, "b", None, 2, "retracted", "2", "2"),
        ])
        self.conn.execute("INSERT INTO daily_context_projection_state VALUES (?,?,?,?)",
                          ("2026-07-07", "h", 2, "2"))
        snap = crm.range_snapshot(self.conn, "2026-07-07", "2026-07-07")
        day = snap["days"][0]
        self.assertEqual(day["context"]["notes"], "Stress: calm")
        self.assertEqual(day["next_morning"]["recovery_score"], 77)
        self.assertEqual(day["outcome_date"], "2026-07-08")

    def test_full_bounded_read_has_no_silent_row_limit(self):
        self.conn.executemany(
            "INSERT INTO workout_exercises VALUES (?,?,?,?,?,?,?,?,?)",
            [(i, "2026-07-06", "Squat", 1, 1, 1, 1, f"a:workout:{i}", None)
             for i in range(650)],
        )
        snap = crm.range_snapshot(self.conn, "2026-07-06", "2026-07-06")
        self.assertEqual(len(snap["activity"]["facets"]["manual_strength"]), 650)
        self.assertEqual(snap["activity"]["summary"]["manual_strength_sets"], 650)
        self.assertFalse(snap["activity"]["pagination"]["has_more"])

    def test_only_current_factor_projection_and_job_completeness(self):
        self.conn.execute("INSERT INTO daily_context_projection_state VALUES (?,?,?,?)",
                          ("2026-07-07", "new", 2, "2"))
        self.conn.executemany(
            "INSERT INTO daily_factor_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
                ("old", "2026-07-07", "alcohol", 1, .99, "1", "1", "j1", "old", 1, 0),
                ("new", "2026-07-07", "alcohol", 0, .99, "2", "2", "j2", "new", 2, 1),
            ])
        self.conn.execute("INSERT INTO factor_extraction_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                          ("j2", "2026-07-07", "new", 2, "succeeded", 1, None, "2", "2"))
        snap = crm.range_snapshot(self.conn, "2026-07-07", "2026-07-07")
        self.assertEqual([r["observation_id"] for r in snap["days"][0]["daily_factors"]], ["new"])
        complete = snap["completeness"]["factor_extraction"]
        self.assertEqual(complete["succeeded_current_projection_days"], 1)
        self.assertEqual(complete["incomplete_current_projection_days"], 0)

    def test_orphan_projection_observation_is_rejected(self):
        self.conn.execute(
            "INSERT INTO daily_factor_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("orphan", "2026-07-07", "alcohol", 1, .99, "1", "1", "j", "missing", 1, 1),
        )
        snap = crm.range_snapshot(self.conn, "2026-07-07", "2026-07-07")
        self.assertEqual(snap["days"][0]["daily_factors"], [])

    def test_disabled_factor_job_is_complete_and_reaches_weekly_conversation(self):
        self.conn.execute("INSERT INTO daily_context_projection_state VALUES (?,?,?,?)",
                          ("2026-07-13", "h", 1, "1"))
        self.conn.execute("INSERT INTO factor_extraction_jobs VALUES (?,?,?,?,?,?,?,?,?)",
                          ("j", "2026-07-13", "h", 1, "disabled", 0, None, "1", "1"))
        now = dt.datetime(2026, 7, 20, 12, tzinfo=crm.TS.ANALYSIS_TZ)
        weekly = conversation_evidence.weekly_evidence(self.conn, now, factors=())
        completeness = weekly["completeness"]["current_week_factor_extraction"]
        self.assertEqual(completeness["disabled_current_projection_days"], 1)
        self.assertEqual(completeness["incomplete_current_projection_days"], 0)
        conversation = conversation_read_models.week_summary(self.conn, now)
        self.assertEqual(conversation["completeness"], weekly["completeness"])

    def test_dashboard_source_uses_one_canonical_kyiv_snapshot(self):
        self.conn.execute("INSERT INTO workouts VALUES (?,?,?,?,?,?,?,?,?,?)", (
            "w", "2026-07-06T22:30:00Z", "2026-07-06T23:30:00Z", "Run",
            7, 120, 150, 50, 1000, "{}"))
        self.conn.execute("INSERT INTO workout_exercises VALUES (?,?,?,?,?,?,?,?,?)",
                          (1, "2026-07-07", "Squat", 100, 2, 5, 1000, "a", None))
        with crm.snapshot_transaction(self.conn) as model:
            source = model.dashboard_snapshot()
            self.assertTrue(self.conn.in_transaction)
        self.assertEqual(source["analysis_timezone"], "Europe/Kyiv")
        self.assertEqual(source["whoop"]["workouts"][0]["date"], "2026-07-07")
        self.assertEqual(
            source["canonical_range"]["activity"]["summary"]["manual_strength_sets"], 2
        )
        self.assertEqual(source["as_of"], source["canonical_range"]["as_of"])

    def test_weekly_consumers_share_exact_snapshot(self):
        now = dt.datetime(2026, 7, 20, 12, tzinfo=crm.TS.ANALYSIS_TZ)
        direct = conversation_evidence.weekly_evidence(self.conn, now)
        weekly = weekly_analysis_v2.build_snapshot(self.conn, dt.date(2026, 7, 19))
        self.assertEqual(direct, weekly)
        legacy = conversation_read_models.week_summary(self.conn, now)
        self.assertEqual(legacy["current_week"]["strength_sets"],
                         direct["current_week"]["activity"]["strength_sets"])

    def test_read_transaction_does_not_change_schema(self):
        before = self.conn.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        crm.range_snapshot(self.conn, "2026-07-06", "2026-07-07")
        after = self.conn.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
