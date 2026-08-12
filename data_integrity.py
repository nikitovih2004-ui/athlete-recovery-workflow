"""Read-only integrity audit for activity/supplement persistence.

The auditor never creates or migrates schema and never repairs data. Existing
legacy rows without ``origin_action_id`` are valid, but every historical
successful mutation is still checked against the record ids persisted in its
audit result. Full action/link provenance is required after integrity schema
installation.
"""
from __future__ import annotations

import json
import os
import sqlite3
import hashlib

import workouts_db


MUTATION_ENTITY = {
    "log_strength_workout": "strength",
    "log_cardio": "cardio",
    "log_supplement": "supplement",
}


class IntegrityError(RuntimeError):
    pass


def _table_names(conn):
    return {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _row_dict(cursor, row):
    if isinstance(row, sqlite3.Row):
        return dict(row)
    return dict(zip((item[0] for item in cursor.description), row))


def _fetch_one_dict(conn, sql, params=()):
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    return _row_dict(cur, row) if row is not None else None


def _issue(issues, code, **detail):
    issues.append({"code": code, **detail})


def _expected_action_count(entity_type, result):
    data = result.get("data") if isinstance(result, dict) else None
    data = data if isinstance(data, dict) else {}
    if entity_type == "strength":
        value = data.get("entries")
    elif entity_type == "supplement":
        value = data.get("items")
    else:
        value = 1
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _validated_record_ids(value):
    """Return normalized record ids, or None for a malformed audit payload."""
    if not isinstance(value, list):
        return None
    if any(
        not isinstance(item, int) or isinstance(item, bool) or item <= 0
        for item in value
    ):
        return None
    if len(value) != len(set(value)):
        return None
    return value


def _validated_context_record_ids(value):
    if not isinstance(value, list):
        return None
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return None
    if len(value) != len(set(value)):
        return None
    return value


def _event_state_hash(entity_type, row, *, before_delete=False):
    state = dict(row)
    if before_delete:
        state["deleted_at"] = None
        state["deleted_by_action_id"] = None
        state["updated_at"] = state.get("created_at")
    return workouts_db._event_row_hash(entity_type, state)


def _open_readonly(path):
    absolute = os.path.abspath(os.fspath(path))
    conn = sqlite3.connect(f"file:{absolute}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _audit_daily_context_actions(conn, tables, issues, installed_at):
    actions = conn.execute(
        """SELECT action_id, source, chat_id, message_id, result_json, completed_at
           FROM conversation_actions
           WHERE status='succeeded' AND tool_name='save_daily_context'
           ORDER BY completed_at, action_id"""
    ).fetchall()
    if not actions:
        return
    required = {
        "daily_context_entries", "daily_context_projection_state",
        "factor_extraction_jobs",
    }
    missing = required - tables
    for table in sorted(missing):
        _issue(issues, "missing_daily_context_audit_table", table=table)
    if missing:
        return

    for raw_action in actions:
        action_id, source, chat_id, message_id, result_json, completed_at = raw_action
        try:
            result = json.loads(result_json) if result_json else None
        except (TypeError, json.JSONDecodeError):
            result = None
        if not isinstance(result, dict):
            _issue(
                issues, "succeeded_action_missing_result", action_id=action_id,
                entity_type="daily_context",
            )
            continue
        created_count = result.get("created_count")
        valid_created_count = (
            isinstance(created_count, int) and not isinstance(created_count, bool)
            and created_count >= 0
        )
        record_ids = _validated_context_record_ids(result.get("record_ids"))
        if not valid_created_count or record_ids is None:
            _issue(
                issues, "succeeded_action_unverifiable", action_id=action_id,
                entity_type="daily_context",
                reason=("created_count_invalid" if not valid_created_count
                        else "record_ids_invalid"),
            )
            continue
        post_install = bool(
            installed_at and completed_at and completed_at >= installed_at
        )
        if not post_install and created_count == 1 and record_ids == []:
            # Phase-1 audit results did not contain entry ids. They can still
            # be checked against the dated compatibility projection, but are
            # intentionally not held to the post-migration provenance format.
            resolved_date = result.get("resolved_date")
            legacy_row = None
            if isinstance(resolved_date, str):
                legacy_row = conn.execute(
                    "SELECT 1 FROM daily_log WHERE date=? AND notes IS NOT NULL",
                    (resolved_date,),
                ).fetchone()
            if legacy_row is None:
                _issue(
                    issues, "legacy_daily_context_missing",
                    action_id=action_id, context_date=resolved_date,
                )
            continue
        # The context API may return the already-existing entry id on an
        # idempotent replay (created_count=0); every returned id is still
        # verified below.
        if created_count > 0 and len(record_ids) != created_count:
            _issue(
                issues, "succeeded_action_record_count_mismatch",
                action_id=action_id, entity_type="daily_context",
                created_count=created_count, record_id_count=len(record_ids),
            )
        expected_source_key = f"{source}:{chat_id}:{message_id}:daily_context:0"
        result_data = result.get("data") if isinstance(result.get("data"), dict) else {}
        result_job_id = result_data.get("factor_job_id")
        for entry_id in record_ids:
            entry = _fetch_one_dict(
                conn,
                """SELECT entry_id, context_date, notes, source_key,
                          origin_action_id, revision, content_sha256
                   FROM daily_context_entries WHERE entry_id=?""",
                (entry_id,),
            )
            if entry is None:
                _issue(
                    issues, "succeeded_action_missing_domain_row",
                    action_id=action_id, entity_type="daily_context",
                    missing_record_ids=[entry_id],
                )
                continue
            if entry.get("origin_action_id") != action_id:
                _issue(
                    issues, "daily_context_origin_action_mismatch",
                    action_id=action_id, entry_id=entry_id,
                    actual=entry.get("origin_action_id"),
                )
            if entry.get("source_key") != expected_source_key:
                _issue(
                    issues, "daily_context_source_key_mismatch",
                    action_id=action_id, entry_id=entry_id,
                    expected=expected_source_key, actual=entry.get("source_key"),
                )
            expected_content_hash = hashlib.sha256(
                str(entry.get("notes") or "").encode("utf-8")
            ).hexdigest()
            if entry.get("content_sha256") != expected_content_hash:
                _issue(
                    issues, "daily_context_content_hash_mismatch",
                    action_id=action_id, entry_id=entry_id,
                )
            revision = entry.get("revision")
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                _issue(
                    issues, "daily_context_revision_invalid",
                    action_id=action_id, entry_id=entry_id, revision=revision,
                )
                continue
            projection = _fetch_one_dict(
                conn,
                """SELECT projection_hash, revision
                   FROM daily_context_projection_state WHERE context_date=?""",
                (entry["context_date"],),
            )
            if projection is None or int(projection.get("revision") or 0) < revision:
                _issue(
                    issues, "daily_context_projection_revision_missing",
                    action_id=action_id, entry_id=entry_id, revision=revision,
                )
            job = None
            if isinstance(result_job_id, str) and result_job_id:
                job = _fetch_one_dict(
                    conn,
                    """SELECT job_id, context_date, projection_hash,
                              projection_revision, origin_action_id
                       FROM factor_extraction_jobs WHERE job_id=?""",
                    (result_job_id,),
                )
            if job is None:
                job = _fetch_one_dict(
                    conn,
                    """SELECT job_id, context_date, projection_hash,
                              projection_revision, origin_action_id
                       FROM factor_extraction_jobs
                       WHERE origin_action_id=? AND context_date=?
                         AND projection_revision=?
                       ORDER BY created_at LIMIT 1""",
                    (action_id, entry["context_date"], revision),
                )
            if job is None:
                _issue(
                    issues, "daily_context_outbox_revision_missing",
                    action_id=action_id, entry_id=entry_id, revision=revision,
                )
                continue
            if (
                job.get("origin_action_id") != action_id
                or job.get("context_date") != entry.get("context_date")
                or job.get("projection_revision") != revision
                or not job.get("projection_hash")
            ):
                _issue(
                    issues, "daily_context_outbox_provenance_mismatch",
                    action_id=action_id, entry_id=entry_id, job_id=job.get("job_id"),
                )
            if (
                projection is not None and projection.get("revision") == revision
                and projection.get("projection_hash") != job.get("projection_hash")
            ):
                _issue(
                    issues, "daily_context_projection_hash_mismatch",
                    action_id=action_id, entry_id=entry_id, job_id=job.get("job_id"),
                )


def _audit_morning_context(conn, issues, installed_at):
    rows = conn.execute(
        """SELECT recovery_date, evening_date, status, source_message_id, updated_at
           FROM morning_context
           WHERE status IN ('answered','analyzing','analyzed')"""
    ).fetchall()
    for recovery_date, evening_date, status, message_id, updated_at in rows:
        post_install = bool(installed_at and updated_at and updated_at >= installed_at)
        entry = None
        if message_id is not None:
            entry = _fetch_one_dict(
                conn,
                """SELECT entry_id, notes, source_key, revision, content_sha256
                   FROM daily_context_entries
                   WHERE context_date=? AND source_key LIKE ?
                   ORDER BY revision LIMIT 1""",
                (evening_date, f"%:{message_id}:morning-context"),
            )
        if entry is None:
            legacy = conn.execute(
                "SELECT 1 FROM daily_log WHERE date=? AND notes IS NOT NULL",
                (evening_date,),
            ).fetchone()
            if post_install or legacy is None:
                _issue(
                    issues, "morning_context_entry_missing",
                    recovery_date=recovery_date, context_date=evening_date,
                    source_message_id=message_id, status=status,
                )
            continue
        expected_hash = hashlib.sha256(
            str(entry.get("notes") or "").encode("utf-8")
        ).hexdigest()
        if entry.get("content_sha256") != expected_hash:
            _issue(
                issues, "morning_context_content_hash_mismatch",
                recovery_date=recovery_date, entry_id=entry["entry_id"],
            )
        projection = conn.execute(
            """SELECT projection_hash, revision FROM daily_context_projection_state
               WHERE context_date=?""", (evening_date,),
        ).fetchone()
        if projection is None or projection[1] < entry["revision"]:
            _issue(
                issues, "morning_context_projection_missing",
                recovery_date=recovery_date, entry_id=entry["entry_id"],
            )
        job = conn.execute(
            """SELECT projection_hash, projection_revision
               FROM factor_extraction_jobs
               WHERE context_date=? AND projection_revision=?
                 AND source_key LIKE ?""",
            (evening_date, entry["revision"],
             f"{entry['source_key']}:factor-projection:%"),
        ).fetchone()
        if job is None:
            _issue(
                issues, "morning_context_outbox_missing",
                recovery_date=recovery_date, entry_id=entry["entry_id"],
            )
        elif (
            projection is not None
            and projection[1] == job[1]
            and projection[0] != job[0]
        ):
            _issue(
                issues, "morning_context_projection_hash_mismatch",
                recovery_date=recovery_date, entry_id=entry["entry_id"],
                projection_revision=projection[1],
            )


def _matching_reconciliation(conn, action_id, entity_type, table, record_ids):
    """Return the applied reconciliation dict if a documented, verified
    recovery accounts for this action's missing historical ids, else None.

    The reconciliation record alone is not trusted: the recovered ids it
    names must actually exist as domain rows and actually be exactly this
    action's current links, or the original missing-row issue still fires.
    """
    reconciliation = _fetch_one_dict(
        conn,
        """SELECT original_record_ids, recovered_record_ids, reason, created_at
           FROM recovery_reconciliations WHERE action_id=? AND entity_type=?""",
        (action_id, entity_type),
    )
    if reconciliation is None:
        return None
    try:
        original = sorted(json.loads(reconciliation["original_record_ids"]))
        recovered = sorted(json.loads(reconciliation["recovered_record_ids"]))
    except (TypeError, json.JSONDecodeError):
        return None
    if original != sorted(record_ids) or not recovered:
        return None
    recovered_existing = {
        row[0] for row in conn.execute(
            f"SELECT id FROM {table} WHERE id IN "
            f"({','.join('?' for _ in recovered)})",
            tuple(recovered),
        )
    }
    recovered_linked = {
        row[0] for row in conn.execute(
            """SELECT entity_id FROM action_domain_links
               WHERE action_id=? AND entity_type=?""",
            (action_id, entity_type),
        )
    }
    if recovered_existing != set(recovered) or recovered_linked != set(recovered):
        return None
    return {
        "original_record_ids": original,
        "recovered_record_ids": recovered,
        "reason": reconciliation["reason"],
    }


def audit_database(path_or_conn):
    """Return a structured integrity report without changing the database."""
    owns_connection = not isinstance(path_or_conn, sqlite3.Connection)
    conn = _open_readonly(path_or_conn) if owns_connection else path_or_conn
    issues = []
    report = {
        "ok": False,
        "quick_check": None,
        "database_uuid": None,
        "generation": None,
        "counts": {},
        "legacy_rows": {},
        "issues": issues,
    }
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()
        report["quick_check"] = quick[0] if quick else None
        if report["quick_check"] != "ok":
            _issue(issues, "quick_check_failed", detail=report["quick_check"])

        tables = _table_names(conn)
        required = {
            "database_meta", "action_domain_links", "domain_events",
            "recovery_reconciliations",
            "conversation_actions", "daily_context_entries",
            "daily_context_projection_state", "factor_extraction_jobs",
            "daily_factor_observations", "morning_context",
            *workouts_db.ENTITY_TABLES.values(),
        }
        for table in sorted(required - tables):
            _issue(issues, "missing_table", table=table)
        if required - tables:
            report["ok"] = False
            return report

        triggers = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        for table in workouts_db.ENTITY_TABLES.values():
            trigger = f"trg_{table}_no_hard_delete"
            if trigger not in triggers:
                _issue(issues, "missing_hard_delete_trigger", table=table, trigger=trigger)
        if "conversation_actions" in tables:
            for trigger in (
                "trg_conversation_actions_no_delete",
                "trg_conversation_actions_success_immutable",
            ):
                if trigger not in triggers:
                    _issue(
                        issues, "missing_audit_action_trigger",
                        table="conversation_actions", trigger=trigger,
                    )
        if "daily_context_entries" in tables:
            for trigger in (
                "trg_daily_context_entries_no_delete",
                "trg_daily_context_entries_content_immutable",
            ):
                if trigger not in triggers:
                    _issue(
                        issues, "missing_daily_context_trigger",
                        table="daily_context_entries", trigger=trigger,
                    )
        for table in ("action_domain_links", "domain_events", "recovery_reconciliations"):
            for operation in ("update", "delete"):
                trigger = f"trg_{table}_immutable_{operation}"
                if trigger not in triggers:
                    _issue(
                        issues, "missing_immutable_ledger_trigger",
                        table=table, operation=operation, trigger=trigger,
                    )

        meta_rows = conn.execute(
            "SELECT singleton_id, database_uuid, generation, created_at FROM database_meta"
        ).fetchall()
        if len(meta_rows) != 1 or meta_rows[0][0] != 1:
            _issue(issues, "invalid_database_meta", row_count=len(meta_rows))
            installed_at = None
        else:
            report["database_uuid"] = meta_rows[0][1]
            report["generation"] = meta_rows[0][2]
            installed_at = meta_rows[0][3]

        report["counts"]["links"] = conn.execute(
            "SELECT COUNT(*) FROM action_domain_links"
        ).fetchone()[0]
        report["counts"]["domain_events"] = conn.execute(
            "SELECT COUNT(*) FROM domain_events"
        ).fetchone()[0]

        actions_exist = "conversation_actions" in tables
        link_cur = conn.execute(
            """SELECT action_id, entity_type, entity_id, source_key, row_hash
               FROM action_domain_links ORDER BY action_id, entity_type, entity_id"""
        )
        for raw_link in link_cur.fetchall():
            link = _row_dict(link_cur, raw_link)
            entity_type = link["entity_type"]
            table = workouts_db.ENTITY_TABLES.get(entity_type)
            if table is None:
                _issue(issues, "invalid_link_entity_type", **link)
                continue
            row = _fetch_one_dict(
                conn, f"SELECT * FROM {table} WHERE id = ?", (link["entity_id"],)
            )
            if row is None:
                _issue(issues, "missing_domain_row", **link)
                continue
            if row.get("source_key") != link["source_key"]:
                _issue(issues, "source_key_mismatch", **link)
            if row.get("origin_action_id") != link["action_id"]:
                _issue(issues, "origin_action_mismatch", **link)
            actual_hash = workouts_db.hash_activity_row(entity_type, row)
            if actual_hash != link["row_hash"]:
                _issue(
                    issues, "row_hash_mismatch", action_id=link["action_id"],
                    entity_type=entity_type, entity_id=link["entity_id"],
                    expected_hash=link["row_hash"], actual_hash=actual_hash,
                )
            event_cur = conn.execute(
                """SELECT event_id, event_type, action_id, source_key,
                          before_hash, after_hash, reason, created_at
                   FROM domain_events
                   WHERE entity_type = ? AND entity_id = ?
                   ORDER BY event_id""",
                (entity_type, link["entity_id"]),
            )
            events = [_row_dict(event_cur, item) for item in event_cur.fetchall()]
            created = [item for item in events if item["event_type"] == "created"]
            deleted = [item for item in events if item["event_type"] == "soft_deleted"]
            if len(created) != 1:
                _issue(
                    issues, "invalid_created_event_count", entity_type=entity_type,
                    entity_id=link["entity_id"], actual=len(created), expected=1,
                )
            else:
                event = created[0]
                expected_created_hash = _event_state_hash(
                    entity_type, row, before_delete=row.get("deleted_at") is not None,
                )
                if event["action_id"] != link["action_id"]:
                    _issue(
                        issues, "created_event_actor_mismatch", entity_type=entity_type,
                        entity_id=link["entity_id"], expected=link["action_id"],
                        actual=event["action_id"],
                    )
                if event["source_key"] != link["source_key"]:
                    _issue(
                        issues, "created_event_source_mismatch", entity_type=entity_type,
                        entity_id=link["entity_id"], expected=link["source_key"],
                        actual=event["source_key"],
                    )
                if event["before_hash"] is not None or event["after_hash"] != expected_created_hash:
                    _issue(
                        issues, "created_event_hash_mismatch", entity_type=entity_type,
                        entity_id=link["entity_id"],
                    )
                if event["reason"] != "conversation mutation":
                    _issue(
                        issues, "created_event_reason_invalid", entity_type=entity_type,
                        entity_id=link["entity_id"],
                    )

            is_deleted = row.get("deleted_at") is not None
            expected_deleted = 1 if is_deleted else 0
            if len(deleted) != expected_deleted:
                _issue(
                    issues, "invalid_soft_delete_event_count", entity_type=entity_type,
                    entity_id=link["entity_id"], actual=len(deleted),
                    expected=expected_deleted,
                )
            if is_deleted and len(deleted) == 1:
                event = deleted[0]
                created_event = created[0] if len(created) == 1 else None
                if event["action_id"] != row.get("deleted_by_action_id"):
                    _issue(
                        issues, "soft_delete_event_actor_mismatch", entity_type=entity_type,
                        entity_id=link["entity_id"],
                    )
                if event["source_key"] != row.get("source_key"):
                    _issue(
                        issues, "soft_delete_event_source_mismatch", entity_type=entity_type,
                        entity_id=link["entity_id"],
                    )
                expected_after = _event_state_hash(entity_type, row)
                expected_before = (
                    created_event["after_hash"] if created_event is not None else None
                )
                if event["before_hash"] != expected_before or event["after_hash"] != expected_after:
                    _issue(
                        issues, "soft_delete_event_hash_mismatch", entity_type=entity_type,
                        entity_id=link["entity_id"],
                    )
                if not isinstance(event["reason"], str) or not event["reason"].strip():
                    _issue(
                        issues, "soft_delete_event_reason_invalid", entity_type=entity_type,
                        entity_id=link["entity_id"],
                    )
                if created_event is not None and (
                    event["event_id"] <= created_event["event_id"]
                    or event["created_at"] < created_event["created_at"]
                ):
                    _issue(
                        issues, "domain_event_order_invalid", entity_type=entity_type,
                        entity_id=link["entity_id"],
                    )
                if event["created_at"] != row.get("deleted_at"):
                    _issue(
                        issues, "soft_delete_event_timestamp_mismatch",
                        entity_type=entity_type, entity_id=link["entity_id"],
                    )
            if actions_exist:
                action = conn.execute(
                    "SELECT 1 FROM conversation_actions WHERE action_id = ?",
                    (link["action_id"],),
                ).fetchone()
                if action is None:
                    _issue(issues, "orphan_link", **link)

        event_cur = conn.execute(
            """SELECT event_id, entity_type, entity_id, event_type, action_id,
                      source_key FROM domain_events ORDER BY event_id"""
        )
        for raw_event in event_cur.fetchall():
            event = _row_dict(event_cur, raw_event)
            table = workouts_db.ENTITY_TABLES.get(event["entity_type"])
            if table is None:
                _issue(issues, "invalid_event_entity_type", **event)
                continue
            domain = conn.execute(
                f"SELECT 1 FROM {table} WHERE id = ?", (event["entity_id"],)
            ).fetchone()
            if domain is None:
                _issue(issues, "orphan_domain_event", **event)
                continue
            link = conn.execute(
                """SELECT 1 FROM action_domain_links
                   WHERE entity_type = ? AND entity_id = ?""",
                (event["entity_type"], event["entity_id"]),
            ).fetchone()
            if link is None:
                _issue(issues, "domain_event_without_link", **event)

        for entity_type, table in workouts_db.ENTITY_TABLES.items():
            report["counts"][entity_type] = conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            report["legacy_rows"][entity_type] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE origin_action_id IS NULL"
            ).fetchone()[0]
            cur = conn.execute(
                f"""SELECT id, origin_action_id, source_key, created_at FROM {table}"""
            )
            for raw_row in cur.fetchall():
                row = _row_dict(cur, raw_row)
                link = None
                if row["origin_action_id"] is not None:
                    link = conn.execute(
                    """SELECT 1 FROM action_domain_links
                       WHERE action_id = ? AND entity_type = ? AND entity_id = ?""",
                    (row["origin_action_id"], entity_type, row["id"]),
                    ).fetchone()
                if row["origin_action_id"] is not None and link is None:
                    _issue(
                        issues, "domain_row_without_link", entity_type=entity_type,
                        entity_id=row["id"], action_id=row["origin_action_id"],
                        source_key=row["source_key"],
                    )
                post_install_row = bool(
                    installed_at and row.get("created_at")
                    and row["created_at"] >= installed_at
                )
                if post_install_row and (
                    not row.get("origin_action_id") or not row.get("source_key")
                    or link is None
                ):
                    _issue(
                        issues, "post_install_row_without_provenance",
                        entity_type=entity_type, entity_id=row["id"],
                        action_id=row.get("origin_action_id"),
                        source_key=row.get("source_key"),
                        created_at=row.get("created_at"), installed_at=installed_at,
                    )

        if actions_exist:
            action_cur = conn.execute(
                """SELECT action_id, tool_name, result_json, completed_at
                   FROM conversation_actions
                   WHERE status = 'succeeded'
                     AND tool_name IN ('log_strength_workout','log_cardio','log_supplement')"""
            )
            for raw_action in action_cur.fetchall():
                action = _row_dict(action_cur, raw_action)
                entity_type = MUTATION_ENTITY[action["tool_name"]]
                table = workouts_db.ENTITY_TABLES[entity_type]
                post_install = bool(
                    installed_at and action["completed_at"]
                    and action["completed_at"] >= installed_at
                )
                try:
                    result = json.loads(action["result_json"]) if action["result_json"] else None
                except (TypeError, json.JSONDecodeError):
                    result = None
                if not isinstance(result, dict):
                    _issue(
                        issues, "succeeded_action_missing_result",
                        action_id=action["action_id"], entity_type=entity_type,
                    )
                    continue
                expected = _expected_action_count(entity_type, result)
                actual = conn.execute(
                    """SELECT COUNT(*) FROM action_domain_links
                       WHERE action_id = ? AND entity_type = ?""",
                    (action["action_id"], entity_type),
                ).fetchone()[0]
                created_count = result.get("created_count")
                valid_created_count = (
                    isinstance(created_count, int) and not isinstance(created_count, bool)
                    and created_count >= 0
                )
                if not valid_created_count:
                    _issue(
                        issues, "succeeded_action_unverifiable",
                        action_id=action["action_id"], entity_type=entity_type,
                        reason="created_count_invalid",
                    )
                record_ids = _validated_record_ids(result.get("record_ids"))
                if record_ids is None:
                    _issue(
                        issues, "succeeded_action_unverifiable",
                        action_id=action["action_id"], entity_type=entity_type,
                        reason="record_ids_invalid",
                    )
                elif valid_created_count and len(record_ids) != created_count:
                    _issue(
                        issues, "succeeded_action_record_count_mismatch",
                        action_id=action["action_id"], entity_type=entity_type,
                        created_count=created_count, record_id_count=len(record_ids),
                    )
                if record_ids is not None:
                    existing_ids = {
                        row[0] for row in conn.execute(
                            f"SELECT id FROM {table} WHERE id IN "
                            f"({','.join('?' for _ in record_ids)})",
                            tuple(record_ids),
                        )
                    } if record_ids else set()
                    missing_ids = sorted(set(record_ids) - existing_ids)
                    if missing_ids:
                        reconciliation = _matching_reconciliation(
                            conn, action["action_id"], entity_type, table, record_ids,
                        )
                        if reconciliation is not None:
                            report.setdefault("reconciliations", []).append({
                                "action_id": action["action_id"],
                                "entity_type": entity_type,
                                **reconciliation,
                            })
                        else:
                            _issue(
                                issues, "succeeded_action_missing_domain_row",
                                action_id=action["action_id"], entity_type=entity_type,
                                missing_record_ids=missing_ids,
                            )
                if post_install:
                    expected_links = created_count if valid_created_count else expected
                    if expected_links is None or actual != expected_links:
                        _issue(
                            issues, "succeeded_action_link_count_mismatch",
                            action_id=action["action_id"], entity_type=entity_type,
                            expected=expected_links, actual=actual,
                        )
                if post_install and record_ids is not None:
                    linked_ids = {
                        row[0] for row in conn.execute(
                            """SELECT entity_id FROM action_domain_links
                               WHERE action_id = ? AND entity_type = ?""",
                            (action["action_id"], entity_type),
                        )
                    }
                    if set(record_ids) != linked_ids:
                        _issue(
                            issues, "result_record_ids_not_linked",
                            action_id=action["action_id"], entity_type=entity_type,
                            result_record_ids=sorted(record_ids),
                            linked_record_ids=sorted(linked_ids),
                        )

            correction_cur = conn.execute(
                """SELECT action_id, result_json FROM conversation_actions
                   WHERE status='succeeded' AND tool_name='correct_activity'"""
            )
            for raw_action in correction_cur.fetchall():
                action = _row_dict(correction_cur, raw_action)
                try:
                    result = json.loads(action["result_json"] or "")
                except (TypeError, json.JSONDecodeError):
                    result = None
                data = result.get("data") if isinstance(result, dict) else None
                entity_type = data.get("entity_type") if isinstance(data, dict) else None
                operation = data.get("operation") if isinstance(data, dict) else None
                created_count = result.get("created_count") if isinstance(result, dict) else None
                deleted_count = result.get("deleted_count") if isinstance(result, dict) else None
                if (
                    entity_type not in workouts_db.ENTITY_TABLES
                    or operation not in {"delete", "move"}
                    or isinstance(created_count, bool)
                    or not isinstance(created_count, int) or created_count < 0
                    or isinstance(deleted_count, bool)
                    or not isinstance(deleted_count, int) or deleted_count < 1
                ):
                    _issue(
                        issues, "correction_action_unverifiable",
                        action_id=action["action_id"],
                    )
                    continue
                links = conn.execute(
                    """SELECT COUNT(*) FROM action_domain_links
                       WHERE action_id=? AND entity_type=?""",
                    (action["action_id"], entity_type),
                ).fetchone()[0]
                created_events = conn.execute(
                    """SELECT COUNT(*) FROM domain_events
                       WHERE action_id=? AND entity_type=? AND event_type='created'""",
                    (action["action_id"], entity_type),
                ).fetchone()[0]
                deleted_events = conn.execute(
                    """SELECT COUNT(*) FROM domain_events
                       WHERE action_id=? AND entity_type=? AND event_type='soft_deleted'""",
                    (action["action_id"], entity_type),
                ).fetchone()[0]
                expected_created = deleted_count if operation == "move" else 0
                if (
                    created_count != expected_created
                    or links != expected_created
                    or created_events != expected_created
                    or deleted_events != deleted_count
                ):
                    _issue(
                        issues, "correction_action_event_count_mismatch",
                        action_id=action["action_id"], entity_type=entity_type,
                        expected_created=expected_created, created_count=created_count,
                        links=links, created_events=created_events,
                        expected_deleted=deleted_count, deleted_events=deleted_events,
                    )

            _audit_daily_context_actions(conn, tables, issues, installed_at)
        _audit_morning_context(conn, issues, installed_at)

        report["ok"] = not issues
        return report
    finally:
        if owns_connection:
            conn.close()


def assert_integrity(path_or_conn):
    report = audit_database(path_or_conn)
    if not report["ok"]:
        codes = ", ".join(issue["code"] for issue in report["issues"][:10])
        raise IntegrityError(f"database integrity audit failed: {codes}")
    return report
