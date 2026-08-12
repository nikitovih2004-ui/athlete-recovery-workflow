"""Persistent, bounded multi-message strength collection workflow."""
from __future__ import annotations

import json

import bounded_agent
import conversation_contract as C
import conversation_router
import conversation_store as store
import conversation_tools as tools
import conversation_validation as validation
import deterministic_strength
from gemini_client import GeminiError, GeminiUnavailable


DECLARATIONS = [
    {
        "name": "append_strength_exercises",
        "description": "Parse one or more strength exercises/sets from the current message.",
        "parameters": {
            "type": "object", "required": ["confidence", "exercises"],
            "properties": {
                "confidence": {"type": "number"},
                "exercises": {"type": "array", "items": {
                    "type": "object", "required": ["exercise_name", "sets", "side", "note"],
                    "properties": {
                        "exercise_name": {"type": "string"},
                        "sets": {"type": "array", "items": {
                            "type": "object", "required": ["weight_kg", "reps"],
                            "properties": {
                                "weight_kg": {"type": "number", "nullable": True},
                                "reps": {"type": "integer", "nullable": True},
                            },
                        }},
                        "side": {"type": "string", "nullable": True},
                        "note": {"type": "string", "nullable": True},
                    },
                }},
            },
        },
    },
    {"name": "finish_strength_draft", "description": "User says collection is complete; show preview.",
     "parameters": {"type": "object", "required": ["confidence"],
                    "properties": {"confidence": {"type": "number"}}}},
    {"name": "cancel_strength_draft", "description": "User explicitly cancels the active workout draft.",
     "parameters": {"type": "object", "required": ["confidence"],
                    "properties": {"confidence": {"type": "number"}}}},
    {"name": "provide_strength_repetitions", "description": "Answer to the pending missing-repetitions question.",
     "parameters": {"type": "object", "required": ["confidence", "reps"],
                    "properties": {"confidence": {"type": "number"}, "reps": {"type": "integer"}}}},
    {"name": "unrelated_to_strength_draft", "description": "Message does not advance, finish, or cancel the draft.",
     "parameters": {"type": "object", "required": ["confidence"],
                    "properties": {"confidence": {"type": "number"}}}},
]
ALLOWED = frozenset(item["name"] for item in DECLARATIONS)

SYSTEM = """You manage one already-open strength workout draft. Select exactly one function.
Parse natural Russian or English exercise text into every visible set; syntax such as
79x11 means weight 79 kg and 11 repetitions. Preserve exercise names. If a set ends
with a weight and x but no repetitions, append it with reps=null. Use
provide_strength_repetitions only when draft_state says awaiting_reps and the current
message answers that question. Finish/cancel only on the user's natural explicit intent.
Do not infer missing repetitions, dates, exercises, weights, or completion."""


def _lease(reservation):
    return {"processing_token": reservation.processing_token,
            "processing_fence": reservation.processing_fence}


def _preview(state):
    lines = [f"Проверь силовую за {state['resolved_date']}:"]
    for exercise in state.get("exercises", []):
        rendered = []
        for item in exercise.get("sets", []):
            weight = item.get("weight_kg")
            reps = item.get("reps")
            rendered.append(f"{weight:g}×{reps}" if weight is not None else f"{reps} повт.")
        suffix = f" ({exercise['side']})" if exercise.get("side") else ""
        lines.append(f"• {exercise['exercise_name']}{suffix}: {', '.join(rendered)}")
    lines.append("\nОтветь на это сообщение «да», чтобы записать, или «нет», чтобы отменить.")
    return "\n".join(lines)


def _flatten(state):
    rows = []
    for exercise in state.get("exercises", []):
        name = exercise.get("exercise_name", "").strip()
        if exercise.get("side"):
            name += f" ({exercise['side'].strip()})"
        if exercise.get("note"):
            name += f" — {exercise['note'].strip()}"
        for item in exercise.get("sets", []):
            rows.append({"exercise_name": name, "weight_kg": item.get("weight_kg"),
                         "sets": 1, "reps": item.get("reps")})
    return rows


