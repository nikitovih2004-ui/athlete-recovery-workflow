"""Regression tests for safe deployment ordering; never connects to a VPS."""

import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import daily_log
import deploy
import morning_context


class DeployFlowTests(unittest.TestCase):
    @contextmanager
    def _flow(self, fail_at=None):
        events = []
        mocks = {}
        changed = [deploy.HERE / "morning_context.py"]
        results = {
            "upload_candidates": changed,
            "changed_uploads": changed,
            "run_backup": {
                "path": "/backup/whoop.db.gz",
                "sha256": "a" * 64,
                "size": 100,
                "timestamp": "20260712T120000Z",
            },
            "create_source_snapshot": {
                "root": "/backup/predeploy",
                "manifest": {"files": []},
            },
            "configure_service": False,
            "morning_context_row_count": 2,
            "daily_log_precheck": {"exists": True, "columns": ["date", "notes", "updated_at"]},
            "validate_migration": {
                "rows": 2,
                "quick_check": "ok",
                "columns": ["question_message_id", "question_claimed_at"],
                "index_list": [{"name": "idx_morning_context_question_message_id", "unique": True}],
                "index_sql": (
                    "CREATE UNIQUE INDEX idx_morning_context_question_message_id "
                    "ON morning_context(question_message_id) "
                    "WHERE question_message_id IS NOT NULL"
                ),
            },
            "validate_data_integrity": {"ok": True, "issues": []},
            "validate_release_lineage": {
                "deployed_commit": "a" * 40,
                "candidate_commit": "b" * 40,
            },
            "validate_analysis_contract": {"ok": True},
            "restart_service": "2026-07-12T12:00:00+00:00",
        }
        names = [
            "preflight",
            "upload_candidates",
            "validate_release_lineage",
            "changed_uploads",
            "run_backup",
            "create_source_snapshot",
            "upload_project",
            "provision",
            "configure_service",
            "stop_service",
            "suspend_project_cron",
            "assert_no_project_writers",
            "morning_context_row_count",
            "upload_release_manifest",
            "verify_remote_manifest",
            "daily_log_precheck",
            "run_migration",
            "validate_migration",
            "validate_data_integrity",
            "validate_analysis_contract",
            "restart_service",
            "validate_service_health",
            "build_dashboard",
            "configure_cron",
            "verify_service_execstart",
            "rollback_sources",
            "recover_previous_service",
        ]

        def effect(name):
            def call(*args, **kwargs):
                events.append(name)
                if name == fail_at:
                    raise RuntimeError(f"{name} failed")
                return results.get(name)
            return call

        with ExitStack() as stack:
            mocks["build_manifest"] = stack.enter_context(
                patch.object(
                    deploy.release_manifest, "build",
                    side_effect=effect("build_manifest"),
                )
            )
            for name in names:
                mocks[name] = stack.enter_context(
                    patch.object(deploy, name, side_effect=effect(name))
                )
            yield events, mocks

    def test_success_order_is_backup_stop_upload_migrate_validate_restart(self):
        with self._flow() as (events, _):
            deploy.deploy_project(object(), object())
        expected = [
            "preflight",
            "upload_candidates",
            "build_manifest",
            "validate_release_lineage",
            "changed_uploads",
            "run_backup",
            "create_source_snapshot",
            "stop_service",
            "suspend_project_cron",
            "assert_no_project_writers",
            "upload_project",
            "upload_release_manifest",
            "verify_remote_manifest",
            "provision",
            "configure_service",
            "verify_service_execstart",
            "morning_context_row_count",
            "daily_log_precheck",
            "run_migration",
            "validate_migration",
            "validate_data_integrity",
            "validate_analysis_contract",
            "restart_service",
            "validate_service_health",
            "build_dashboard",
            "configure_cron",
        ]
        self.assertEqual(events, expected)
        self.assertLess(events.index("validate_release_lineage"), events.index("run_backup"))
        self.assertLess(events.index("stop_service"), events.index("upload_project"))
        self.assertLess(events.index("daily_log_precheck"), events.index("run_migration"))
        self.assertLess(events.index("validate_data_integrity"), events.index("restart_service"))
        self.assertLess(events.index("validate_analysis_contract"), events.index("restart_service"))
        self.assertLess(events.index("run_migration"), events.index("restart_service"))

    def test_migration_failure_rolls_back_and_recovers_old_service(self):
        with self._flow(fail_at="run_migration") as (events, mocks):
            with self.assertRaisesRegex(RuntimeError, "run_migration failed"):
                deploy.deploy_project(object(), object())
        mocks["restart_service"].assert_not_called()
        self.assertEqual(events[-3:], ["rollback_sources", "recover_previous_service", "configure_cron"])

    def test_validation_failure_never_restarts_new_code_or_restores_database(self):
        with self._flow(fail_at="validate_migration") as (events, mocks):
            with self.assertRaisesRegex(RuntimeError, "validate_migration failed"):
                deploy.deploy_project(object(), object())
        mocks["restart_service"].assert_not_called()
        self.assertIn("rollback_sources", events)
        self.assertFalse(hasattr(deploy, "restore_database"))

    def test_data_integrity_failure_never_restarts_new_code(self):
        with self._flow(fail_at="validate_data_integrity") as (events, mocks):
            with self.assertRaisesRegex(RuntimeError, "validate_data_integrity failed"):
                deploy.deploy_project(object(), object())
        mocks["restart_service"].assert_not_called()
        self.assertIn("rollback_sources", events)

    def test_analysis_contract_failure_never_restarts_new_code(self):
        with self._flow(fail_at="validate_analysis_contract") as (events, mocks):
            with self.assertRaisesRegex(RuntimeError, "validate_analysis_contract failed"):
                deploy.deploy_project(object(), object())
        mocks["restart_service"].assert_not_called()
        self.assertIn("rollback_sources", events)

    def test_analysis_contract_gate_checks_mapping_fields_and_current_exclusion(self):
        payload = {
            "ok": True,
            "mapping": {
                "recovery": ["recovery_score", "%"],
                "hrv": ["hrv_rmssd", "ms"],
                "rhr": ["resting_hr", "bpm"],
                "sleep_duration": ["sleep_hours", "h"],
                "sleep_performance": ["sleep_performance", "%"],
            },
            "fields": ["metric", "unit"],
        }
        with patch.object(
            deploy, "_remote_python", return_value=__import__("json").dumps(payload)
        ) as remote:
            self.assertEqual(deploy.validate_analysis_contract(object()), payload)
        code = remote.call_args.args[1]
        self.assertIn("unknown metric mapping", code)
        self.assertIn("missing prompt contract fields", code)
        self.assertIn("current value entered baseline period", code)
        self.assertTrue(remote.call_args.kwargs["as_service_user"])

    def test_validate_data_integrity_accepts_clean_read_only_report(self):
        payload = {
            "ok": True, "quick_check": "ok", "issues": [],
            "database_uuid": "db-uuid", "generation": 1,
        }
        with patch.object(
            deploy, "_remote_python", return_value=__import__("json").dumps(payload)
        ) as remote:
            self.assertEqual(deploy.validate_data_integrity(object()), payload)
        self.assertTrue(remote.call_args.kwargs["as_service_user"])

    def test_validate_data_integrity_rejects_action_domain_mismatch(self):
        payload = {
            "ok": False, "quick_check": "ok",
            "issues": [{"code": "missing_domain_row"}],
        }
        with patch.object(
            deploy, "_remote_python", return_value=__import__("json").dumps(payload)
        ):
            with self.assertRaisesRegex(RuntimeError, "missing_domain_row"):
                deploy.validate_data_integrity(object())

    def test_release_lineage_rejects_missing_manifest(self):
        class MissingManifestSFTP:
            def file(self, *_args, **_kwargs):
                raise IOError("missing")

        with self.assertRaisesRegex(RuntimeError, "manifest is unavailable"):
            deploy.validate_release_lineage(
                MissingManifestSFTP(), {"git_commit": "b" * 40},
            )

    def test_release_lineage_checks_remote_commit_before_upload(self):
        import io
        import json

        class ManifestSFTP:
            def file(self, *_args, **_kwargs):
                payload = json.dumps({"git_commit": "a" * 40}).encode()
                return io.BytesIO(payload)

        with patch.object(
            deploy.release_manifest, "require_descends_from",
        ) as require:
            result = deploy.validate_release_lineage(
                ManifestSFTP(), {"git_commit": "b" * 40},
            )
        require.assert_called_once_with(deploy.HERE, "a" * 40, "b" * 40)
        self.assertEqual(result["deployed_commit"], "a" * 40)

    def test_restart_failure_rolls_back_sources(self):
        with self._flow(fail_at="restart_service") as (events, _):
            with self.assertRaisesRegex(RuntimeError, "restart_service failed"):
                deploy.deploy_project(object(), object())
        self.assertEqual(events[-3:], ["rollback_sources", "recover_previous_service", "configure_cron"])

    def test_stop_failure_never_runs_migration_and_recovers_old_service(self):
        with self._flow(fail_at="stop_service") as (events, mocks):
            with self.assertRaisesRegex(RuntimeError, "stop_service failed"):
                deploy.deploy_project(object(), object())
        mocks["daily_log_precheck"].assert_not_called()
        mocks["run_migration"].assert_not_called()
        mocks["upload_project"].assert_not_called()
        mocks["recover_previous_service"].assert_called_once()

    def test_running_cron_writer_blocks_upload_and_cron_is_restored(self):
        with self._flow(fail_at="assert_no_project_writers") as (events, mocks):
            with self.assertRaisesRegex(RuntimeError, "assert_no_project_writers failed"):
                deploy.deploy_project(object(), object())
        mocks["upload_project"].assert_not_called()
        self.assertEqual(
            events[-2:], ["recover_previous_service", "configure_cron"],
        )

    def test_writer_probe_does_not_match_its_own_shell_wrapper(self):
        captured = []
        with patch.object(deploy, "run", side_effect=lambda _client, command: captured.append(command)):
            deploy.assert_no_project_writers(object())
        command = captured[0]
        self.assertIn("[m]orning_flow.py", command)
        self.assertIn("[s]end_reminder.py", command)
        self.assertIn("[b]ackup_database.py", command)
        self.assertIn("[w]eekly_report.py", command)
        self.assertNotIn("'morning_flow.py|send_reminder.py", command)

    def test_manual_rollback_suspends_writers_and_restores_cron(self):
        events = []
        manifest = b'{"files":[]}'

        class _RemoteFile:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return manifest

        class _SFTP:
            def file(self, _path, _mode):
                return _RemoteFile()

        names = [
            "stop_service",
            "suspend_project_cron",
            "assert_no_project_writers",
            "rollback_sources",
            "verify_remote_manifest",
            "verify_service_execstart",
            "recover_previous_service",
            "configure_cron",
        ]
        with ExitStack() as stack:
            for name in names:
                stack.enter_context(
                    patch.object(
                        deploy, name,
                        side_effect=lambda *_args, _name=name, **_kwargs: events.append(_name),
                    )
                )
            deploy.rollback_project(object(), _SFTP(), "/backup/predeploy")

        self.assertEqual(events, names)
        self.assertLess(events.index("assert_no_project_writers"), events.index("rollback_sources"))
        self.assertLess(events.index("recover_previous_service"), events.index("configure_cron"))

    def test_manual_rollback_failure_recovers_service_and_cron(self):
        events = []
        manifest = b'{"files":[]}'

        class _RemoteFile:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return manifest

        class _SFTP:
            def file(self, _path, _mode):
                return _RemoteFile()

        def record(name, *, fail=False):
            def call(*_args, **_kwargs):
                events.append(name)
                if fail:
                    raise RuntimeError("restore failed")
            return call

        with ExitStack() as stack:
            for name in (
                "stop_service", "suspend_project_cron", "assert_no_project_writers",
                "verify_remote_manifest", "verify_service_execstart",
                "recover_previous_service", "configure_cron",
            ):
                stack.enter_context(patch.object(deploy, name, side_effect=record(name)))
            stack.enter_context(
                patch.object(deploy, "rollback_sources", side_effect=record("rollback_sources", fail=True))
            )
            with self.assertRaisesRegex(RuntimeError, "restore failed"):
                deploy.rollback_project(object(), _SFTP(), "/backup/predeploy")

        self.assertEqual(
            events[-3:],
            ["rollback_sources", "recover_previous_service", "configure_cron"],
        )

    def test_precheck_failure_never_runs_migration_and_recovers_old_service(self):
        with self._flow(fail_at="daily_log_precheck") as (events, mocks):
            with self.assertRaisesRegex(RuntimeError, "daily_log_precheck failed"):
                deploy.deploy_project(object(), object())
        mocks["run_migration"].assert_not_called()
        mocks["restart_service"].assert_not_called()
        self.assertEqual(events[-3:], ["rollback_sources", "recover_previous_service", "configure_cron"])

    def test_health_failure_rolls_back_sources(self):
        with self._flow(fail_at="validate_service_health") as (events, _):
            with self.assertRaisesRegex(RuntimeError, "validate_service_health failed"):
                deploy.deploy_project(object(), object())
        self.assertEqual(events[-3:], ["rollback_sources", "recover_previous_service", "configure_cron"])

    def test_backup_failure_does_not_upload_or_stop_service(self):
        with self._flow(fail_at="run_backup") as (_, mocks):
            with self.assertRaisesRegex(RuntimeError, "run_backup failed"):
                deploy.deploy_project(object(), object())
        mocks["upload_project"].assert_not_called()
        mocks["stop_service"].assert_not_called()
        mocks["rollback_sources"].assert_not_called()

    def test_primary_error_is_not_hidden_by_rollback_error(self):
        with self._flow(fail_at="run_migration") as (_, mocks):
            mocks["rollback_sources"].side_effect = RuntimeError("rollback failed")
            with self.assertRaisesRegex(RuntimeError, "run_migration failed.*rollback failed"):
                deploy.deploy_project(object(), object())

    def test_predeploy_snapshot_always_contains_generated_dashboard(self):
        class SFTP:
            def stat(self, path):
                return object()

        written = {}
        with patch.object(deploy, "ensure_remote_dir"), \
             patch.object(deploy, "_copy_remote_file") as copy, \
             patch.object(
                 deploy, "write_remote_file",
                 side_effect=lambda _sftp, path, content: written.update(
                     {path: __import__("json").loads(content)}
                 ),
             ):
            snapshot = deploy.create_source_snapshot(
                SFTP(), [], "20260724T190000Z"
            )
        dashboard_entry = next(
            entry for entry in snapshot["manifest"]["files"]
            if entry["path"] == "dashboard.html"
        )
        self.assertTrue(dashboard_entry["existed"])
        self.assertTrue(any(
            args[1].endswith("/dashboard.html")
            and args[2].endswith("/files/dashboard.html")
            for args, _ in copy.call_args_list
        ))
        self.assertIn(
            "/opt/whoop-workouts/data/backups/predeploy_20260724T190000Z/manifest.json",
            written,
        )

    _GOOD_INDEX_SQL = (
        "CREATE UNIQUE INDEX idx_morning_context_question_message_id "
        "ON morning_context(question_message_id) "
        "WHERE question_message_id IS NOT NULL"
    )

    def _validate_payload(self, **overrides):
        phase2 = {
            "conversation_actions": {
                "columns": ["response_kind", "response_text", "reply_delivery_status",
                            "reply_message_id", "reply_attempt_count",
                            "response_claimed_at", "processing_token", "processing_fence",
                            "processing_claimed_at", "processing_claim_expires_at"],
                "table_info": [
                    {"name": "response_kind", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                    {"name": "response_text", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                    {"name": "reply_delivery_status", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                    {"name": "reply_message_id", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                    {"name": "reply_attempt_count", "type": "INTEGER", "notnull": True, "default": "0", "pk": 0},
                    {"name": "response_claimed_at", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                    {"name": "processing_token", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                    {"name": "processing_fence", "type": "INTEGER", "notnull": True, "default": "0", "pk": 0},
                    {"name": "processing_claimed_at", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                    {"name": "processing_claim_expires_at", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                ],
                "indexes": [],
                "sql": "CREATE TABLE conversation_actions (...)"
            },
            "pending_actions": {
                "columns": ["claimed_by_action_id", "claimed_at", "claim_expires_at"],
                "table_info": [
                    {"name": "claimed_by_action_id", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                    {"name": "claimed_at", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                    {"name": "claim_expires_at", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                ],
                "indexes": [{"name": "idx_pending_actions_claim_expiry",
                             "unique": False, "columns": ["status", "claim_expires_at"]}],
                "sql": "CREATE TABLE pending_actions (...)"
            },
            "conversation_sessions": {
                "columns": ["source", "chat_id", "user_id", "state_json", "version",
                            "expires_at", "created_at", "updated_at"],
                "table_info": [
                    {"name": "source", "type": "TEXT", "notnull": True, "default": None, "pk": 1},
                    {"name": "chat_id", "type": "TEXT", "notnull": True, "default": None, "pk": 2},
                    {"name": "user_id", "type": "TEXT", "notnull": True, "default": "''", "pk": 3},
                    {"name": "state_json", "type": "TEXT", "notnull": True, "default": None, "pk": 0},
                    {"name": "version", "type": "INTEGER", "notnull": True, "default": "1", "pk": 0},
                    {"name": "expires_at", "type": "TEXT", "notnull": True, "default": None, "pk": 0},
                    {"name": "created_at", "type": "TEXT", "notnull": True, "default": None, "pk": 0},
                    {"name": "updated_at", "type": "TEXT", "notnull": True, "default": None, "pk": 0},
                ],
                "indexes": [{"name": "idx_conversation_sessions_expiry",
                             "unique": False, "columns": ["expires_at"]}],
                "sql": "CREATE TABLE conversation_sessions (version INTEGER CHECK(version >= 1), state_json TEXT CHECK(length(CAST(state_json AS BLOB)) <= 8192))"
            },
            "daily_factor_observations": {
                "columns": ["observation_id", "context_date", "factor_key", "state",
                            "extractor_version", "confidence", "source_key",
                            "created_at", "updated_at", "job_id", "projection_hash",
                            "projection_revision", "is_current"],
                "table_info": [
                    {"name": "observation_id", "type": "TEXT", "notnull": False, "default": None, "pk": 1},
                    {"name": "context_date", "type": "TEXT", "notnull": True, "default": None, "pk": 0},
                    {"name": "factor_key", "type": "TEXT", "notnull": True, "default": None, "pk": 0},
                    {"name": "state", "type": "INTEGER", "notnull": True, "default": None, "pk": 0},
                    {"name": "extractor_version", "type": "TEXT", "notnull": True, "default": None, "pk": 0},
                    {"name": "confidence", "type": "REAL", "notnull": False, "default": None, "pk": 0},
                    {"name": "source_key", "type": "TEXT", "notnull": True, "default": None, "pk": 0},
                    {"name": "created_at", "type": "TEXT", "notnull": True, "default": None, "pk": 0},
                    {"name": "updated_at", "type": "TEXT", "notnull": True, "default": None, "pk": 0},
                    {"name": "job_id", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                    {"name": "projection_hash", "type": "TEXT", "notnull": False, "default": None, "pk": 0},
                    {"name": "projection_revision", "type": "INTEGER", "notnull": False, "default": None, "pk": 0},
                    {"name": "is_current", "type": "INTEGER", "notnull": True, "default": "1", "pk": 0},
                ],
                "indexes": [
                    {"name": "idx_daily_factor_date_key", "unique": False,
                      "columns": ["context_date", "factor_key"]},
                    {"name": "idx_daily_factor_current", "unique": False,
                     "columns": ["context_date", "factor_key", "is_current"]},
                    {"name": "sqlite_autoindex_daily_factor_observations_2", "unique": True,
                     "columns": ["source_key"]},
                ],
                "sql": "CREATE TABLE daily_factor_observations (state INTEGER CHECK(state IN (0, 1)), confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)))"
            },
            "factor_extraction_jobs": {
                "columns": ["job_id", "context_date", "projection_hash", "projection_revision",
                            "extractor_version", "origin_action_id", "source_key", "status",
                            "attempt_count", "available_at", "lease_token", "lease_expires_at",
                            "last_error_code", "created_at", "updated_at", "completed_at"],
                "table_info": [],
                "indexes": [
                    {"name": "idx_factor_jobs_ready", "unique": False,
                     "columns": ["status", "available_at", "lease_expires_at"]},
                    {"name": "idx_factor_jobs_date_revision", "unique": False,
                     "columns": ["context_date", "projection_revision"]},
                ],
                "sql": "CREATE TABLE factor_extraction_jobs (...)"
            },
            "daily_context_entries": {
                "columns": ["entry_id", "context_date", "notes", "label", "source_key",
                            "origin_action_id", "revision", "status", "supersedes_entry_id",
                            "status_action_id", "content_sha256", "created_at", "updated_at"],
                "table_info": [],
                "indexes": [{"name": "idx_daily_context_entries_date_status",
                             "unique": False,
                             "columns": ["context_date", "status", "revision", "entry_id"]}],
                "sql": "CREATE TABLE daily_context_entries (...)"
            },
            "daily_context_projection_state": {
                "columns": ["context_date", "projection_hash", "revision", "updated_at"],
                "table_info": [], "indexes": [],
                "sql": "CREATE TABLE daily_context_projection_state (...)"
            },
        }
        payload = {
            "columns": [
                "recovery_date", "question_message_id", "question_claimed_at",
                "analysis_mode", "analysis_claimed_at", "analysis_available_at",
                "analysis_attempt_count",
            ],
            "index_list": [{"name": "idx_morning_context_question_message_id", "unique": True}],
            "index_sql": self._GOOD_INDEX_SQL,
            "tables": ["morning_context", "conversation_actions", "pending_actions",
                       "conversation_sessions", "daily_factor_observations",
                       "factor_extraction_jobs", "daily_context_entries",
                       "daily_context_projection_state", "report_deliveries",
                       "morning_pipeline_events"],
            "rows": 2,
            "quick_check": "ok",
            "phase2": phase2,
            "cardio_columns": {
                "strain": "REAL", "max_hr": "INTEGER", "steps": "INTEGER",
            },
            "report_delivery_columns": [
                "delivery_key", "report_kind", "payload", "payload_sha256",
                "total_chunks", "next_chunk", "status", "claim_token",
                "claimed_at", "delivered_at", "created_at", "updated_at",
            ],
            "report_delivery_indexes": ["idx_report_deliveries_status"],
            "morning_observability_columns": [
                "event_id", "pipeline_date", "run_id", "stage",
                "started_at", "finished_at", "outcome", "reason",
                "duration_ms", "details_json",
            ],
            "morning_observability_indexes": [
                "idx_morning_pipeline_date_stage",
                "idx_morning_pipeline_run",
            ],
            "correction_trigger_sql": (
                "CREATE TRIGGER trg_conversation_actions_success_immutable "
                "BEFORE UPDATE ON conversation_actions "
                "WHEN OLD.tool_name IN ('log_cardio','correct_activity')"
            ),
        }
        payload.update(overrides)
        return payload

    def test_migration_validation_checks_columns_index_rows_and_integrity(self):
        payload = self._validate_payload()
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            self.assertEqual(deploy.validate_migration(object(), 2), payload)

    def test_validation_rejects_changed_row_count(self):
        payload = self._validate_payload(rows=1)
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            with self.assertRaisesRegex(RuntimeError, "row count changed"):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_missing_cardio_extraction_columns(self):
        payload = self._validate_payload(cardio_columns={"strain": "REAL"})
        with patch.object(
            deploy, "_remote_python",
            return_value=__import__("json").dumps(payload),
        ):
            with self.assertRaisesRegex(RuntimeError, "cardio extraction columns"):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_unprotected_correction_actions(self):
        payload = self._validate_payload(
            correction_trigger_sql="CREATE TRIGGER old_trigger"
        )
        with patch.object(
            deploy, "_remote_python",
            return_value=__import__("json").dumps(payload),
        ):
            with self.assertRaisesRegex(RuntimeError, "correction actions"):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_non_unique_index(self):
        payload = self._validate_payload(
            index_list=[{"name": "idx_morning_context_question_message_id", "unique": False}],
        )
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            with self.assertRaisesRegex(RuntimeError, "not UNIQUE"):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_index_on_wrong_column(self):
        payload = self._validate_payload(
            index_sql=(
                "CREATE UNIQUE INDEX idx_morning_context_question_message_id "
                "ON morning_context(recovery_date) "
                "WHERE question_message_id IS NOT NULL"
            ),
        )
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            with self.assertRaisesRegex(RuntimeError, "not on question_message_id"):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_non_partial_index(self):
        payload = self._validate_payload(
            index_sql=(
                "CREATE UNIQUE INDEX idx_morning_context_question_message_id "
                "ON morning_context(question_message_id)"
            ),
        )
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            with self.assertRaisesRegex(RuntimeError, "partial index"):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_missing_index(self):
        payload = self._validate_payload(index_list=[], index_sql=None)
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            with self.assertRaisesRegex(RuntimeError, "expected index is missing"):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_incomplete_phase2_schema(self):
        payload = self._validate_payload()
        payload["phase2"]["conversation_sessions"]["columns"].remove("expires_at")
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            with self.assertRaisesRegex(RuntimeError, "conversation_sessions columns"):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_incomplete_morning_observability_schema(self):
        payload = self._validate_payload(
            morning_observability_columns=["event_id", "pipeline_date"]
        )
        with patch.object(
            deploy, "_remote_python",
            return_value=__import__("json").dumps(payload),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "morning observability columns"
            ):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_missing_morning_observability_index(self):
        payload = self._validate_payload(
            morning_observability_indexes=[
                "idx_morning_pipeline_date_stage"
            ]
        )
        with patch.object(
            deploy, "_remote_python",
            return_value=__import__("json").dumps(payload),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "morning observability indexes"
            ):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_missing_durable_report_delivery_schema(self):
        payload = self._validate_payload(report_delivery_columns=[])
        with patch.object(
            deploy, "_remote_python",
            return_value=__import__("json").dumps(payload),
        ):
            with self.assertRaisesRegex(RuntimeError, "report delivery columns"):
                deploy.validate_migration(object(), 2)

    def test_validation_requires_unique_factor_source_key(self):
        payload = self._validate_payload()
        payload["phase2"]["daily_factor_observations"]["indexes"] = [
            {"name": "idx_daily_factor_date_key", "unique": False,
             "columns": ["context_date", "factor_key"]},
            {"name": "idx_daily_factor_current", "unique": False,
             "columns": ["context_date", "factor_key", "is_current"]},
        ]
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            with self.assertRaisesRegex(RuntimeError, "source_key is not uniquely indexed"):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_phase2_notnull_or_pk_mismatch(self):
        payload = self._validate_payload()
        payload["phase2"]["conversation_sessions"]["table_info"][0]["notnull"] = False
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            with self.assertRaisesRegex(RuntimeError, "exact schema mismatch"):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_phase2_index_on_wrong_columns(self):
        payload = self._validate_payload()
        payload["phase2"]["pending_actions"]["indexes"][0]["columns"] = ["status"]
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            with self.assertRaisesRegex(RuntimeError, "exact shape mismatch"):
                deploy.validate_migration(object(), 2)

    def test_validation_rejects_missing_phase2_check_constraint(self):
        payload = self._validate_payload()
        payload["phase2"]["conversation_sessions"]["sql"] = (
            "CREATE TABLE conversation_sessions (version INTEGER, state_json TEXT)"
        )
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            with self.assertRaisesRegex(RuntimeError, "session CHECK mismatch"):
                deploy.validate_migration(object(), 2)

    def test_daily_log_precheck_passes_when_columns_match(self):
        payload = {"exists": True, "columns": ["date", "notes", "updated_at"]}
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            self.assertEqual(deploy.daily_log_precheck(object()), payload)

    def test_daily_log_precheck_passes_when_table_absent(self):
        payload = {"exists": False, "columns": []}
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            self.assertEqual(deploy.daily_log_precheck(object()), payload)

    def test_daily_log_precheck_rejects_schema_mismatch(self):
        payload = {"exists": True, "columns": ["date", "value", "updated_at"]}
        with patch.object(deploy, "_remote_python", return_value=__import__("json").dumps(payload)):
            with self.assertRaisesRegex(RuntimeError, "daily_log precheck failed"):
                deploy.daily_log_precheck(object())

    def test_remote_python_changes_into_remote_dir_before_import(self):
        captured = {}

        def fake_run(client, command):
            captured["command"] = command
            return ""

        with patch.object(deploy, "run", side_effect=fake_run):
            deploy._remote_python(object(), "import morning_context")
        self.assertTrue(captured["command"].startswith(f"cd {deploy.REMOTE_DIR} && "))
        self.assertIn(f"{deploy.REMOTE_DIR}/venv/bin/python", captured["command"])

    def test_remote_python_as_service_user_still_changes_dir_first(self):
        captured = {}

        def fake_run(client, command):
            captured["command"] = command
            return ""

        with patch.object(deploy, "run", side_effect=fake_run):
            deploy._remote_python(object(), "import conversation_store", as_service_user=True)
        command = captured["command"]
        self.assertTrue(command.startswith(f"cd {deploy.REMOTE_DIR} && "))
        self.assertIn("runuser", command)
        self.assertLess(command.index("cd "), command.index("runuser"))

    def test_restart_service_captures_epoch_timestamp_before_restart(self):
        captured = []

        def fake_run(client, command):
            captured.append(command)
            if command == "date -u +%s":
                return "1752320444"
            return ""

        with patch.object(deploy, "run", side_effect=fake_run):
            since = deploy.restart_service(object())
        self.assertEqual(since, "1752320444")
        self.assertIn("date -u +%s", captured)
        self.assertIn("systemctl restart whoop-bot.service", captured)
        # Timestamp must be captured before the restart, or a log line right
        # at the boundary could be missed by the later --since filter.
        self.assertLess(
            captured.index("date -u +%s"),
            captured.index("systemctl restart whoop-bot.service"),
        )

    def test_restart_service_rejects_non_numeric_date_output(self):
        def fake_run(client, command):
            if command == "date -u +%s":
                return "Failed to parse timestamp"
            return ""

        with patch.object(deploy, "run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "Unexpected timestamp"):
                deploy.restart_service(object())

    def test_since_format_is_not_the_previously_rejected_iso8601_offset_shape(self):
        # Regression test for the confirmed production failure: journalctl
        # rejected "2026-07-12T11:20:44+00:00" (date -u --iso-8601=seconds)
        # with "Failed to parse timestamp". The new format must not look
        # like that, and must be plain digits (epoch seconds).
        def fake_run(client, command):
            if command == "date -u +%s":
                return "1752320444"
            return ""

        with patch.object(deploy, "run", side_effect=fake_run):
            since = deploy.restart_service(object())
        self.assertNotRegex(since, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        self.assertRegex(since, r"^\d+$")

    def test_validate_service_health_uses_utc_epoch_since_and_quotes_it(self):
        captured = []

        def fake_run(client, command):
            captured.append(command)
            if "NRestarts" in command:
                return "0"
            return ""

        with patch.object(deploy, "run", side_effect=fake_run):
            deploy.validate_service_health(object(), "1752320444")
        journalctl_cmd = next(c for c in captured if c.startswith("journalctl"))
        # shlex.quote leaves "@<digits>" unquoted -- '@' and digits are both
        # in its safe/unquoted character set, so this is correct as-is.
        self.assertEqual(
            journalctl_cmd,
            "journalctl -u whoop-bot.service --utc --since @1752320444 --no-pager -o cat",
        )

    def test_validate_service_health_shell_quotes_hostile_since_value(self):
        # since_epoch normally comes from restart_service's own digit-checked
        # output, but validate_service_health must not trust its argument
        # blindly — an unexpected value must still be safely quoted, never
        # interpolated raw into the remote shell command.
        captured = []

        def fake_run(client, command):
            captured.append(command)
            if "NRestarts" in command:
                return "0"
            return ""

        with patch.object(deploy, "run", side_effect=fake_run):
            deploy.validate_service_health(object(), "1; rm -rf /")
        journalctl_cmd = next(c for c in captured if c.startswith("journalctl"))
        self.assertEqual(
            journalctl_cmd,
            "journalctl -u whoop-bot.service --utc --since '@1; rm -rf /' --no-pager -o cat",
        )

    def test_validate_service_health_still_detects_restart_loop(self):
        counts = iter(["0", "1"])  # before, after -> mismatch

        def fake_run(client, command):
            if "NRestarts" in command:
                return next(counts)
            return ""

        with patch.object(deploy, "run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "restart loop detected"):
                deploy.validate_service_health(object(), "1752320444")

    def test_validate_service_health_still_detects_traceback(self):
        def fake_run(client, command):
            if "NRestarts" in command:
                return "0"
            if command.startswith("journalctl"):
                return "Traceback (most recent call last):\n  File ..."
            return ""

        with patch.object(deploy, "run", side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, "traceback in recent journal"):
                deploy.validate_service_health(object(), "1752320444")

    def test_underlying_migration_is_idempotent_and_preserves_legacy_row(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "whoop.db")
            conn = sqlite3.connect(path)
            conn.execute(
                """CREATE TABLE morning_context (
                    recovery_date TEXT PRIMARY KEY,
                    evening_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    asked_at TEXT,
                    reminded_at TEXT,
                    replied_at TEXT,
                    analyzed_at TEXT,
                    source_message_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                "INSERT INTO morning_context "
                "(recovery_date, evening_date, status, created_at, updated_at) "
                "VALUES ('2026-07-10', '2026-07-09', 'analyzed', 't0', 't0')"
            )
            conn.commit()
            old_path = daily_log.DB_PATH
            try:
                daily_log.DB_PATH = path
                morning_context.ensure_table(conn)
                morning_context.ensure_table(conn)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(morning_context)")}
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(morning_context)")}
                rows = conn.execute("SELECT COUNT(*) FROM morning_context").fetchone()[0]
            finally:
                daily_log.DB_PATH = old_path
                conn.close()
        self.assertTrue({"question_message_id", "question_claimed_at"}.issubset(columns))
        self.assertIn("idx_morning_context_question_message_id", indexes)
        self.assertEqual(rows, 1)

    def test_cron_content_is_idempotent_and_has_no_duplicates(self):
        first = deploy.cron_content()
        second = deploy.cron_content()
        lines = [line for line in first.splitlines() if line]
        self.assertEqual(first, second)
        self.assertEqual(len(lines), len(set(lines)))
        self.assertEqual(sum("morning_flow.py" in line for line in lines), 1)
        self.assertTrue(any(line.startswith("7,22,37,52 6-23 * * *") and
                            "morning_flow.py" in line for line in lines))
        self.assertEqual(sum("send_reminder.py" in line for line in lines), 1)
        self.assertEqual(sum("backup_database.py" in line for line in lines), 1)

    def test_generated_dashboard_is_hashed_and_checked_for_sheet_contracts(self):
        payload = '{"sha256":"' + ('a' * 64) + '","size":473154}'
        with patch.object(deploy, "_remote_python", return_value=payload) as remote:
            result = deploy.validate_dashboard_artifact(object())
        self.assertEqual(result["size"], 473154)
        code = remote.call_args.args[1]
        self.assertIn("import dashboard_contract", code)
        self.assertIn("dashboard_contract.validate_artifact(text)", code)
        self.assertTrue(remote.call_args.kwargs["as_service_user"])

    def test_existing_exclusions_and_graphify_exclusion_remain(self):
        excluded = [
            deploy.HERE / ".env",
            deploy.HERE / "tokens.json",
            deploy.HERE / "dashboard.html",
            deploy.HERE / "data" / "whoop.db",
            deploy.HERE / ".codex" / "state.json",
            deploy.HERE / "graphify-out" / "graph.json",
            deploy.HERE / "PHASE2_SESSION_HANDOFF.md",
        ]
        self.assertTrue(all(deploy.is_excluded(path) for path in excluded))
        self.assertFalse(deploy.is_excluded(deploy.HERE / "morning_context.py"))


if __name__ == "__main__":
    unittest.main()
