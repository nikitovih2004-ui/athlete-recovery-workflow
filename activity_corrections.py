"""Deterministic, confirmed deletion and date correction for logged activities."""
from __future__ import annotations

import datetime as dt
import json
import re

import conversation_contract as C
import conversation_store as store
import conversation_tools
import phase2_store
import workouts_db

INTENT = "correct_logged_activity"
TOOL = "correct_activity"
_YES_DELETE = frozenset({"да, удалить", "да удалить", "удалить"})
_YES_MOVE = frozenset({"да, перенести", "да перенести", "перенести"})
_NO = frozenset({"нет", "отмена", "не надо", "не удалять", "не переносить"})


def parse_command(text, local_now):
    normalized = " ".join((text or "").casefold().strip().split())
    is_delete = bool(re.search(r"\b(?:удали|удалить|сотри|стереть)\b", normalized))
    is_move = bool(re.search(
        r"\b(?:перенеси|перенести|перезапиши|перезаписать|исправь дату)\b",
        normalized,
    ))
    if not is_delete and not is_move:
        return None
    entity_type = (
        "cardio" if "кардио" in normalized
        else "strength" if any(word in normalized for word in (
            "силов", "упражнен",
        ))
        else None
    )
    today = local_now.date()
    source_date = None
    target_date = None
    if is_move:
        if "вчера" in normalized:
            target_date = (today - dt.timedelta(days=1)).isoformat()
        elif "сегодня" in normalized:
            target_date = today.isoformat()
        else:
            match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", normalized)
            target_date = match.group(1) if match else None
        if target_date is not None:
            try:
                parsed_date = dt.date.fromisoformat(target_date)
            except ValueError:
                target_date = None
            else:
                if (
                    parsed_date > today
                    or (today - parsed_date).days > C.MAX_PAST_DAYS
                ):
                    target_date = None
    else:
        if "вчера" in normalized:
            source_date = (today - dt.timedelta(days=1)).isoformat()
        elif "сегодня" in normalized:
            source_date = today.isoformat()
    return {
        "operation": "move" if is_move else "delete",
        "entity_type": entity_type,
        "source_date": source_date,
        "target_date": target_date,
    }


def _lease(reservation):
    return {
        "processing_token": reservation.processing_token,
        "processing_fence": reservation.processing_fence,
    }


def _latest_candidate(conn, ctx, request):
    filters = [
        "a.status='succeeded'",
        "a.chat_id=?",
        "IFNULL(a.user_id,'')=IFNULL(?,'')",
        "d.deleted_at IS NULL",
    ]
    params = [str(ctx.chat_id), str(ctx.user_id) if ctx.user_id is not None else None]
    entities = [request["entity_type"]] if request["entity_type"] else ["strength", "cardio"]
    candidates = []
    for entity_type in entities:
        table = workouts_db.ENTITY_TABLES[entity_type]
        local_filters = list(filters)
        local_params = list(params)
        if request.get("source_date"):
            local_filters.append("d.date=?")
            local_params.append(request["source_date"])
        if request.get("origin_action_id"):
            local_filters.append("a.action_id=?")
            local_params.append(request["origin_action_id"])
        item_expression = (
            "COUNT(DISTINCT d.exercise_name)" if entity_type == "strength"
            else "COUNT(*)"
        )
        row = conn.execute(
            f"""SELECT a.action_id, a.completed_at, d.date, COUNT(*) AS row_count,
                       {item_expression} AS item_count
                FROM conversation_actions a
                JOIN action_domain_links l ON l.action_id=a.action_id
                                           AND l.entity_type=?
                JOIN {table} d ON d.id=l.entity_id
                WHERE {' AND '.join(local_filters)}
                GROUP BY a.action_id, a.completed_at, d.date
                ORDER BY a.completed_at DESC LIMIT 1""",
            [entity_type, *local_params],
        ).fetchone()
        if row:
            candidates.append((row["completed_at"], entity_type, dict(row)))
    return max(candidates, default=None, key=lambda item: item[0])


