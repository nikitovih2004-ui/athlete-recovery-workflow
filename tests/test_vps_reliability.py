import gzip
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import backup_database
import daily_log
import deploy
import generate_insights
import morning_context
import morning_flow
import morning_reporting
import release_manifest
import send_reminder
import telegram_bot
import weekly_report
import workouts_db


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Sftp:
    def __init__(self):
        self.created = []
        self.uploaded = []

    def stat(self, path):
        raise IOError

    def mkdir(self, path):
        self.created.append(path)

    def put(self, local_path, remote_path):
        self.uploaded.append((local_path, remote_path))


class DatabaseAndDeployTests(unittest.TestCase):
    def test_release_manifest_requires_clean_committed_tree_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = root / "telegram_bot.py"
            runtime.write_text("print('safe')\n", encoding="utf-8")
            with patch.object(
                release_manifest, "_git",
                side_effect=["", "a" * 40, "codex/release"],
            ):
                manifest = release_manifest.build(root, [runtime])
            self.assertEqual(manifest["git_commit"], "a" * 40)
            self.assertEqual(release_manifest.validate_tree(root, manifest), [])
            runtime.write_text("print('changed')\n", encoding="utf-8")
            self.assertEqual(release_manifest.validate_tree(root, manifest), ["telegram_bot.py"])
            with patch.object(release_manifest, "_git", return_value=" M telegram_bot.py"):
                with self.assertRaisesRegex(RuntimeError, "dirty"):
                    release_manifest.require_clean_committed_head(root)

    def test_release_lineage_accepts_forward_commit_and_rejects_downgrade(self):
        import subprocess

        forward = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(release_manifest.subprocess, "run", return_value=forward):
            release_manifest.require_descends_from(
                Path("."), "a" * 40, "b" * 40,
            )

        downgrade = subprocess.CompletedProcess([], 1, "", "")
        with patch.object(release_manifest.subprocess, "run", return_value=downgrade):
            with self.assertRaisesRegex(RuntimeError, "not a descendant"):
                release_manifest.require_descends_from(
                    Path("."), "a" * 40, "b" * 40,
                )

    def test_sqlite_writers_enable_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = str(Path(temp) / 'whoop.db')
            old_workouts = workouts_db.DB_PATH
            old_daily_log = daily_log.DB_PATH
            try:
                workouts_db.DB_PATH = db_path
                daily_log.DB_PATH = db_path
                conn = workouts_db.connect()
                try:
                    self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                    self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
                finally:
                    conn.close()
                conn = daily_log.connect()
                try:
                    self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                    self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
                finally:
                    conn.close()
            finally:
                workouts_db.DB_PATH = old_workouts
                daily_log.DB_PATH = old_daily_log

    def test_deployment_excludes_sensitive_local_artifacts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            included = root / 'fetch_data.py'
            excluded = [
                root / '.env',
                root / 'tokens.json',
                root / 'dashboard.html',
                root / 'data' / 'whoop.db',
                root / 'graphify-out' / 'graph.json',
                root / 'graphify-out' / 'cache' / 'example.json',
                root / '.local-state' / 'config.json',
                root / '.private-tools' / 'settings.json',
                root / '.agent-state' / 'state.json',
            ]
            for path in [included, *excluded]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}', encoding='utf-8')

            sftp = _Sftp()
            with patch.object(deploy, 'HERE', root):
                deploy.upload_project(sftp)

            uploaded = [remote_path for _, remote_path in sftp.uploaded]
            self.assertEqual(uploaded, ['/opt/whoop-workouts/fetch_data.py'])

    def test_remote_paths_are_posix_on_windows(self):
        sftp = _Sftp()
        deploy.ensure_remote_dir(sftp, '/opt/whoop-workouts/data')
        self.assertEqual(sftp.created, ['/opt', '/opt/whoop-workouts', '/opt/whoop-workouts/data'])


    def test_telegram_source_keys_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = str(Path(temp) / 'whoop.db')
            old_path = workouts_db.DB_PATH
            try:
                workouts_db.DB_PATH = db_path
                self.assertTrue(workouts_db.log_exercise('2026-07-10', 'press', 30, 1, 8, source_key='telegram:1:2:workout:0'))
                self.assertFalse(workouts_db.log_exercise('2026-07-10', 'press', 30, 1, 8, source_key='telegram:1:2:workout:0'))
                self.assertTrue(workouts_db.log_supplement('2026-07-10', '21:00', 'creatine', '5g', source_key='telegram:1:2:supplement:0'))
                self.assertFalse(workouts_db.log_supplement('2026-07-10', '21:00', 'creatine', '5g', source_key='telegram:1:2:supplement:0'))
                self.assertTrue(workouts_db.log_cardio('2026-07-10', '21:00', 'run', 20, 3, 140, 200, source_key='telegram:1:3:cardio'))
                self.assertFalse(workouts_db.log_cardio('2026-07-10', '21:00', 'run', 20, 3, 140, 200, source_key='telegram:1:3:cardio'))
                conn = workouts_db.connect()
                try:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM workout_exercises').fetchone()[0], 1)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM supplements_log').fetchone()[0], 1)
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM cardio_exercises').fetchone()[0], 1)
                finally:
                    conn.close()
            finally:
                workouts_db.DB_PATH = old_path

    def test_backup_is_readable_and_contains_database(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / 'whoop.db'
            source = sqlite3.connect(db_path)
            source.execute('CREATE TABLE sample (value TEXT)')
            source.execute("INSERT INTO sample VALUES ('ok')")
            source.commit()
            source.close()

            old_path = backup_database.DB_PATH
            try:
                backup_database.DB_PATH = db_path
                target = backup_database.create_backup(Path(temp) / 'backups', retain=2)
                self.assertTrue(target.exists())
                restored = Path(temp) / 'restored.db'
                with gzip.open(target, 'rb') as compressed, restored.open('wb') as raw:
                    raw.write(compressed.read())
                restored_conn = sqlite3.connect(restored)
                try:
                    self.assertEqual(restored_conn.execute('SELECT value FROM sample').fetchone()[0], 'ok')
                finally:
                    restored_conn.close()
            finally:
                backup_database.DB_PATH = old_path

    def test_failed_compression_never_publishes_partial_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "whoop.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE sample(value TEXT)")
            conn.commit()
            conn.close()
            destination = Path(temp) / "backups"
            old_path = backup_database.DB_PATH
            try:
                backup_database.DB_PATH = db_path
                with patch.object(
                    backup_database.shutil, "copyfileobj",
                    side_effect=OSError("simulated power loss"),
                ):
                    with self.assertRaisesRegex(OSError, "simulated power loss"):
                        backup_database.create_backup(destination, retain=2)
                self.assertEqual(list(destination.glob("whoop-*.db.gz")), [])
                self.assertEqual(list(destination.glob("whoop-*.json")), [])
            finally:
                backup_database.DB_PATH = old_path


    def test_morning_context_is_idempotent_and_keeps_existing_log(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = str(Path(temp) / "whoop.db")
            old_daily_log = daily_log.DB_PATH
            try:
                daily_log.DB_PATH = db_path
                context, needs_question = morning_context.ensure_request("2026-07-10")
                self.assertTrue(needs_question)
                self.assertEqual(context["evening_date"], "2026-07-09")
                self.assertIsNotNone(morning_context.claim_question("2026-07-10"))
                self.assertTrue(morning_context.mark_question_sent("2026-07-10", 900))
                _, needs_question_again = morning_context.ensure_request("2026-07-10")
                self.assertFalse(needs_question_again)

                conn = daily_log.connect()
                try:
                    daily_log.save_log(conn, "2026-07-09", {"notes": "ранняя запись"})
                finally:
                    conn.close()
                accepted = morning_context.accept_pending_reply(55, 900)
                self.assertEqual(accepted["recovery_date"], "2026-07-10")
                self.assertIsNone(morning_context.accept_pending_reply(55, 900))

                conn = daily_log.connect()
                try:
                    daily_log.append_log(conn, "2026-07-09", "магний и поздний ужин", label="Утренний контекст")
                    notes = daily_log.get_log(conn, "2026-07-09")["notes"]
                finally:
                    conn.close()
                self.assertIn("ранняя запись", notes)
                self.assertIn("магний и поздний ужин", notes)

                claimed = morning_context.claim_analysis("2026-07-10")
                self.assertEqual(claimed["evening_date"], "2026-07-09")
                self.assertIsNone(morning_context.claim_analysis("2026-07-10"))
                self.assertTrue(morning_context.complete_analysis("2026-07-10"))
            finally:
                daily_log.DB_PATH = old_daily_log

    def test_weekly_report_uses_complete_evening_to_next_morning_window(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = str(Path(temp) / "whoop.db")
            old_daily_log = daily_log.DB_PATH
            old_workouts = workouts_db.DB_PATH
            try:
                daily_log.DB_PATH = db_path
                workouts_db.DB_PATH = db_path
                conn = daily_log.connect()
                try:
                    conn.execute("CREATE TABLE recovery (created_at TEXT, recovery_score REAL, hrv_rmssd REAL, resting_hr REAL)")
                    conn.execute("CREATE TABLE sleep (end TEXT, raw_json TEXT, performance_pct REAL)")
                    for day in range(7, 21):
                        conn.execute(
                            "INSERT INTO recovery VALUES (?, ?, ?, ?)",
                            (f"2026-07-{day:02d}T07:00:00Z", 70, 60, 50),
                        )
                    for day in range(6, 13):
                        daily_log.save_log(conn, f"2026-07-{day:02d}", {"notes": "обычный вечер"})
                    conn.commit()
                finally:
                    conn.close()
                report = weekly_report.create_report("2026-07-12")
                self.assertIn("Еженедельный отчёт", report)
                self.assertIn("WHOOP-утра 7/7", report)
                self.assertTrue(weekly_report.is_weekly_context("2026-07-12"))
            finally:
                daily_log.DB_PATH = old_daily_log
                workouts_db.DB_PATH = old_workouts

class GeminiRequestTests(unittest.TestCase):
    def test_insights_relay_uses_typed_function_without_direct_api_key(self):
        with patch.dict(os.environ, {"GEMINI_TRANSPORT": "relay"}), \
             patch.object(generate_insights.gemini_transport, "relay_call_with_fallback") as call:
            call.return_value = ("grounded analysis", {"attempt_count": 1})
            result = generate_insights.generate_llm_insights(
                "synthetic report", "", "gemini-2.5-flash"
            )
        self.assertEqual(result, "grounded analysis")
        kwargs = call.call_args.kwargs
        self.assertEqual(
            kwargs["function_declarations"],
            [generate_insights.ANALYSIS_FUNCTION],
        )
        self.assertEqual(
            kwargs["allowed_function_names"], {"return_daily_analysis"}
        )
        self.assertEqual(
            kwargs["result_validator"]({
                "name": "return_daily_analysis",
                "args": {"analysis_markdown": "  safe report  "},
            }),
            "safe report",
        )
        with self.assertRaises(generate_insights.gemini_transport.RelayTransportError):
            kwargs["result_validator"]({"name": "unknown", "args": {}})

    def test_insights_use_header_for_api_key(self):
        response = _Response({'candidates': [{'content': {'parts': [{'text': 'ok'}]}}]})
        with patch.object(generate_insights.requests, 'post', return_value=response) as post:
            self.assertEqual(generate_insights.generate_llm_insights('report', 'api-key', 'gemini-3.5-flash'), 'ok')
        url, = post.call_args.args
        self.assertNotIn('api-key', url)
        self.assertEqual(post.call_args.kwargs['headers']['x-goog-api-key'], 'api-key')

    def test_bot_parser_uses_header_for_api_key(self):
        response = _Response({'candidates': [{'content': {'parts': [{'text': '{"workouts": [], "supplements": [], "date": null}'}]}}]})
        old_key = telegram_bot.GEMINI_KEY
        try:
            telegram_bot.GEMINI_KEY = 'api-key'
            with patch.object(telegram_bot.requests, 'post', return_value=response) as post:
                parsed = telegram_bot.parse_with_gemini('note')
            self.assertEqual(parsed['workouts'], [])
        finally:
            telegram_bot.GEMINI_KEY = old_key
        url, = post.call_args.args
        self.assertNotIn('api-key', url)
        self.assertEqual(post.call_args.kwargs['headers']['x-goog-api-key'], 'api-key')


class TelegramSecretRedactionTests(unittest.TestCase):
    def test_morning_flow_never_logs_bot_token_from_request_error(self):
        token = "123456:release-gate-secret"
        error = requests.ConnectionError(
            f"connection failed for https://api.telegram.org/bot{token}/sendMessage"
        )
        with patch.object(morning_flow, "TG_TOKEN", token), \
             patch.object(morning_flow, "TG_CHAT", "42"), \
             patch.object(morning_flow.requests, "post", side_effect=error), \
             patch.object(morning_flow, "log") as log:
            self.assertIsNone(morning_flow.send_telegram("health check"))

        logged = " ".join(str(call) for call in log.call_args_list)
        self.assertNotIn(token, logged)
        self.assertNotIn("api.telegram.org", logged)
        self.assertIn("ConnectionError", logged)

    def test_reminder_error_suppresses_token_and_exception_chaining(self):
        token = "654321:release-gate-secret"
        error = requests.ConnectionError(
            f"connection failed for https://api.telegram.org/bot{token}/sendMessage"
        )
        environment = {
            "TELEGRAM_BOT_TOKEN": token,
            "TELEGRAM_CHAT_ID": "42",
        }
        with patch.dict(os.environ, environment), \
             patch.object(send_reminder.requests, "post", side_effect=error), \
             patch("sys.argv", ["send_reminder.py"]), \
             self.assertRaises(RuntimeError) as raised:
            send_reminder.main()

        self.assertNotIn(token, str(raised.exception))
        self.assertNotIn("api.telegram.org", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)



class MorningReportTests(unittest.TestCase):
    def test_telegram_report_split_preserves_all_lines(self):
        text = ("строка\n" * 2000).strip()
        chunks = morning_reporting.split_telegram_text(text, limit=300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 300 for chunk in chunks))
        self.assertEqual(sum(chunk.count("строка") for chunk in chunks), 2000)

if __name__ == '__main__':
    unittest.main()