def _validate_exercises(value):
    if not isinstance(value, list) or not value or len(value) > C.MAX_WORKOUT_ENTRIES:
        raise ValueError("invalid_exercises")
    clean = []
    for exercise in value:
        if not isinstance(exercise, dict):
            raise ValueError("invalid_exercise")
        name = exercise.get("exercise_name")
        sets = exercise.get("sets")
        if not isinstance(name, str) or not name.strip() or len(name) > C.MAX_NAME_CHARS:
            raise ValueError("invalid_exercise_name")
        if not isinstance(sets, list) or not sets:
            raise ValueError("invalid_sets")
        clean_sets = []
        for item in sets:
            if not isinstance(item, dict):
                raise ValueError("invalid_set")
            weight, reps = item.get("weight_kg"), item.get("reps")
            if weight is not None and (isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 <= weight <= C.MAX_WEIGHT_KG):
                raise ValueError("invalid_weight")
            if reps is not None and (isinstance(reps, bool) or not isinstance(reps, int) or not 0 < reps <= C.MAX_REPS):
                raise ValueError("invalid_reps")
            clean_sets.append({"weight_kg": round(float(weight), 2) if weight is not None else None,
                               "reps": reps})
        side, note = exercise.get("side"), exercise.get("note")
        if side is not None and (not isinstance(side, str) or len(side) > 40):
            raise ValueError("invalid_side")
        if note is not None and (not isinstance(note, str) or len(note) > 120):
            raise ValueError("invalid_note")
        clean.append({"exercise_name": name.strip(), "sets": clean_sets,
                      "side": side.strip() if side else None,
                      "note": note.strip() if note else None})
    return clean


def _affirmative(text):
    normalized = (text or "").strip().casefold().strip(" .,!?—")
    first = normalized.split()[0] if normalized else ""
    if first in {"да", "ага", "угу", "yes", "yep", "подтверждаю"}:
        return True
    if first in {"нет", "не", "no", "отмена", "отмени"}:
        return False
    return None


def start(ctx, reservation, resolved_date):
    lease = _lease(reservation)
    store.mark_clarification(reservation.action_id, C.INTENT_START_STRENGTH_DRAFT, 1.0, **lease)
    pending_id = store.create_pending(
        reservation.action_id, ctx, C.INTENT_START_STRENGTH_DRAFT,
        {"stage": "collecting", "resolved_date": resolved_date, "exercises": []},
        ["exercises"], ttl_minutes=180,
    )
    return conversation_router.RouterOutcome(
        reservation.action_id, "clarification",
        f"Начал силовую за {resolved_date}. Присылай упражнения по одному или все сразу; когда закончишь, напиши «готово».",
        pending_id=pending_id,
    )


def preview_existing(ctx, reservation, args):
    lease = _lease(reservation)
    state = {"stage": "preview", "resolved_date": args["resolved_date"],
             "tool_entries": args["entries"], "exercises": []}
    store.mark_clarification(reservation.action_id, C.INTENT_START_STRENGTH_DRAFT, 1.0, **lease)
    pending_id = store.create_pending(reservation.action_id, ctx,
                                      C.INTENT_START_STRENGTH_DRAFT, state,
                                      ["confirmation"], ttl_minutes=180)
    display = {"resolved_date": args["resolved_date"], "exercises": []}
    for row in args["entries"]:
        display["exercises"].append({"exercise_name": row["exercise_name"],
                                     "side": None, "sets": [{"weight_kg": row.get("weight_kg"),
                                                                "reps": row.get("reps")} ]})
    return conversation_router.RouterOutcome(reservation.action_id, "clarification",
                                              _preview(display), pending_id=pending_id)


