"""Regression coverage for WHOOP-gated, restart-safe morning orchestration."""
import datetime as dt
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import daily_log
import morning_context
import morning_flow
import morning_observability
import morning_readiness
import telegram_bot


KYIV = morning_readiness.TZ


class MorningWhoopGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "whoop.db")
        self.old_daily = daily_log.DB_PATH
        self.old_flow = morning_flow.DB_PATH
        daily_log.DB_PATH = self.db
        morning_flow.DB_PATH = Path(self.db)
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE recovery (
                cycle_id INTEGER, created_at TEXT, recovery_score REAL,
                hrv_rmssd REAL, resting_hr REAL
            );
            CREATE TABLE sleep (
                sleep_id TEXT, end TEXT, performance_pct REAL, raw_json TEXT
            );
        """)
        conn.close()

    def tearDown(self):
        daily_log.DB_PATH = self.old_daily
        morning_flow.DB_PATH = self.old_flow
        self.temp.cleanup()

    def add_morning(self, day="2026-07-10", recovery=True, sleep=True):
        target = dt.date.fromisoformat(day)
        instant = dt.datetime.combine(target, dt.time(7), tzinfo=KYIV).astimezone(dt.timezone.utc)
        conn = sqlite3.connect(self.db)
        if recovery:
            conn.execute(
                "INSERT INTO recovery VALUES (?,?,?,?,?)",
                (1, instant.isoformat(), 82, 61, 52),
            )
        if sleep:
            conn.execute(
                "INSERT INTO sleep VALUES (?,?,?,?)",
                ("s1", instant.isoformat(), 91, json.dumps({"nap": False})),
            )
        conn.commit()
        conn.close()

    def observed_script(self, name, *_args, **kwargs):
        if name == "fetch_data.py":
            run_id = kwargs["pipeline_run_id"]
            pipeline_date = kwargs["pipeline_date"]
            status = morning_readiness.morning_data_status(
                pipeline_date, db_path=self.db
            )
            morning_observability.record_stage(
                pipeline_date, run_id, "whoop_refresh_attempted", "skipped",
                "access_token_current_no_refresh_required",
            )
            morning_observability.record_stage(
                pipeline_date, run_id, "whoop_refresh_result", "success",
                "access_token_current",
            )
            morning_observability.record_stage(
                pipeline_date, run_id, "recovery_imported",
                "success" if status["recovery"] else "waiting",
                "current_morning_recovery_present"
                if status["recovery"] else "current_morning_recovery_absent",
            )
            morning_observability.record_stage(
                pipeline_date, run_id, "sleep_imported",
                "success" if status["sleep"] else "waiting",
                "current_morning_sleep_present"
                if status["sleep"] else "current_morning_sleep_absent",
            )
        return morning_flow.ScriptResult(
            True, f"subprocess_completed:{name}", 1, 0
        )

    def ask_and_reply(self, recovery_date="2026-07-10", question=700, message=701):
        morning_context.ensure_request(recovery_date)
        self.assertIsNotNone(morning_context.claim_question(recovery_date))
        self.assertTrue(morning_context.mark_question_sent(recovery_date, question))
        return morning_context.accept_and_record_reply(
            message, question, "спал хорошо", source_key=f"telegram:1:{message}:morning-context",
            factor_capture_enabled=False,
        )

    def row(self, recovery_date="2026-07-10"):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute(
                "SELECT * FROM morning_context WHERE recovery_date=?", (recovery_date,)
            ).fetchone())
        finally:
            conn.close()

    def test_readiness_requires_recovery_hrv_rhr_and_sleep(self):
        self.add_morning(sleep=False)
        self.assertFalse(morning_readiness.morning_data_status("2026-07-10")["ready"])
        self.add_morning(recovery=False)
        self.assertTrue(morning_readiness.morning_data_status("2026-07-10")["ready"])

    def test_no_early_prompt_and_late_whoop_prompts_once(self):
        now = dt.datetime(2026, 7, 10, 8, 0, tzinfo=KYIV)
        with patch.object(sys, "argv", ["morning_flow.py"]), \
             patch.object(morning_flow, "current_time", side_effect=[now, now.replace(hour=10), now.replace(hour=10, minute=15)]), \
             patch.object(morning_flow, "run_script", side_effect=self.observed_script), \
             patch.object(morning_flow, "send_telegram", return_value=1700) as send, \
             patch.object(morning_flow, "log"):
            morning_flow.main()
            self.assertEqual(send.call_count, 0)
            self.add_morning()
            morning_flow.main()
            morning_flow.main()
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.row()["question_message_id"], "1700")


    def test_materialized_morning_skips_later_provider_fetch(self):
        self.add_morning()
        now = dt.datetime(2026, 7, 10, 10, 0, tzinfo=KYIV)
        with patch.object(sys, "argv", ["morning_flow.py"]), \
             patch.object(morning_flow, "current_time", return_value=now), \
             patch.object(morning_flow, "run_script", side_effect=self.observed_script) as run, \
             patch.object(morning_flow, "send_telegram", return_value=1702), \
             patch.object(morning_flow, "log"):
            self.assertEqual(morning_flow.main(), 0)
            self.assertEqual(morning_flow.main(), 0)

        fetch_calls = [
            call for call in run.call_args_list
            if call.args and call.args[0] == "fetch_data.py"
        ]
        self.assertEqual(len(fetch_calls), 1)
        events = morning_observability.events_for_period(
            "2026-07-10", "2026-07-10"
        )
        self.assertTrue(any(
            event["stage"] == "whoop_refresh_attempted"
            and event["outcome"] == "skipped"
            and event["reason"] == "current_morning_workflow_already_materialized"
            for event in events
        ))

    def test_force_fetches_after_morning_is_materialized(self):
        self.add_morning()
        now = dt.datetime(2026, 7, 10, 10, 0, tzinfo=KYIV)
        with patch.object(morning_flow, "current_time", return_value=now), \
             patch.object(morning_flow, "run_script", side_effect=self.observed_script) as run, \
             patch.object(morning_flow, "send_telegram", return_value=1703), \
             patch.object(morning_flow, "log"):
            with patch.object(sys, "argv", ["morning_flow.py"]):
                self.assertEqual(morning_flow.main(), 0)
            with patch.object(sys, "argv", ["morning_flow.py", "--force"]):
                self.assertEqual(morning_flow.main(), 0)

        fetch_calls = [
            call for call in run.call_args_list
            if call.args and call.args[0] == "fetch_data.py"
        ]
        self.assertEqual(len(fetch_calls), 2)

    def test_oauth_failure_does_not_prompt_until_catchup_data_exists(self):
        now = dt.datetime(2026, 7, 10, 9, 0, tzinfo=KYIV)
        failed = False

        def fail_once(name, *_args, **kwargs):
            nonlocal failed
            if name == "fetch_data.py" and not failed:
                failed = True
                run_id = kwargs["pipeline_run_id"]
                pipeline_date = kwargs["pipeline_date"]
                morning_observability.record_stage(
                    pipeline_date, run_id, "whoop_refresh_attempted",
                    "success", "refresh_http_post_attempted",
                )
                morning_observability.record_stage(
                    pipeline_date, run_id, "whoop_refresh_result",
                    "failed", "whoop_oauth:oauth_server_error:http_status=502",
                )
                return morning_flow.ScriptResult(
                    False, "subprocess_exit:fetch_data.py:code=1", 1, 1
                )
            return self.observed_script(name, *_args, **kwargs)

        with patch.object(sys, "argv", ["morning_flow.py"]), \
             patch.object(morning_flow, "current_time", return_value=now), \
             patch.object(morning_flow, "run_script", side_effect=fail_once), \
             patch.object(morning_flow, "send_telegram", return_value=1701) as send, \
             patch.object(morning_flow, "log"):
            morning_flow.main()
            self.add_morning()
            morning_flow.main()
        self.assertEqual(send.call_count, 1)

    def test_dashboard_failure_stops_before_prompt_with_exact_timeline(self):
        self.add_morning()
        now = dt.datetime(2026, 7, 10, 10, 0, tzinfo=KYIV)

        def fail_dashboard(name, *_args, **kwargs):
            if name == "build_dashboard.py":
                return morning_flow.ScriptResult(
                    False,
                    "subprocess_exit:build_dashboard.py:code=2",
                    14,
                    2,
                )
            return self.observed_script(name, *_args, **kwargs)

        with patch.object(sys, "argv", ["morning_flow.py"]), \
             patch.object(morning_flow, "current_time", return_value=now), \
             patch.object(morning_flow, "run_script", side_effect=fail_dashboard), \
             patch.object(morning_flow, "send_telegram") as send, \
             patch.object(morning_flow, "log"):
            self.assertEqual(morning_flow.main(), 1)
        send.assert_not_called()
        events = morning_observability.events_for_period(
            "2026-07-10", "2026-07-10"
        )
        dashboard = [
            event for event in events
            if event["stage"] == "dashboard_rebuilt"
        ][-1]
        self.assertEqual(dashboard["outcome"], "failed")
        self.assertEqual(
            dashboard["reason"],
            "subprocess_exit:build_dashboard.py:code=2",
        )
        downstream = {
            event["stage"]: event for event in events
            if event["stage"] in {
                "prompt_candidate_created", "prompt_delivered",
                "analysis_generated",
            }
        }
        self.assertEqual(
            {event["outcome"] for event in downstream.values()},
            {"skipped"},
        )

    def test_missing_child_observability_fails_closed(self):
        self.add_morning()
        now = dt.datetime(2026, 7, 10, 10, 0, tzinfo=KYIV)
        with patch.object(sys, "argv", ["morning_flow.py"]), \
             patch.object(morning_flow, "current_time", return_value=now), \
             patch.object(
                 morning_flow,
                 "run_script",
                 return_value=morning_flow.ScriptResult(
                     True, "subprocess_completed:fetch_data.py", 1, 0
                 ),
             ), \
             patch.object(morning_flow, "send_telegram") as send, \
             patch.object(morning_flow, "log"):
            self.assertEqual(morning_flow.main(), 1)
        send.assert_not_called()
        events = morning_observability.events_for_period(
            "2026-07-10", "2026-07-10"
        )
        missing = [
            event for event in events
            if event["reason"].startswith(
                "child_observability_event_missing:"
            )
        ]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["outcome"], "failed")

    def test_prompt_failure_stops_analysis_and_records_transport_category(self):
        self.add_morning()
        now = dt.datetime(2026, 7, 10, 10, 0, tzinfo=KYIV)

        def fail_send(_text):
            morning_flow._last_telegram_reason = "telegram_http_status=502"
            return None

        with patch.object(sys, "argv", ["morning_flow.py"]), \
             patch.object(morning_flow, "current_time", return_value=now), \
             patch.object(morning_flow, "run_script", side_effect=self.observed_script), \
             patch.object(morning_flow, "send_telegram", side_effect=fail_send), \
             patch.object(morning_flow, "log"):
            self.assertEqual(morning_flow.main(), 1)
        events = morning_observability.events_for_period(
            "2026-07-10", "2026-07-10"
        )
        prompt = [
            event for event in events
            if event["stage"] == "prompt_delivered"
        ][-1]
        self.assertEqual(prompt["outcome"], "failed")
        self.assertEqual(prompt["reason"], "telegram_http_status=502")
        analysis = [
            event for event in events
            if event["stage"] == "analysis_generated"
        ][-1]
        self.assertEqual(analysis["outcome"], "skipped")
        self.assertIn("prompt_delivery_failed", analysis["reason"])

    def test_reply_with_ready_whoop_delivers_without_preparing_message(self):
        self.add_morning()
        morning_context.ensure_request("2026-07-10")
        morning_context.claim_question("2026-07-10")
        morning_context.mark_question_sent("2026-07-10", 700)
        message = SimpleNamespace(
            message_id=701, text="спал хорошо", chat=SimpleNamespace(id=1),
            reply_to_message=SimpleNamespace(message_id=700),
        )
        sent = []
        def record_send(_chat_id, text, **_kwargs):
            sent.append(text)
            return SimpleNamespace(message_id=len(sent))

        with patch.object(telegram_bot.bot, "send_message", side_effect=record_send), \
             patch.object(telegram_bot.morning_reporting, "generate_daily_analysis", return_value="report"), \
             patch.object(telegram_bot, "_capture_daily_factors"):
            self.assertTrue(telegram_bot.handle_morning_context_response(message, message.text))
        self.assertEqual(self.row()["status"], morning_context.STATUS_ANALYZED)
        self.assertFalse(any("Готовлю" in text for text in sent))
        self.assertEqual(len(sent), 1)
        self.assertLess(
            sent[0].index("📊 WHOOP"),
            sent[0].index("🧠 Персональный разбор"),
        )

    def test_scheduled_analysis_delivers_one_ordered_final_message(self):
        self.add_morning()
        self.ask_and_reply()
        claimed = morning_context.claim_analysis("2026-07-10", "whoop")
        sent = []

        with patch.object(
            morning_flow.morning_reporting,
            "generate_daily_analysis",
            return_value="scheduled report",
        ), patch.object(
            morning_flow, "send_telegram",
            side_effect=lambda text: sent.append(text) or 1,
        ), patch.object(morning_flow, "log"):
            self.assertTrue(morning_flow.deliver_claimed_analysis(claimed))

        self.assertEqual(len(sent), 1)
        self.assertLess(sent[0].index("📊 WHOOP"), sent[0].index(
            "🧠 Персональный разбор"
        ))

    def test_provider_failure_retries_after_backoff(self):
        self.add_morning()
        context = self.ask_and_reply()
        now = dt.datetime(2026, 7, 10, 10, 0, tzinfo=KYIV)
        claimed = morning_context.claim_analysis("2026-07-10", "whoop", now=now)
        self.assertIsNotNone(claimed)
        with patch.object(morning_flow.morning_reporting, "generate_daily_analysis", side_effect=RuntimeError("temporary")), \
             patch.object(morning_flow, "log"):
            self.assertFalse(morning_flow.deliver_claimed_analysis(claimed))
        row = self.row()
        self.assertEqual(row["status"], morning_context.STATUS_ANSWERED)
        available = dt.datetime.fromisoformat(row["analysis_available_at"])
        self.assertIsNone(morning_context.claim_analysis("2026-07-10", "whoop", now=available - dt.timedelta(seconds=1)))
        self.assertIsNotNone(morning_context.claim_analysis("2026-07-10", "whoop", now=available))

    def test_duplicate_reply_does_not_duplicate_context_or_analysis_claim(self):
        self.add_morning()
        first = self.ask_and_reply()
        duplicate = morning_context.accept_and_record_reply(
            701, 700, "спал хорошо", source_key="telegram:1:701:morning-context",
            factor_capture_enabled=False,
        )
        self.assertFalse(duplicate["inserted"])
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_context_entries").fetchone()[0], 1)
        finally:
            conn.close()
        self.assertIsNotNone(morning_context.claim_analysis(first["recovery_date"], "whoop"))
        self.assertIsNone(morning_context.claim_analysis(first["recovery_date"], "whoop"))

    def test_restart_recovers_abandoned_analysis_lease(self):
        self.add_morning()
        self.ask_and_reply()
        start = dt.datetime(2026, 7, 10, 10, 0, tzinfo=dt.timezone.utc)
        self.assertIsNotNone(morning_context.claim_analysis("2026-07-10", "whoop", now=start))
        self.assertEqual(morning_context.recover_stale_analysis_claims(now=start + dt.timedelta(minutes=14)), 0)
        self.assertEqual(morning_context.recover_stale_analysis_claims(now=start + dt.timedelta(minutes=15)), 1)
        self.assertEqual(self.row()["status"], morning_context.STATUS_ANSWERED)

    def test_context_only_fallback_starts_at_23_kyiv_without_metrics(self):
        before = dt.datetime(2026, 7, 10, 22, 59, tzinfo=KYIV)
        cutoff = before.replace(hour=23, minute=0)
        self.assertIsNone(morning_readiness.analysis_mode("2026-07-10", now=before))
        self.assertEqual(morning_readiness.analysis_mode("2026-07-10", now=cutoff), "context_only")

    def test_corrupt_canonical_payload_never_becomes_context_only_fallback(self):
        target = dt.datetime(2026, 7, 10, 7, 0, tzinfo=KYIV).astimezone(
            dt.timezone.utc
        )
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO sleep VALUES (?,?,?,?)",
            ("broken", target.isoformat(), 90, "{not-json"),
        )
        conn.commit()
        conn.close()
        status = morning_readiness.morning_data_status("2026-07-10")
        self.assertEqual(status["error"], "canonical_payload_invalid")
        cutoff = dt.datetime(2026, 7, 10, 23, 0, tzinfo=KYIV)
        self.assertIsNone(
            morning_readiness.analysis_mode("2026-07-10", now=cutoff)
        )

    def test_missing_database_is_unknown_not_missing_whoop(self):
        missing = Path(self.temp.name) / "absent.db"
        status = morning_readiness.morning_data_status(
            "2026-07-10", db_path=missing
        )
        self.assertEqual(status["error"], "database_missing")
        self.assertFalse(status["ready"])

    def test_kyiv_boundary_attributes_utc_instant_to_local_morning(self):
        conn = sqlite3.connect(self.db)
        instant = "2026-07-09T21:30:00+00:00"  # 00:30 on July 10 in Kyiv
        conn.execute("INSERT INTO recovery VALUES (?,?,?,?,?)", (1, instant, 80, 60, 50))
        conn.execute("INSERT INTO sleep VALUES (?,?,?,?)", ("s", instant, 90, '{"nap": false}'))
        conn.commit()
        conn.close()
        self.assertTrue(morning_readiness.morning_data_status("2026-07-10")["ready"])
        self.assertFalse(morning_readiness.morning_data_status("2026-07-09")["ready"])


if __name__ == "__main__":
    unittest.main()
