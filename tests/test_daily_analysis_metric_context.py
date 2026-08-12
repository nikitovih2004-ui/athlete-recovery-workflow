import datetime as dt
import json
import unittest

import generate_insights as gi
import canonical_read_model as crm


class DailyAnalysisMetricContextTests(unittest.TestCase):
    def _outcomes(self, days=29):
        start = dt.date(2026, 6, 26)
        rows = {}
        for index in range(days):
            day = start + dt.timedelta(days=index)
            rows[day] = {
                "recovery_score": 60 + index,
                "hrv_rmssd": 50.0 + index,
                "resting_hr": 70.0 - index,
                "sleep_hours": 7.0 + index / 100,
                "sleep_performance": 80 + index / 2,
            }
        return rows

    def test_named_fields_and_units_cannot_swap_hrv_and_rhr(self):
        outcomes = self._outcomes()
        target = dt.date(2026, 7, 24)
        outcomes[target]["hrv_rmssd"] = 71.6
        outcomes[target]["resting_hr"] = 53
        metrics = {item["metric"]: item for item in gi.build_metric_context(outcomes, target)}

        self.assertEqual(metrics["hrv"]["source_field"], "hrv_rmssd")
        self.assertEqual(metrics["hrv"]["current_value"], 71.6)
        self.assertEqual(metrics["hrv"]["unit"], "ms")
        self.assertEqual(metrics["rhr"]["source_field"], "resting_hr")
        self.assertEqual(metrics["rhr"]["current_value"], 53.0)
        self.assertEqual(metrics["rhr"]["unit"], "bpm")

    def test_baseline_is_previous_28_valid_days_with_explicit_period(self):
        outcomes = self._outcomes()
        metrics = {item["metric"]: item for item in gi.build_metric_context(
            outcomes, dt.date(2026, 7, 24)
        )}
        recovery = metrics["recovery"]
        self.assertEqual(recovery["valid_observations"], 28)
        self.assertEqual(recovery["period_start"], "2026-06-26")
        self.assertEqual(recovery["period_end"], "2026-07-23")
        self.assertEqual(recovery["baseline_value"], 73.5)
        self.assertEqual(recovery["comparison_status"], "sufficient")

    def test_missing_days_nulls_and_zero_defaults_are_excluded(self):
        outcomes = self._outcomes()
        del outcomes[dt.date(2026, 7, 1)]
        outcomes[dt.date(2026, 7, 2)]["hrv_rmssd"] = None
        outcomes[dt.date(2026, 7, 3)]["hrv_rmssd"] = 0
        hrv = {item["metric"]: item for item in gi.build_metric_context(
            outcomes, dt.date(2026, 7, 24)
        )}["hrv"]
        self.assertEqual(hrv["valid_observations"], 25)
        self.assertEqual(hrv["excluded_invalid_values"], 1)
        self.assertEqual(hrv["period_start"], "2026-06-26")
        self.assertEqual(hrv["period_end"], "2026-07-23")

    def test_latest_28_are_used_after_catch_up_without_current_day(self):
        outcomes = self._outcomes(days=50)
        for row in outcomes.values():
            row["recovery_score"] = 70
        target = dt.date(2026, 8, 14)
        outcomes[target]["recovery_score"] = 99
        recovery = {item["metric"]: item for item in gi.build_metric_context(
            outcomes, target
        )}["recovery"]
        self.assertEqual(recovery["valid_observations"], 28)
        self.assertEqual(recovery["period_start"], "2026-07-17")
        self.assertEqual(recovery["period_end"], "2026-08-13")
        self.assertNotEqual(recovery["baseline_value"], 99)

    def test_insufficient_history_has_no_baseline(self):
        outcomes = self._outcomes(days=5)
        target = dt.date(2026, 7, 1)
        recovery = {item["metric"]: item for item in gi.build_metric_context(
            outcomes, target
        )}["recovery"]
        self.assertEqual(recovery["comparison_status"], "insufficient")
        self.assertIsNone(recovery["baseline_value"])
        self.assertEqual(recovery["valid_observations"], 5)
        self.assertEqual(recovery["required_observations"], 14)

    def test_prompt_receives_structured_contract_and_period_rule(self):
        context = gi.build_metric_context(self._outcomes(), dt.date(2026, 7, 24))
        payload = gi.metric_context_json(context)
        parsed = json.loads(payload)
        prompt = gi.build_llm_prompt("STRUCTURED_METRIC_CONTEXT_JSON:\n" + payload)
        self.assertEqual(parsed["schema"], "daily_metric_context.v1")
        self.assertIn("average over N valid days from DATE to DATE", prompt)
        self.assertIn("среднее по N валидным дням с DATE по DATE", prompt)
        self.assertIn('"source_field": "hrv_rmssd"', prompt)
        self.assertNotIn("dashboard.html", payload)

    def test_all_consumers_use_the_canonical_contract_function(self):
        outcomes = self._outcomes()
        target = dt.date(2026, 7, 24)
        self.assertEqual(
            gi.build_metric_context(outcomes, target),
            crm.daily_metric_context(outcomes, target),
        )

    def test_current_value_can_never_enter_its_own_baseline(self):
        outcomes = self._outcomes()
        target = dt.date(2026, 7, 24)
        outcomes[target]["recovery_score"] = 100
        metrics = {
            item["metric"]: item
            for item in crm.daily_metric_context(outcomes, target)
        }
        self.assertEqual(metrics["recovery"]["current_value"], 100.0)
        self.assertEqual(metrics["recovery"]["baseline_value"], 73.5)
        self.assertNotEqual(
            metrics["recovery"]["current_value"],
            metrics["recovery"]["baseline_value"],
        )


if __name__ == "__main__":
    unittest.main()