def exact_origin_delete_request(origin_action_id, *, entity_type="strength"):
    """Build a maintenance-only, audited correction for one exact origin.

    This does not mutate. It is passed through the same hash-bound preview and
    Reply-confirmed transaction as a user correction, but avoids dangerous
    "latest workout" selection during an incident repair.
    """
    if not isinstance(origin_action_id, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        origin_action_id,
    ):
        raise ValueError("invalid_origin_action_id")
    if entity_type not in {"strength", "cardio"}:
        raise ValueError("invalid_entity_type")
    return {
        "operation": "delete", "entity_type": entity_type,
        "source_date": None, "target_date": None,
        "origin_action_id": origin_action_id,
        "correction_reason": "verified_data_repair",
    }


def propose(ctx, local_now, request, *, reservation=None, router_metadata=None):
    """Build a preview from a validated bounded request; never mutate here."""
    reservation = reservation or store.reserve(ctx)
    if not reservation.is_new:
        result = json.loads(reservation.result_json) if reservation.result_json else None
        return {
            "action_id": reservation.action_id,
            "kind": "in_progress" if reservation.in_progress else "duplicate",
            "message": (
                "Команда уже обрабатывается." if reservation.in_progress
                else reservation.response_text
            ),
            "result": result,
        }
    lease = _lease(reservation)
    metadata = router_metadata or {}
    store.record_router(
        reservation.action_id, model=metadata.get("model", "deterministic-correction-v1"),
        response_sha256=metadata.get("response_sha256"), intent=INTENT,
        confidence=metadata.get("confidence", 1.0),
        latency_ms=metadata.get("latency_ms", 0),
        attempt_count=metadata.get("attempt_count", 0),
        prompt_version=metadata.get("prompt_version", "correction-v1"), **lease,
    )
    # "this" without a bounded entity/date is intentionally not resolved to
    # whichever activity happens to be newest. The user must disambiguate.
    if router_metadata is not None and not request.get("entity_type") and not request.get("source_date"):
        store.mark_rejected(
            reservation.action_id, "correction_ambiguous_target", None,
            intent=INTENT, confidence=metadata.get("confidence", 1.0), **lease,
        )
        return {
            "action_id": reservation.action_id, "kind": "clarification",
            "message": "Уточни, какую тренировку: кардио или силовую и за какой день.",
        }
    if request["operation"] == "move" and not request.get("target_date"):
        store.mark_rejected(
            reservation.action_id, "correction_missing_target_date",
            None, intent=INTENT, confidence=1.0, **lease,
        )
        return {
            "action_id": reservation.action_id, "kind": "rejected",
            "message": "Укажи новую дату: например, «перенеси последнюю тренировку на вчера».",
        }
    conn = store.connect()
    try:
        workouts_db.ensure_tables(conn)
        candidate = _latest_candidate(conn, ctx, request)
        if not candidate:
            store.mark_noop(reservation.action_id, INTENT, 1.0, **lease)
            return {
                "action_id": reservation.action_id, "kind": "general",
                "message": "Не нашёл подходящую активную тренировку для этой команды.",
            }
        _, entity_type, summary = candidate
        if request["operation"] == "move" and summary["date"] == request["target_date"]:
            store.mark_noop(reservation.action_id, INTENT, 1.0, **lease)
            return {
                "action_id": reservation.action_id, "kind": "general",
                "message": f"Эта тренировка уже записана за {summary['date']}.",
            }
        table = workouts_db.ENTITY_TABLES[entity_type]
        rows = conn.execute(
            f"""SELECT d.* FROM {table} d
                JOIN action_domain_links l ON l.entity_id=d.id AND l.entity_type=?
                WHERE l.action_id=? AND d.deleted_at IS NULL ORDER BY d.id""",
            (entity_type, summary["action_id"]),
        ).fetchall()
        expected = [
            {"id": row["id"], "row_hash": workouts_db.hash_activity_row(entity_type, dict(row))}
            for row in rows
        ]
    finally:
        conn.close()
    partial = {
        "operation": request["operation"],
        "entity_type": entity_type,
        "origin_action_id": summary["action_id"],
        "source_date": summary["date"],
        "target_date": request.get("target_date"),
        "correction_reason": request.get("correction_reason"),
        "expected": expected,
        "row_count": summary["row_count"],
        "item_count": summary["item_count"],
    }
    store.mark_clarification(reservation.action_id, INTENT, 1.0, **lease)
    pending_id = store.create_pending(
        reservation.action_id, ctx, INTENT, partial, ["confirmation"],
    )
    noun = (
        f"силовую: {summary['item_count']} упражнений, {summary['row_count']} подходов"
        if entity_type == "strength"
        else "кардио-тренировку"
    )
    if request["operation"] == "delete":
        question = (
            f"Удалить последнюю {noun} за {summary['date']}? "
            "Ответь через Reply: «да, удалить»."
        )
    else:
        question = (
            f"Перенести последнюю {noun} с {summary['date']} на "
            f"{request['target_date']}? Ответь через Reply: «да, перенести»."
        )
    return {
        "action_id": reservation.action_id, "kind": "clarification",
        "message": question, "pending_id": pending_id,
    }