def handle(pending, ctx, exec_ctx, gemini):
    reservation = store.reserve(ctx)
    action_id = reservation.action_id
    exec_ctx.action_id = action_id
    exec_ctx.processing_token = reservation.processing_token
    exec_ctx.processing_fence = reservation.processing_fence
    lease = _lease(reservation)
    if not reservation.is_new:
        result = json.loads(reservation.result_json) if reservation.result_json else None
        return conversation_router.RouterOutcome(action_id,
            "in_progress" if reservation.in_progress else "duplicate",
            "Запрос уже обрабатывается." if reservation.in_progress else reservation.response_text,
            result=result, write_committed=bool(result))
    state = json.loads(pending["partial_arguments_json"])
    stage = state.get("stage")
    exact_reply = (ctx.reply_to_message_id is not None and
                   str(ctx.reply_to_message_id) == str(pending.get("clarification_question_message_id")))
    if stage == "preview" and exact_reply:
        answer = _affirmative(ctx.input_text)
        if answer is None:
            store.mark_noop(action_id, C.INTENT_START_STRENGTH_DRAFT, **lease)
            return conversation_router.RouterOutcome(action_id, "clarification",
                "Ответь на preview через Reply: «да» для записи или «нет» для отмены.",
                pending_id=pending["pending_id"])
        if not answer:
            store.mark_noop(action_id, C.INTENT_START_STRENGTH_DRAFT, **lease)
            store.resolve_pending(pending["pending_id"], action_id)
            return conversation_router.RouterOutcome(action_id, "general", "Черновик силовой отменён.")
        if not store.claim_pending_for_resolution(pending["pending_id"], action_id):
            store.mark_noop(action_id, C.INTENT_START_STRENGTH_DRAFT, **lease)
            return conversation_router.RouterOutcome(action_id, "general", "Этот preview уже обработан.")
        entries = state.get("tool_entries") or _flatten(state)
        envelope = validation.ValidationResult(ok=True, intent=C.INTENT_LOG_STRENGTH,
            confidence=1.0, arguments={"date_ref": {"kind": "absolute", "value": state["resolved_date"]},
                                       "fact_status": C.FACT_STATUS_COMPLETED, "entries": entries})
        verdict = validation.validate(envelope, local_now=exec_ctx.local_now)
        if not verdict.ok or verdict.tool != "log_strength_workout":
            store.mark_rejected(action_id, verdict.error_code or C.ERR_BAD_TYPE,
                                verdict.error_detail, intent=C.INTENT_LOG_STRENGTH, **lease)
            store.release_pending_claim(pending["pending_id"], action_id)
            return conversation_router.RouterOutcome(action_id, "rejected", conversation_router.NO_WRITE_MESSAGE)
        exec_ctx.pending_id = pending["pending_id"]
        store.record_validated(action_id, intent=verdict.intent, tool_name=verdict.tool,
                               validated_arguments=verdict.arguments, **lease)
        try:
            result = tools.execute(verdict.tool, verdict.arguments, exec_ctx)
        except Exception as exc:
            store.release_pending_claim(pending["pending_id"], action_id)
            return conversation_router._tool_failure_outcome(action_id, verdict.tool, exc, lease)
        return conversation_router.RouterOutcome(action_id, "confirmation",
            conversation_router._confirm_message(C.INTENT_LOG_STRENGTH, verdict.arguments,
                                                  result, local_now=exec_ctx.local_now),
            result=result, write_committed=result.get("created_count", 0) > 0)
    if stage == "preview":
        store.mark_noop(action_id, C.INTENT_START_STRENGTH_DRAFT, **lease)
        return conversation_router.RouterOutcome(action_id, "clarification",
            "Жду Reply на preview: «да» для записи или «нет» для отмены.", pending_id=pending["pending_id"])

    normalized = " ".join((ctx.input_text or "").casefold().strip().split())
    parsed_update = deterministic_strength.parse_draft_update(ctx.input_text)
    deterministic_call = None
    if stage == "awaiting_reps" and normalized.isdigit():
        deterministic_call = ("provide_strength_repetitions",
                              {"confidence": 1.0, "reps": int(normalized)})
    elif normalized.strip(" .!?") in {"готово", "закончил", "закончила", "done", "finish"}:
        deterministic_call = ("finish_strength_draft", {"confidence": 1.0})
    elif normalized.strip(" .!?") in {
        "отмена", "отмени", "отмени тренировку", "cancel", "cancel workout",
    }:
        deterministic_call = ("cancel_strength_draft", {"confidence": 1.0})
    elif parsed_update is not None:
        deterministic_call = ("append_strength_exercises",
                              {"confidence": 1.0, "exercises": parsed_update})

    if deterministic_call is not None:
        name, args = deterministic_call
        store.record_router(
            action_id, model="deterministic-strength-draft-v2",
            response_sha256=store._sha256(json.dumps(
                {"name": name, "args": args}, ensure_ascii=False, sort_keys=True,
            )), intent=C.INTENT_START_STRENGTH_DRAFT,
            confidence=1.0, latency_ms=0, attempt_count=0,
            prompt_version="strength-draft-v2", **lease,
        )
    else:
        planner = json.dumps({"current_message": ctx.input_text,
                              "draft_state": {"stage": stage,
                                              "exercise_count": len(state.get("exercises", [])),
                                              "missing_reps": state.get("missing_reps")}},
                             ensure_ascii=False, sort_keys=True)
        try:
            gen = gemini.generate_tool_call(SYSTEM, planner, DECLARATIONS, allowed_names=ALLOWED)
        except (GeminiUnavailable, GeminiError) as exc:
            store.mark_failed(action_id, "strength_draft_provider_unavailable", type(exc).__name__, **lease)
            return conversation_router.RouterOutcome(action_id, "outage",
                "Relay временно недоступен; черновик сохранён. Повтори последнее сообщение позже.")
        store.record_router(action_id, model=gen.model, response_sha256=store._sha256(gen.canonical_call),
                            intent=C.INTENT_START_STRENGTH_DRAFT,
                            confidence=gen.args.get("confidence") if isinstance(gen.args, dict) else None,
                            latency_ms=gen.latency_ms, attempt_count=gen.attempt_count,
                            prompt_version="strength-draft-v2",
                            relay_metadata=getattr(gen, "relay_metadata", None), **lease)
        name, args = gen.name, gen.args
    if name not in ALLOWED or not isinstance(args, dict):
        store.mark_rejected(action_id, C.ERR_BAD_TYPE, "draft_tool", **lease)
        return conversation_router.RouterOutcome(action_id, "rejected", "Не понял сообщение для активной силовой. Пришли упражнение или напиши «готово».")
    if name == "cancel_strength_draft":
        store.mark_noop(action_id, C.INTENT_START_STRENGTH_DRAFT, **lease)
        store.resolve_pending(pending["pending_id"], action_id)
        return conversation_router.RouterOutcome(action_id, "general", "Черновик силовой отменён.")
    if name == "unrelated_to_strength_draft":
        store.mark_noop(action_id, C.INTENT_START_STRENGTH_DRAFT, **lease)
        return conversation_router.RouterOutcome(action_id, "general",
            "Сейчас собираю силовую: пришли упражнение, напиши «готово» или «отмени тренировку».")
    if name == "provide_strength_repetitions":
        reps = args.get("reps")
        if stage != "awaiting_reps" or isinstance(reps, bool) or not isinstance(reps, int) or not 0 < reps <= C.MAX_REPS:
            store.mark_rejected(action_id, C.ERR_BAD_TYPE, "draft_reps", **lease)
            return conversation_router.RouterOutcome(action_id, "clarification", "Нужно целое число повторов.", pending_id=pending["pending_id"])
        def fill(current, missing):
            exercise_index, set_index = current["missing_reps"]
            current["exercises"][exercise_index]["sets"][set_index]["reps"] = reps
            current["missing_reps"] = None
            current["stage"] = "collecting"
            return current, ["exercises"]
        store.update_pending_arguments(pending["pending_id"], fill)
        store.mark_noop(action_id, C.INTENT_START_STRENGTH_DRAFT, **lease)
        return conversation_router.RouterOutcome(action_id, "clarification",
            f"Добавил {reps} повторов. Присылай следующее упражнение или напиши «готово».",
            pending_id=pending["pending_id"])
    if name == "finish_strength_draft":
        if not state.get("exercises"):
            store.mark_noop(action_id, C.INTENT_START_STRENGTH_DRAFT, **lease)
            return conversation_router.RouterOutcome(action_id, "clarification",
                "В черновике пока нет упражнений. Пришли первое упражнение.", pending_id=pending["pending_id"])
        if state.get("missing_reps") is not None:
            store.mark_noop(action_id, C.INTENT_START_STRENGTH_DRAFT, **lease)
            return conversation_router.RouterOutcome(action_id, "clarification",
                "Сначала укажи недостающие повторы для последнего подхода.", pending_id=pending["pending_id"])
        def finish(current, missing):
            current["stage"] = "preview"
            return current, ["confirmation"]
        updated = store.update_pending_arguments(pending["pending_id"], finish)
        store.mark_noop(action_id, C.INTENT_START_STRENGTH_DRAFT, **lease)
        return conversation_router.RouterOutcome(action_id, "clarification",
            _preview(json.loads(updated["partial_arguments_json"])), pending_id=pending["pending_id"])
    try:
        exercises = _validate_exercises(args.get("exercises"))
    except ValueError as exc:
        store.mark_rejected(action_id, C.ERR_BAD_TYPE, str(exc), **lease)
        return conversation_router.RouterOutcome(action_id, "clarification",
            "Не смог разобрать подходы. Пришли в формате «упражнение: вес×повторы, вес×повторы».",
            pending_id=pending["pending_id"])
    missing_position = None
    def append(current, missing):
        nonlocal missing_position
        if len(_flatten(current)) + sum(len(item["sets"]) for item in exercises) > C.MAX_WORKOUT_ENTRIES:
            raise ValueError("too_many_entries")
        base = len(current["exercises"])
        current["exercises"].extend(exercises)
        for ex_offset, exercise in enumerate(exercises):
            for set_index, item in enumerate(exercise["sets"]):
                if item["reps"] is None:
                    missing_position = [base + ex_offset, set_index]
                    break
            if missing_position is not None:
                break
        current["missing_reps"] = missing_position
        current["stage"] = "awaiting_reps" if missing_position is not None else "collecting"
        return current, (["reps"] if missing_position is not None else ["exercises"])
    store.update_pending_arguments(pending["pending_id"], append)
    store.mark_noop(action_id, C.INTENT_START_STRENGTH_DRAFT, **lease)
    first = exercises[0]
    if missing_position is not None:
        missing_exercise = exercises[missing_position[0] - (len(state.get("exercises", [])))]
        missing_set = missing_exercise["sets"][missing_position[1]]
        weight = missing_set.get("weight_kg")
        weight_text = f"{weight:g}" if weight is not None else "без указанного веса"
        return conversation_router.RouterOutcome(action_id, "clarification",
            f"Сколько повторов было с весом {weight_text} в «{missing_exercise['exercise_name']}»?",
            pending_id=pending["pending_id"])
    count = sum(len(item["sets"]) for item in exercises)
    return conversation_router.RouterOutcome(action_id, "clarification",
        f"Добавил {first['exercise_name']}: {count} подхода. Присылай следующее упражнение или напиши «готово».",
        pending_id=pending["pending_id"])
