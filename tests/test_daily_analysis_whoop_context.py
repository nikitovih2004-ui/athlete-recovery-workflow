import json
import sqlite3
import unittest

import canonical_read_model as crm
import generate_insights as gi


class DailyAnalysisWhoopContextTests(unittest.TestCase):
    def test_context_carries_normalized_categories_and_provider_scores(self):
        day = {
            "action_date": "2026-08-07", "outcome_date": "2026-08-08",
            "activities": {"whoop_sessions": [{
                "sport_name": "Running", "start": "start", "end": "end",
                "duration_minutes": 42, "strain": 12.3, "avg_hr": 145,
                "max_hr": 177, "kilojoule": 1800, "distance_meter": 9000,
                "raw_json": json.dumps({"id": "not-for-analysis", "score": {
                    "zone_duration": {"zone_one_milli": 1000},
                    "altitude_gain_meter": 12,
                }}),
            }]},
            "next_morning": {
                "recovery_score": 78, "hrv_rmssd": 61, "resting_hr": 49,
                "spo2": 97.5, "skin_temp": 33.2, "sleep_hours": 7.4,
                "sleep_performance": 91, "sleep_efficiency": 89.5,
                "respiratory_rate": 14.1, "sleep_disturbances": 5,
                "sleep_stages_ms": {"rem": 1, "deep": 2},
                "recovery_provider_score": {"user_calibrating": False},
                "sleep_provider_score": {"sleep_consistency_percentage": 82},
            },
        }

        context = gi.whoop_stored_context(day)

        self.assertEqual(context["workouts"][0]["strain"], 12.3)
        self.assertIn("zone_duration", context["workouts"][0]["provider_score"])
        self.assertNotIn("id", context["workouts"][0])
        outcome = context["next_morning_recovery_and_sleep"]
        self.assertEqual(outcome["spo2"], 97.5)
        self.assertEqual(outcome["sleep_stages_ms"]["deep"], 2)
        self.assertEqual(outcome["sleep_provider_score"]["sleep_consistency_percentage"], 82)




class CanonicalOutcomeContextTests(unittest.TestCase):
    def test_outcomes_surface_recovery_and_sleep_categories(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE recovery(cycle_id TEXT, created_at TEXT, recovery_score REAL,
              hrv_rmssd REAL, resting_hr REAL, spo2 REAL, skin_temp REAL, raw_json TEXT);
            CREATE TABLE sleep(id TEXT, start TEXT, end TEXT, performance_pct REAL,
              efficiency_pct REAL, respiratory_rate REAL, raw_json TEXT);
        """)
        conn.execute("INSERT INTO recovery VALUES (?,?,?,?,?,?,?,?)", (
            "c", "2026-08-08T05:00:00Z", 78, 61, 49, 97.5, 33.2,
            json.dumps({"score": {"user_calibrating": False}}),
        ))
        conn.execute("INSERT INTO sleep VALUES (?,?,?,?,?,?,?)", (
            "s", "2026-08-07T21:00:00Z", "2026-08-08T05:00:00Z", 91, 89.5, 14.1,
            json.dumps({"nap": False, "score": {"stage_summary": {
                "total_in_bed_time_milli": 28_800_000,
                "total_awake_time_milli": 1_800_000,
                "total_rem_sleep_time_milli": 6_000_000,
                "total_light_sleep_time_milli": 14_000_000,
                "total_slow_wave_sleep_time_milli": 7_000_000,
                "disturbance_count": 5,
            }, "sleep_consistency_percentage": 82}}),
        ))

        outcome = crm.CanonicalReadModel(conn).outcomes(
            "2026-08-08", "2026-08-08"
        )[__import__("datetime").date(2026, 8, 8)]

        self.assertEqual(outcome["spo2"], 97.5)
        self.assertEqual(outcome["skin_temp"], 33.2)
        self.assertEqual(outcome["sleep_efficiency"], 89.5)
        self.assertEqual(outcome["respiratory_rate"], 14.1)
        self.assertEqual(outcome["sleep_disturbances"], 5)
        self.assertEqual(outcome["sleep_stages_ms"]["deep"], 7_000_000)
        self.assertEqual(
            outcome["sleep_provider_score"]["sleep_consistency_percentage"], 82
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