def _copy_row(conn, entity_type, row, target_date, ctx, index):
    source_key = ctx.source_key(
        "workout" if entity_type == "strength" else "cardio", index
    )
    if entity_type == "strength":
        row_id = workouts_db.insert_workout_row(
            conn, target_date, row["exercise_name"], row["weight"],
            row["sets"], row["reps"], raw_text=f"conversation:{ctx.action_id}",
            source_key=source_key, origin_action_id=ctx.action_id,
        )
    else:
        row_id = workouts_db.insert_cardio_row(
            conn, target_date, row["time"], row["type"], row["duration"],
            row["distance"], row["avg_hr"], row["calories"],
            row["hr_zone_0_duration"], row["hr_zone_1_duration"],
            row["hr_zone_2_duration"], row["hr_zone_3_duration"],
            row["hr_zone_4_duration"], row["hr_zone_5_duration"],
            raw_text=f"conversation:{ctx.action_id}", source_key=source_key,
            origin_action_id=ctx.action_id, strain=row["strain"],
            max_hr=row["max_hr"], steps=row["steps"],
        )
    if row_id is None:
        raise RuntimeError("correction duplicate source key")
    workouts_db.link_action_domain(
        conn, ctx.action_id, entity_type, row_id, source_key
    )
    return row_id


