"""True end-to-end coverage: real write path -> real consumer read model.

Existing suites cover "real write -> raw SQL check" and "raw SQL fixture ->
read model" as two disjoint halves. Neither proves that a workout, supplement,
daily-context note, and factor observation actually become visible, with
consistent numbers, through the same read models Dashboard/Weekly/Analytics/
Conversation use in production. This file stitches both halves together.
"""
import datetime as dt
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import build_dashboard
import canonical_read_model as crm
import conversation_read_models
import conversation_store
import conversation_tools
import dashboard_contract
import factor_capture
import generate_insights
import morning_reporting
import phase2_store
import workouts_db
from tests.conversation_fakes import TempDBCase

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
DATE = "2026-07-10"


class FullCycleEndToEndTests(TempDBCase):
    def _ctx(self, message_id):
        reservation = conversation_store.reserve(conversation_store.ActionContext(
            source="telegram", chat_id="1", user_id="42",
            message_id=message_id, input_text="e2e",
        ))
        return conversation_tools.ExecContext(
            action_id=reservation.action_id, source="telegram", chat_id="1",
            message_id=message_id, local_now=NOW,
            processing_token=reservation.processing_token,
            processing_fence=reservation.processing_fence,
        )

    def test_full_write_then_read_cycle_across_consumers(self):
        strength_result = conversation_tools.execute("log_strength_workout", {
            "entries": [{"exercise_name": "жим лёжа", "weight_kg": 60,
                         "sets": 3, "reps": 8}],
            "resolved_date": DATE,
        }, self._ctx("m1"))
        self.assertEqual(strength_result["created_count"], 1)

        supplement_result = conversation_tools.execute("log_supplement", {
            "items": [{"name": "creatine", "dose_text": "5 g", "taken": True}],
            "resolved_date": DATE,
        }, self._ctx("m2"))
        self.assertEqual(supplement_result["created_count"], 1)

        with patch.dict(os.environ, {"DAILY_FACTOR_CAPTURE_ENABLED": "true"}):
            context_result = conversation_tools.execute("save_daily_context", {
                "notes": "без алкоголя, лёг рано",
                "resolved_date": DATE,
            }, self._ctx("m3"))
        self.assertEqual(context_result["created_count"], 1)
        factor_job_id = context_result["data"]["factor_job_id"]
        self.assertIsNotNone(factor_job_id)

        # _save_daily_context() doesn't pass `now=` to enqueue_factor_job, so
        # the job's available_at is stamped with real wall-clock time - claim
        # with real "now" too, independent of the fixed business-date NOW
        # used for message/context timestamps above.
        claim_now = dt.datetime.now(UTC)
        conn = workouts_db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            job = phase2_store.claim_factor_job(conn, job_id=factor_job_id, now=claim_now)
            self.assertIsNotNone(job)
            completed = phase2_store.complete_factor_job(
                conn, job["job_id"], job["lease_token"],
                [{"factor_key": "alcohol", "state": 0, "confidence": 0.9}],
                allowed_factor_keys=factor_capture.FACTOR_KEY_SET, now=claim_now,
            )
            self.assertTrue(completed)
            conn.commit()
        finally:
            conn.close()

        # --- Read everything back through the real consumer-facing paths. ---
        conn = workouts_db.connect()
        try:
            snapshot = crm.range_snapshot(conn, DATE, DATE)
            today = conversation_read_models.today_status(conn, NOW)
        finally:
            conn.close()

        # Dashboard / Weekly / Analytics all source range_snapshot.
        self.assertEqual(snapshot["activity"]["summary"]["manual_strength_sets"], 3)
        self.assertEqual(snapshot["supplements"]["summary"]["taken_count"], 1)
        day = next(d for d in snapshot["days"] if d["action_date"] == DATE)
        self.assertIn("без алкоголя", day["context"]["notes"])
        self.assertEqual(len(day["daily_factors"]), 1)
        self.assertEqual(day["daily_factors"][0]["factor_key"], "alcohol")
        self.assertEqual(day["daily_factors"][0]["state"], 0)
        self.assertEqual(
            snapshot["completeness"]["factor_extraction"]["succeeded_current_projection_days"],
            1,
        )

        # Conversation's own today_status must show the identical numbers -
        # this is what actually proves the consumers are not diverging.
        self.assertEqual(today["logged_today"]["strength_sets"], 3)
        self.assertEqual(today["logged_today"]["supplements"], 1)
        self.assertEqual(
            today["logged_today"]["strength_sets"],
            snapshot["activity"]["summary"]["manual_strength_sets"],
        )
        self.assertEqual(
            today["logged_today"]["supplements"],
            snapshot["supplements"]["summary"]["taken_count"],
        )
        self.assertEqual(
            today["completeness"]["factor_extraction"]["succeeded_current_projection_days"],
            snapshot["completeness"]["factor_extraction"]["succeeded_current_projection_days"],
        )

    def test_write_rebuild_analysis_and_delivery_share_canonical_state(self):
        conversation_tools.execute("log_strength_workout", {
            "entries": [{"exercise_name": "bench press", "weight_kg": 60,
                         "sets": 3, "reps": 8}],
            "resolved_date": DATE,
        }, self._ctx("pipeline-strength"))
        conversation_tools.execute("log_supplement", {
            "items": [{"name": "creatine", "dose_text": "5 g", "taken": True}],
            "resolved_date": DATE,
        }, self._ctx("pipeline-supplement"))

        conn = workouts_db.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE recovery (
                    cycle_id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    recovery_score REAL,
                    hrv_rmssd REAL,
                    resting_hr REAL
                );
                CREATE TABLE sleep (
                    id TEXT PRIMARY KEY,
                    start TEXT,
                    end TEXT NOT NULL,
                    performance_pct REAL,
                    efficiency_pct REAL,
                    respiratory_rate REAL,
                    raw_json TEXT
                );
                """
            )
            first_outcome = dt.date(2026, 6, 13)
            for offset in range(29):
                outcome = first_outcome + dt.timedelta(days=offset)
                instant = f"{outcome.isoformat()}T05:00:00+00:00"
                stages = {
                    "total_in_bed_time_milli": 8 * 3_600_000,
                    "total_awake_time_milli": 30 * 60_000,
                    "total_rem_sleep_time_milli": 90 * 60_000,
                    "total_light_sleep_time_milli": 4 * 3_600_000,
                    "total_slow_wave_sleep_time_milli": 90 * 60_000,
                    "disturbance_count": 2,
                }
                conn.execute(
                    "INSERT INTO recovery VALUES (?,?,?,?,?)",
                    (offset + 1, instant, 55 + (offset % 10),
                     45 + offset, 62 - offset / 10),
                )
                conn.execute(
                    "INSERT INTO sleep VALUES (?,?,?,?,?,?,?)",
                    (
                        f"sleep-{offset}", instant, instant,
                        80 + (offset % 10), 94, 15,
                        json.dumps({"nap": False, "score": {"stage_summary": stages}}),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        output = Path(self.temp.name) / "dashboard.html"
        with patch.object(build_dashboard, "DB_PATH", self.db), \
             patch.object(build_dashboard, "OUT", str(output)):
            build_dashboard.main()

        first_artifact = output.read_text(encoding="utf-8")
        dashboard_contract.validate_artifact(first_artifact)
        self.assertIn("bench press", first_artifact)
        self.assertIn('"source":"canonical_read_model"', first_artifact)

        # A later canonical write must be visible after the next rebuild; the
        # generated artifact is never allowed to become an input/source cache.
        conversation_tools.execute("log_strength_workout", {
            "entries": [{"exercise_name": "lat pulldown", "weight_kg": 43,
                         "sets": 2, "reps": 10}],
            "resolved_date": DATE,
        }, self._ctx("pipeline-strength-2"))
        with patch.object(build_dashboard, "DB_PATH", self.db), \
             patch.object(build_dashboard, "OUT", str(output)):
            build_dashboard.main()
        second_artifact = output.read_text(encoding="utf-8")
        self.assertNotEqual(first_artifact, second_artifact)
        self.assertIn("lat pulldown", second_artifact)

        conn = workouts_db.connect()
        try:
            model = crm.CanonicalReadModel(conn)
            outcomes = model.outcomes(dt.date(2026, 6, 13), dt.date(2026, 7, 11))
            metric_context = crm.daily_metric_context(outcomes, dt.date(2026, 7, 11))
        finally:
            conn.close()

        expected = {
            "recovery": ("recovery_score", "%"),
            "hrv": ("hrv_rmssd", "ms"),
            "rhr": ("resting_hr", "bpm"),
            "sleep_duration": ("sleep_hours", "h"),
            "sleep_performance": ("sleep_performance", "%"),
        }
        metric_by_name = {item["metric"]: item for item in metric_context}
        for key, (field, unit) in expected.items():
            metric = metric_by_name[key]
            self.assertEqual(metric["source_field"], field)
            self.assertEqual(metric["unit"], unit)
            self.assertEqual(metric["valid_observations"], 28)
            self.assertEqual(metric["period_start"], "2026-06-13")
            self.assertEqual(metric["period_end"], "2026-07-10")
            self.assertEqual(metric["current_outcome_date"], "2026-07-11")
            self.assertNotEqual(metric["period_end"], metric["current_outcome_date"])

        structured = generate_insights.metric_context_json(metric_context)
        prompt = generate_insights.build_llm_prompt(
            "canonical e2e report\n\nDAILY_METRIC_CONTEXT_JSON:\n" + structured
        )
        self.assertIn('"unit": "ms"', prompt)
        self.assertIn('"unit": "bpm"', prompt)
        self.assertIn('"valid_observations": 28', prompt)
        self.assertIn('"period_start": "2026-06-13"', prompt)

        rendered = "\n\n".join(
            f"section-{index}: {prompt[index:index + 500]}"
            for index in range(0, len(prompt), 500)
        )
        chunks = morning_reporting.split_telegram_text(rendered, limit=900)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 900 for chunk in chunks))
        for index in range(0, len(prompt), 500):
            self.assertIn(f"section-{index}:", "\n".join(chunks))


if __name__ == "__main__":
    unittest.main()
