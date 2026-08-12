import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import morning_reporting


class MorningResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "whoop.db"
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE workouts (
                id TEXT, start TEXT, end TEXT, sport_name TEXT, strain REAL,
                avg_hr REAL, max_hr REAL, kilojoule REAL,
                distance_meter REAL, raw_json TEXT
            );
            CREATE TABLE recovery (
                cycle_id TEXT, created_at TEXT, recovery_score REAL,
                hrv_rmssd REAL, resting_hr REAL, spo2 REAL,
                skin_temp REAL, raw_json TEXT
            );
            CREATE TABLE sleep (
                id TEXT, start TEXT, end TEXT, performance_pct REAL,
                efficiency_pct REAL, respiratory_rate REAL, raw_json TEXT
            );
        """)
        workout_score = {
            "strain": 12.3,
            "average_heart_rate": 145,
            "max_heart_rate": 177,
            "kilojoule": 1800,
            "distance_meter": 9000,
            "altitude_gain_meter": 42,
            "altitude_change_meter": 18,
            "percent_recorded": 99,
            "zone_duration": {
                "zone_zero_milli": 600_000,
                "zone_one_milli": 900_000,
                "zone_two_milli": 1_200_000,
                "zone_three_milli": 600_000,
                "zone_four_milli": 240_000,
                "zone_five_milli": 60_000,
            },
        }
        conn.execute(
            "INSERT INTO workouts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "internal-workout-id", "2026-08-07T15:00:00Z",
                "2026-08-07T16:00:00Z", "Running", 12.3, 145, 177,
                1800, 9000, json.dumps({
                    "id": "raw-internal-id", "score": workout_score
                }),
            ),
        )
        conn.execute(
            "INSERT INTO recovery VALUES (?,?,?,?,?,?,?,?)",
            (
                "internal-cycle-id", "2026-08-08T05:00:00Z",
                78, 61, 49, 97.5, 33.2,
                json.dumps({"score": {"user_calibrating": False}}),
            ),
        )
        sleep_score = {
            "sleep_performance_percentage": 91,
            "sleep_efficiency_percentage": 89.5,
            "sleep_consistency_percentage": 82,
            "respiratory_rate": 14.1,
            "stage_summary": {
                "total_in_bed_time_milli": 28_800_000,
                "total_awake_time_milli": 1_800_000,
                "total_light_sleep_time_milli": 14_000_000,
                "total_rem_sleep_time_milli": 6_000_000,
                "total_slow_wave_sleep_time_milli": 7_000_000,
                "total_no_data_time_milli": 0,
                "disturbance_count": 5,
                "sleep_cycle_count": 4,
            },
            "sleep_needed": {
                "baseline_milli": 27_000_000,
                "need_from_sleep_debt_milli": 1_800_000,
                "need_from_recent_strain_milli": 900_000,
                "need_from_recent_nap_milli": -600_000,
            },
        }
        conn.execute(
            "INSERT INTO sleep VALUES (?,?,?,?,?,?,?)",
            (
                "internal-sleep-id", "2026-08-07T21:00:00Z",
                "2026-08-08T05:00:00Z", 91, 89.5, 14.1,
                json.dumps({
                    "id": "raw-sleep-id", "nap": False, "score": sleep_score
                }),
            ),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_all_available_whoop_categories_are_normalized_without_ids(self):
        metrics = morning_reporting.load_morning_whoop_metrics(
            "2026-08-08", db_path=self.db
        )

        self.assertEqual(metrics["workouts"][0]["sport"], "Running")
        self.assertEqual(metrics["workouts"][0]["zones_ms"]["5"], 60_000)
        self.assertEqual(metrics["recovery"]["spo2_pct"], 97.5)
        self.assertEqual(metrics["recovery"]["skin_temp_c"], 33.2)
        self.assertEqual(metrics["sleep"]["consistency_pct"], 82)
        self.assertEqual(metrics["sleep"]["cycles"], 4)
        self.assertEqual(metrics["sleep"]["need_h"]["sleep_debt"], 0.5)
        self.assertNotIn("id", metrics["workouts"][0])
        self.assertNotIn("raw_json", metrics["workouts"][0])

    def test_final_result_is_one_bounded_payload_with_metrics_before_analysis(self):
        payload = morning_reporting.compose_morning_result(
            "2026-08-08", "Персональный вывод.", db_path=self.db
        )

        self.assertLessEqual(len(payload), 3900)
        self.assertLess(payload.index("📊 WHOOP"), payload.index("🧠 Персональный разбор"))
        self.assertIn("🏋️ Тренировки", payload)
        self.assertIn("💚 Восстановление", payload)
        self.assertIn("😴 Сон", payload)
        self.assertIn("SpO₂", payload)
        self.assertIn("Стадии", payload)
        self.assertIn("Потребность во сне", payload)
        self.assertNotIn("internal-workout-id", payload)
        self.assertNotIn("raw-internal-id", payload)
        self.assertIsInstance(payload, str)

    def test_long_analysis_is_transparently_shortened_not_split(self):
        payload = morning_reporting.compose_morning_result(
            "2026-08-08", ("наблюдение " * 1000), db_path=self.db, limit=1200
        )

        self.assertLessEqual(len(payload), 1200)
        self.assertIn("Разбор сокращён", payload)
        self.assertEqual(payload.count("🧠 Персональный разбор"), 1)


if __name__ == "__main__":
    unittest.main()