def resolve(pending, ctx, exec_ctx):
    reservation = store.reserve(ctx)
    action_id = reservation.action_id
    exec_ctx.action_id = action_id
    exec_ctx.processing_token = reservation.processing_token
    exec_ctx.processing_fence = reservation.processing_fence
    lease = _lease(reservation)
    if not reservation.is_new:
        result = json.loads(reservation.result_json) if reservation.result_json else None
        return {
            "action_id": action_id,
            "kind": "in_progress" if reservation.in_progress else "duplicate",
            "message": (
                "Исправление уже обрабатывается." if reservation.in_progress
                else reservation.response_text
            ),
            "result": result,
        }
    if not store.claim_pending_for_resolution(pending["pending_id"], action_id):
        conn = store.connect()
        try:
            owned = conn.execute(
                """SELECT 1 FROM pending_actions
                   WHERE pending_id=? AND status=? AND claimed_by_action_id=?""",
                (pending["pending_id"], C.PENDING_RESOLVING, action_id),
            ).fetchone() is not None
        finally:
            conn.close()
        if not owned:
            store.mark_noop(action_id, INTENT, 1.0, **lease)
            return {"action_id": action_id, "kind": "general",
                    "message": "Это подтверждение уже обработано."}
    partial = json.loads(pending["partial_arguments_json"])
    answer = " ".join((ctx.input_text or "").casefold().strip().split())
    expected_yes = _YES_DELETE if partial["operation"] == "delete" else _YES_MOVE
    if answer in _NO:
        store.mark_noop(action_id, INTENT, 1.0, **lease)
        store.resolve_pending(pending["pending_id"], action_id)
        return {"action_id": action_id, "kind": "general",
                "message": "Отменено, данные не изменены."}
    if answer not in expected_yes:
        store.mark_rejected(
            action_id, "correction_confirmation_mismatch",
            None, intent=INTENT, confidence=1.0, **lease,
        )
        store.release_pending_claim(pending["pending_id"], action_id)
        required = "да, удалить" if partial["operation"] == "delete" else "да, перенести"
        return {"action_id": action_id, "kind": "rejected",
                "message": f"Для подтверждения ответь через Reply: «{required}»."}
    store.record_router(
        action_id, model="deterministic-correction-v1", response_sha256=None,
        intent=INTENT, confidence=1.0, latency_ms=0, attempt_count=0,
        prompt_version="correction-v1", **lease,
    )
    store.record_validated(
        action_id, intent=INTENT, tool_name=TOOL,
        validated_arguments={
            key: value for key, value in partial.items() if key != "expected"
        },
        **lease,
    )
    entity_type = partial["entity_type"]
    table = workouts_db.ENTITY_TABLES[entity_type]
    conn = store.connect()
    try:
        workouts_db.ensure_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        rows = []
        for expected in partial["expected"]:
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id=? AND deleted_at IS NULL",
                (expected["id"],),
            ).fetchone()
            if row is None or workouts_db.hash_activity_row(
                entity_type, dict(row)
            ) != expected["row_hash"]:
                raise RuntimeError("correction target changed")
            rows.append(row)
        new_ids = []
        if partial["operation"] == "move":
            for index, row in enumerate(rows):
                new_ids.append(_copy_row(
                    conn, entity_type, row, partial["target_date"], exec_ctx, index
                ))
        for row in rows:
            if not workouts_db.soft_delete_activity(
                conn, entity_type, row["id"], deleted_by_action_id=action_id,
                reason=(
                    "user_date_correction"
                    if partial["operation"] == "move"
                    else (partial.get("correction_reason") or "user_confirmed_delete")
                ),
            ):
                raise RuntimeError("correction target already deleted")
        result = {
            "status": "success", "action_id": action_id,
            "created_count": len(new_ids), "record_ids": new_ids,
            "deleted_count": len(rows),
            "resolved_date": partial.get("target_date"),
            "data": {
                "operation": partial["operation"],
                "entity_type": entity_type,
                "source_date": partial["source_date"],
                "target_date": partial.get("target_date"),
                "row_count": len(rows),
                "item_count": partial.get("item_count"),
            },
        }
        store.finalize_success_tx(
            conn, action_id, tool_name=TOOL,
            validated_arguments={
                key: value for key, value in partial.items() if key != "expected"
            },
            result=result, **lease,
        )
        if not phase2_store.finalize_pending_resolution(
            conn, pending["pending_id"], action_id
        ):
            raise RuntimeError("correction pending ownership changed")
        conn.commit()
    except Exception:
        conn.rollback()
        store.release_pending_claim(pending["pending_id"], action_id)
        try:
            store.mark_tool_failed(action_id, tool_name=TOOL, **lease)
        except store.ActionLeaseLost:
            pass
        return {
            "action_id": action_id, "kind": "tool_failed",
            "message": "Не удалось безопасно изменить запись; данные оставлены без изменений.",
        }
    finally:
        conn.close()
    if partial["operation"] == "delete":
        message = f"Удалил выбранную тренировку за {partial['source_date']}."
    else:
        message = (
            f"Перенёс тренировку с {partial['source_date']} на "
            f"{partial['target_date']}: {len(rows)} подходов/записей."
        )
    return {
        "action_id": action_id, "kind": "confirmation",
        "message": message, "result": result, "write_committed": True,
    }
