import unittest

import weekly_analysis_v2


def snapshot(with_factor=False):
    metric = lambda value, n=7: {"mean": value, "sample_size": n}
    week = {
        "action_week_start": "2026-07-06", "action_week_end": "2026-07-12",
        "outcome_start": "2026-07-07", "outcome_end": "2026-07-13",
        "metrics": {
            "recovery_score": metric(70), "hrv_rmssd": metric(55),
            "resting_hr": metric(52), "sleep_hours": metric(7.5),
            "sleep_performance": metric(85),
        },
        "coverage": {},
        "activity": {"strength_days": 3, "strength_sets": 12,
                     "strength_volume": 4200, "cardio_sessions": 2,
                     "cardio_minutes": 75},
    }
    factor = {
        "factor_key": "magnesium", "start_date": "2026-04-20",
        "end_date": "2026-07-12", "eligible": True, "minimum_cohort": 5,
        "day_states": {"missing": 0, "unknown": 0, "conflict": 0},
        "cohorts": {
            "present": {"outcomes": {"recovery_score": {"sample_size": 6, "mean": 72}}},
            "absent": {"outcomes": {"recovery_score": {"sample_size": 5, "mean": 68}}},
        },
        "mean_delta_present_minus_absent": {"recovery_score": 4},
    }
    return {
        "snapshot": "weekly_evidence.v2", "current_week": week,
        "previous_week": week,
        "mean_delta_current_minus_previous": {
            "recovery_score": 2, "hrv_rmssd": 3, "resting_hr": -1,
            "sleep_hours": 0.4, "sleep_performance": 2,
        },
        "factor_observations": [factor] if with_factor else [],
    }


class WeeklyAnalysisV2Tests(unittest.TestCase):
    def test_report_is_compact_grounded_and_noncausal(self):
        text = weekly_analysis_v2.render(snapshot())
        self.assertIn("WHOOP outcomes:", text)
        self.assertIn("5+5", text)
        self.assertIn("причинных выводов не делаю", text)
        self.assertLess(len(text), 4096)

    def test_eligible_factor_keeps_observational_language(self):
        text = weekly_analysis_v2.render(snapshot(with_factor=True))
        self.assertIn("наблюдаемое совпадение", text)
        self.assertIn("не доказательство причинности", text)
        self.assertIn("n=6", text)


if __name__ == "__main__":
    unittest.main()
