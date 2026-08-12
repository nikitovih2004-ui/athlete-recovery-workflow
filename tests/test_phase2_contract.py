import os
import unittest
from pathlib import Path
from unittest.mock import patch

import conversation_contract as C
import phase2_flags


class Phase2ContractTests(unittest.TestCase):
    def test_new_reads_are_static_allowlisted_tools(self):
        self.assertIn(C.INTENT_GET_METRIC_TREND, C.READ_INTENTS)
        self.assertIn(C.INTENT_GET_FACTOR_OBSERVATION, C.READ_INTENTS)
        self.assertEqual(C.tool_for_intent(C.INTENT_GET_METRIC_TREND),
                         "get_metric_trend")
        self.assertEqual(C.tool_for_intent(C.INTENT_GET_FACTOR_OBSERVATION),
                         "get_factor_observation")

    def test_query_and_factor_vocabularies_are_closed(self):
        self.assertEqual(C.TREND_WINDOWS_DAYS, {7, 14, 28, 56, 84})
        self.assertNotIn("sql", C.TREND_METRICS)
        self.assertEqual(C.DAILY_FACTOR_KEYS,
                         {"alcohol", "late_caffeine", "late_meal", "high_stress"})
        self.assertEqual(C.MIN_FACTOR_COHORT_DAYS, 5)

    def test_all_phase2_rollout_flags_default_off(self):
        names = [
            "CONVERSATION_MEMORY_ENABLED",
            "CONVERSATIONAL_ROUTER_ENABLED",
            "CONVERSATION_ANALYTICS_V2_ENABLED",
            "DAILY_FACTOR_CAPTURE_ENABLED",
            "WEEKLY_ANALYSIS_V2_ENABLED",
            "GEMINI_VISION_ENABLED",
        ]
        clean = {k: v for k, v in os.environ.items() if k not in names}
        with patch.dict(os.environ, clean, clear=True):
            self.assertFalse(phase2_flags.memory_enabled())
            self.assertFalse(phase2_flags.analytics_v2_enabled())
            self.assertFalse(phase2_flags.factor_capture_enabled())
            self.assertFalse(phase2_flags.weekly_v2_enabled())
            self.assertFalse(phase2_flags.router_enabled())
            self.assertFalse(phase2_flags.gemini_vision_enabled())

    def test_flags_are_read_at_call_time(self):
        with patch.dict(os.environ, {"CONVERSATION_MEMORY_ENABLED": "true"}):
            self.assertTrue(phase2_flags.memory_enabled())

    def test_unknown_flag_value_fails_closed_instead_of_disabling_silently(self):
        with patch.dict(os.environ, {"CONVERSATION_MEMORY_ENABLED": "treu"}):
            with self.assertRaisesRegex(ValueError, "explicit boolean"):
                phase2_flags.memory_enabled()

    def test_every_production_flag_has_one_central_default(self):
        self.assertEqual(
            set(phase2_flags.validate_environment()),
            set(phase2_flags.FLAG_DEFAULTS),
        )

    def test_example_environment_keeps_rollout_off_and_documents_relay(self):
        example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("GEMINI_TRANSPORT=", example)
        self.assertIn("GEMINI_RELAY_URL=", example)
        self.assertIn("GEMINI_RELAY_SECRET=", example)
        self.assertIn("CONVERSATION_MEMORY_ENABLED=false", example)
        self.assertIn("CONVERSATION_ANALYTICS_V2_ENABLED=false", example)

        self.assertIn("GEMINI_VISION_ENABLED=false", example)

if __name__ == "__main__":
    unittest.main()
