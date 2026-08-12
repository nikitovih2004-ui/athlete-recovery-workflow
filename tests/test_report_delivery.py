import datetime as dt

import report_delivery
from tests.conversation_fakes import TempDBCase


class DurableReportDeliveryTests(TempDBCase):
    def test_partial_delivery_resumes_without_replaying_confirmed_chunks(self):
        sent = []

        def first_attempt(chunk):
            sent.append(chunk)
            if chunk == "two":
                return None
            return object()

        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            report_delivery.deliver(
                "daily_analysis:2026-07-10",
                "daily_analysis",
                "one\ntwo\nthree",
                ["one", "two", "three"],
                first_attempt,
            )
        row = report_delivery.get("daily_analysis:2026-07-10")
        self.assertEqual(row["status"], report_delivery.STATUS_PENDING)
        self.assertEqual(row["next_chunk"], 1)

        resumed = []
        self.assertTrue(report_delivery.deliver(
            "daily_analysis:2026-07-10",
            "daily_analysis",
            "one\ntwo\nthree",
            ["one", "two", "three"],
            lambda chunk: resumed.append(chunk) or object(),
        ))
        self.assertEqual(resumed, ["two", "three"])
        self.assertEqual(
            report_delivery.get("daily_analysis:2026-07-10")["status"],
            report_delivery.STATUS_DELIVERED,
        )

    def test_delivered_report_is_idempotent_after_restart(self):
        sent = []
        self.assertTrue(report_delivery.deliver(
            "weekly_report:2026-07-12",
            "weekly_report",
            "weekly",
            ["weekly"],
            lambda chunk: sent.append(chunk) or object(),
        ))
        self.assertTrue(report_delivery.deliver(
            "weekly_report:2026-07-12",
            "weekly_report",
            "weekly",
            ["weekly"],
            lambda chunk: sent.append(chunk) or object(),
        ))
        self.assertEqual(sent, ["weekly"])

    def test_delivery_key_cannot_be_reused_for_different_payload(self):
        report_delivery.prepare("daily:1", "daily_analysis", "first", 1)
        with self.assertRaises(report_delivery.DeliveryConflict):
            report_delivery.prepare("daily:1", "daily_analysis", "second", 1)

    def test_stale_claim_is_recoverable_but_live_claim_is_not(self):
        start = dt.datetime(2026, 7, 10, 8, 0, tzinfo=dt.timezone.utc)
        report_delivery.prepare("daily:2", "daily_analysis", "payload", 1, now=start)
        self.assertIsNotNone(report_delivery.claim("daily:2", now=start))
        self.assertIsNone(
            report_delivery.claim(
                "daily:2",
                now=start + report_delivery.CLAIM_TIMEOUT - dt.timedelta(seconds=1),
            )
        )
        self.assertIsNotNone(
            report_delivery.claim(
                "daily:2", now=start + report_delivery.CLAIM_TIMEOUT
            )
        )
