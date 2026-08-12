import datetime as dt

import morning_observability as obs
from tests.conversation_fakes import TempDBCase


UTC = dt.timezone.utc


class MorningObservabilityTests(TempDBCase):
    def test_every_event_requires_closed_stage_outcome_reason_and_duration(self):
        event_id = obs.record_stage(
            "2026-07-25",
            "cron:test",
            "cron_started",
            "success",
            "cron_invocation_started",
            started_at=dt.datetime(2026, 7, 25, 3, 0, tzinfo=UTC),
            finished_at=dt.datetime(2026, 7, 25, 3, 0, 1, tzinfo=UTC),
            duration_ms=1000,
        )
        self.assertGreater(event_id, 0)
        event = obs.events_for_period("2026-07-25", "2026-07-25")[0]
        self.assertEqual(event["stage"], "cron_started")
        self.assertEqual(event["outcome"], "success")
        self.assertEqual(event["reason"], "cron_invocation_started")
        self.assertEqual(event["duration_ms"], 1000)
        with self.assertRaises(ValueError):
            obs.record_stage(
                "2026-07-25", "x", "unknown", "success", "reason"
            )
        with self.assertRaises(ValueError):
            obs.record_stage(
                "2026-07-25", "x", "cron_started", "unknown", "reason"
            )
        with self.assertRaises(ValueError):
            obs.record_stage(
                "2026-07-25", "x", "cron_started", "success", ""
            )
        with self.assertRaises(ValueError):
            obs.record_stage(
                "2026-07-25", "x", "cron_started", "success",
                "provider response included free-form text",
            )

    def test_timeline_has_all_stages_and_does_not_hide_failures(self):
        at = dt.datetime(2026, 7, 25, 5, 0, tzinfo=UTC)
        obs.record_stage(
            "2026-07-25", "run-1", "cron_started", "success",
            "cron_invocation_started", started_at=at, duration_ms=0,
        )
        obs.record_stage(
            "2026-07-25", "run-1", "whoop_refresh_result", "failed",
            "oauth_server_error:http_status=502", started_at=at, duration_ms=321,
        )
        obs.record_stage(
            "2026-07-25", "run-2", "whoop_refresh_result", "success",
            "token_rotated", started_at=at, duration_ms=200,
        )
        report = obs.render_timeline(1, "2026-07-25")
        self.assertIn("2026-07-25", report)
        self.assertIn("✓ refresh result", report)
        self.assertIn("latest failure", report)
        self.assertIn("oauth_server_error:http_status=502", report)
        self.assertIn("— Dashboard rebuilt | not_observed", report)
        self.assertEqual(
            set(obs.timeline(1, "2026-07-25")["2026-07-25"]),
            set(obs.STAGES),
        )

    def test_safe_exception_reason_uses_category_not_secret_message(self):
        class SafeError(RuntimeError):
            category = "provider_5xx"
            status_code = 503

        error = SafeError("secret-token-should-not-appear")
        reason = obs.safe_exception_reason(error, prefix="recovery_fetch")
        self.assertEqual(reason, "recovery_fetch:provider_5xx:http_status=503")
        self.assertNotIn("secret-token", reason)
