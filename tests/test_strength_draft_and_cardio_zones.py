import datetime as dt
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import conversation_contract as C
import canonical_read_model
import conversation_read_models
import conversation_router
import conversation_store as store
import conversation_tools
import grounded_responder
import strength_draft
import strength_presentation
import telegram_bot
import workouts_db
from conversation_fakes import FakeBot, TempDBCase, message


NOW = dt.datetime(2026, 7, 18, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))


class OneCall:
    def __init__(self, name, args):
        self.name, self.args = name, args

    def generate_tool_call(self, *args, **kwargs):
        return SimpleNamespace(name=self.name, args=self.args,
            canonical_call=json.dumps({"name": self.name, "args": self.args}, sort_keys=True),
            model="real-contract-fake", latency_ms=1, attempt_count=1,
            relay_metadata={"request_id": "opaque-test"})


def context(message_id, text, reply=None, user=42):
    ctx = store.ActionContext(source="telegram", chat_id="1", user_id=str(user),
                              message_id=str(message_id), input_text=text,
                              reply_to_message_id=str(reply) if reply else None)
    exec_ctx = conversation_tools.ExecContext(action_id="", source="telegram",
        chat_id="1", message_id=str(message_id), local_now=NOW,
        reply_to_message_id=str(reply) if reply else None)
    return ctx, exec_ctx


class CardioCanonicalConfirmationTests(TempDBCase):
    def test_confirmation_and_day_read_use_persisted_zones(self):
        ctx, exec_ctx = context(1, "photo")
        reservation = store.reserve(ctx)
        exec_ctx.action_id = reservation.action_id
        exec_ctx.processing_token = reservation.processing_token
        exec_ctx.processing_fence = reservation.processing_fence
        args = {
            "resolved_date": "2026-07-17", "activity_type": "Race Walking",
            "duration_minutes": 22.7, "distance_km": None, "avg_hr_bpm": 141,
            "calories_kcal": 155, "strain": 8.4, "max_hr_bpm": None,
            "steps": 1772, "start_time": "12:08",
            "hr_zone_minutes": [0, 63 / 60, 19 + 2 / 60, 2 / 60, 0, 0],
        }
        store.record_router(reservation.action_id, model="vision", response_sha256=None,
            intent=C.INTENT_LOG_CARDIO, confidence=.99, latency_ms=1,
            attempt_count=1, prompt_version="test",
            processing_token=reservation.processing_token,
            processing_fence=reservation.processing_fence)
        store.record_validated(reservation.action_id, intent=C.INTENT_LOG_CARDIO,
            tool_name="log_cardio", validated_arguments=args,
            processing_token=reservation.processing_token,
            processing_fence=reservation.processing_fence)
        result = conversation_tools.execute("log_cardio", args, exec_ctx)
        self.assertEqual(result["data"]["cardio"]["zones"]["2"], "19:02")
        message = conversation_router._confirm_message(C.INTENT_LOG_CARDIO, {}, result, NOW)
        for expected in ("Race Walking", "22:42", "Strain: 8.4", "1 772",
                         "Zone 5 — 00:00", "Zone 3 — 00:02",
                         "Zone 2 — 19:02", "Zone 1 — 01:03", "Zone 0 — 00:00"):
            self.assertIn(expected, message)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        snapshot = conversation_read_models.day_snapshot(conn, "2026-07-17")
        conn.close()
        cardio = snapshot["day"]["activities"]["manual_cardio"][0]
        self.assertEqual(cardio["zones"]["1"], "1:03")
        read_result = {"data": snapshot}
        self.assertIn("Zone 2 — 19:02", conversation_router._read_message(C.INTENT_GET_DAY_SNAPSHOT, read_result))


class StrengthDraftTests(TempDBCase):
    def _start(self):
        ctx, _ = context(10, "Запиши силовую за вчера")
        reservation = store.reserve(ctx)
        outcome = strength_draft.start(ctx, reservation, "2026-07-17")
        store.set_clarification_message_id(outcome.pending_id, 1000)
        return outcome.pending_id

    def _handle(self, pending_id, mid, text, name, args, reply=None):
        pending = store.get_pending(pending_id)
        ctx, exec_ctx = context(mid, text, reply)
        result = strength_draft.handle(pending, ctx, exec_ctx, OneCall(name, args))
        if result.pending_id:
            store.set_clarification_message_id(result.pending_id, 1000 + mid)
        return result

    def test_multi_message_missing_reps_preview_atomic_confirm_and_duplicate(self):
        pending_id = self._start()
        exercises = [
            ("Chest press", [(79, 11), (79, 8), (79, 8)]),
            ("Vertical row", [(70, 10), (70, 8)]),
            ("Тяга снизу", [(17.5, 9), (17.5, 7)]),
        ]
        mid = 20
        for name, sets in exercises:
            parsed = [{"exercise_name": name, "side": None, "note": None,
                       "sets": [{"weight_kg": w, "reps": r} for w, r in sets]}]
            outcome = self._handle(pending_id, mid, f"{name}",
                "append_strength_exercises", {"confidence": .99, "exercises": parsed})
            self.assertIn("Добавил", outcome.message)
            mid += 1
        incomplete = [{"exercise_name": "Вертикальная тяга одной рукой", "side": "по 1 руке",
                       "note": None, "sets": [{"weight_kg": 30, "reps": None}]}]
        outcome = self._handle(pending_id, mid, "30x", "append_strength_exercises",
                               {"confidence": .99, "exercises": incomplete})
        self.assertIn("Сколько повторов", outcome.message)
        mid += 1
        outcome = self._handle(pending_id, mid, "10", "provide_strength_repetitions",
                               {"confidence": .99, "reps": 10})
        self.assertIn("Добавил 10 повторов", outcome.message)
        mid += 1
        outcome = self._handle(pending_id, mid, "готово", "finish_strength_draft",
                               {"confidence": .99})
        self.assertIn("Проверь силовую", outcome.message)
        preview_message = 1000 + mid
        mid += 1
        pending = store.get_pending(pending_id)
        ctx, exec_ctx = context(mid, "да", preview_message)
        confirmed = strength_draft.handle(pending, ctx, exec_ctx,
                                           OneCall("unrelated_to_strength_draft", {"confidence": 1}))
        self.assertTrue(confirmed.write_committed)
        self.assertIn("Chest press", confirmed.message)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM workout_exercises").fetchone()[0], 8)
        self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
        conn.close()
        duplicate = strength_draft.handle(store.get_pending(pending_id) or pending, ctx, exec_ctx,
                                           OneCall("unrelated_to_strength_draft", {"confidence": 1}))
        self.assertEqual(duplicate.kind, "duplicate")
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM workout_exercises").fetchone()[0], 8)
        conn.close()

    def test_drafts_are_isolated_by_user(self):
        pending_id = self._start()
        self.assertIsNotNone(store.active_pending("telegram", "1", "42"))
        self.assertIsNone(store.active_pending("telegram", "1", "99"))
        self.assertEqual(store.get_pending(pending_id)["status"], C.PENDING_OPEN)

    def test_live_router_serializes_concurrent_updates_for_same_user(self):
        active = 0
        peak = 0
        guard = threading.Lock()

        def observed(_message, _text):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(.04)
            with guard:
                active -= 1

        telegram_bot._conversation_locks.clear()
        with patch.object(telegram_bot, "_route_via_conversation_serial",
                          side_effect=observed):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [
                    pool.submit(
                        telegram_bot.route_via_conversation,
                        message(mid, f"exercise-{mid}"), f"exercise-{mid}",
                    )
                    for mid in (1, 2, 3)
                ]
                for future in futures:
                    future.result()
        self.assertEqual(peak, 1)
        telegram_bot._conversation_locks.clear()

    def test_live_telegram_multi_update_flow_survives_restart_and_reads_back(self):
        real_bot = telegram_bot.bot
        fake_bot = FakeBot(start_id=2000)
        telegram_bot.bot = fake_bot
        telegram_bot._conversation_locks.clear()

        def send(mid, text, tool_name, args, *, reply=None):
            # Dropping process-local locks between every update simulates a bot
            # restart. The draft itself must survive entirely through SQLite.
            telegram_bot._conversation_locks.clear()
            with patch.object(telegram_bot, "get_kiev_time", return_value=NOW), \
                 patch.object(telegram_bot.phase2_flags, "bounded_agent_enabled", return_value=True), \
                 patch.object(telegram_bot, "_build_gemini_client",
                              return_value=OneCall(tool_name, args)):
                telegram_bot.route_via_conversation(
                    message(mid, text, reply_to_message_id=reply), text
                )

        try:
            send(100, "Запиши силовую за вчера", "start_strength_draft", {
                "confidence": .99,
                "date_ref": {"kind": "yesterday", "value": None},
            })
            pending = store.active_pending("telegram", "1", "42")
            pending_id = pending["pending_id"]

            send(101, "Chest press: 79x11, 79x8", "append_strength_exercises", {
                "confidence": .99,
                "exercises": [{"exercise_name": "Chest press", "side": None,
                               "note": None, "sets": [
                                   {"weight_kg": 79, "reps": 11},
                                   {"weight_kg": 79, "reps": 8},
                               ]}],
            })
            send(102, "Vertical row: 70x", "append_strength_exercises", {
                "confidence": .99,
                "exercises": [{"exercise_name": "Vertical row", "side": None,
                               "note": None,
                               "sets": [{"weight_kg": 70, "reps": None}]}],
            })
            self.assertEqual(
                json.loads(store.get_pending(pending_id)["partial_arguments_json"])["stage"],
                "awaiting_reps",
            )
            send(103, "10", "provide_strength_repetitions", {
                "confidence": .99, "reps": 10,
            })
            send(104, "Тяга снизу: 17.5x9", "append_strength_exercises", {
                "confidence": .99,
                "exercises": [{"exercise_name": "Тяга снизу", "side": None,
                               "note": None,
                               "sets": [{"weight_kg": 17.5, "reps": 9}]}],
            })
            send(105, "готово", "finish_strength_draft", {"confidence": .99})
            preview_id = fake_bot.sent[-1].message_id
            preview_state = json.loads(
                store.get_pending(pending_id)["partial_arguments_json"]
            )
            self.assertEqual(preview_state["stage"], "preview")
            self.assertEqual(len(preview_state["exercises"]), 3)

            send(106, "да", "unrelated_to_strength_draft", {"confidence": 1},
                 reply=preview_id)
            # Telegram may redeliver the same confirmed update after reconnect.
            send(106, "да", "unrelated_to_strength_draft", {"confidence": 1},
                 reply=preview_id)
            send(107, "Что было вчера?", "get_day_snapshot", {
                "confidence": .99,
                "date_ref": {"kind": "yesterday", "value": None},
            })

            conn = sqlite3.connect(self.db)
            try:
                rows = conn.execute(
                    "SELECT exercise_name, reps, origin_action_id "
                    "FROM workout_exercises ORDER BY id"
                ).fetchall()
                self.assertEqual(len(rows), 4)
                self.assertEqual(len({row[2] for row in rows}), 1)
                self.assertEqual([row[1] for row in rows], [11, 8, 10, 9])
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                conn.close()
            self.assertIn("Chest press", fake_bot.sent[-1].text)
            self.assertEqual(store.get_pending(pending_id)["status"], C.PENDING_RESOLVED)
        finally:
            telegram_bot.bot = real_bot
            telegram_bot._conversation_locks.clear()


class ExactTelegramStrengthRegressionTests(TempDBCase):
    TEXT = """Запиши силовую за вчера
Chest press: 79x11, 79x8, 79x8
Vertical row: 70x10, 70x8
Тяга снизу в тренажере: 17.5x9, 17.5x7
Бабочка стоя на низ груди: 20x12, 20x10
Махи в кроссовере: 14x12, 14x10
; :Вертикальная тяга по 1 руке в тренажере: 30x9"""

    PRODUCTION_TEXT = """Жим в тренажере под наклоном: 30x11, 30x9
Горизонтальная тяга в хаммере: 30x12, 30x10
Махи стоя с гантелями: 12.5x15, 12.5x13
Вертикальная тяга узким хватом: 43x14, 52x10
Разгибания на трицепс в кроссовере: 57x12, 57x8
Молотки сидя: 12.5x14
Запиши эту"""

    def test_production_one_message_is_authoritative_over_bad_model_set_counts(self):
        # Reproduce the production provider defect: the model returned each
        # already-expanded set with sets=2. The explicit server grammar must
        # retain the real 11 sets without invoking the provider.
        bad_entries = [
            {"exercise_name": f"wrong-{index}", "weight_kg": 1,
             "sets": 2, "reps": 1}
            for index in range(11)
        ]
        provider = OneCall("log_strength_workout", {
            "confidence": .99,
            "date_ref": {"kind": "today", "value": None},
            "fact_status": "completed", "entries": bad_entries,
        })
        ctx = store.ActionContext(source="telegram", chat_id="1", user_id="42",
                                  message_id="production-one", input_text=self.PRODUCTION_TEXT)
        exec_ctx = conversation_tools.ExecContext(
            action_id="", source="telegram", chat_id="1", message_id="production-one",
            local_now=NOW,
        )
        with patch("phase2_flags.bounded_agent_enabled", return_value=True):
            first = conversation_router.route(ctx, exec_ctx, local_now=NOW, gemini=provider)
            replay = conversation_router.route(ctx, exec_ctx, local_now=NOW, gemini=provider)
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute(
                "SELECT exercise_name,weight,sets,reps,origin_action_id "
                "FROM workout_exercises ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 11)
        self.assertEqual(len({row[4] for row in rows}), 1)
        self.assertTrue(all(row[2] == 1 for row in rows))
        self.assertEqual([row[0] for row in rows], [
            "Жим в тренажере под наклоном", "Жим в тренажере под наклоном",
            "Горизонтальная тяга в хаммере", "Горизонтальная тяга в хаммере",
            "Махи стоя с гантелями", "Махи стоя с гантелями",
            "Вертикальная тяга узким хватом", "Вертикальная тяга узким хватом",
            "Разгибания на трицепс в кроссовере", "Разгибания на трицепс в кроссовере",
            "Молотки сидя",
        ])
        self.assertEqual([row[3] for row in rows], [11, 9, 12, 10, 15, 13, 14, 10, 12, 8, 14])
        self.assertEqual(first.kind, "confirmation")
        self.assertEqual(replay.kind, "duplicate")
        self.assertIn("6 упражнений / 11 подходов", first.message)

    def test_exact_single_update_accepts_native_redundant_yesterday_value(self):
        entries = []
        for name, sets in (
            ("Chest press", ((79, 11), (79, 8), (79, 8))),
            ("Vertical row", ((70, 10), (70, 8))),
            ("Тяга снизу в тренажере", ((17.5, 9), (17.5, 7))),
            ("Бабочка стоя на низ груди", ((20, 12), (20, 10))),
            ("Махи в кроссовере", ((14, 12), (14, 10))),
            ("Вертикальная тяга по 1 руке в тренажере", ((30, 9),)),
        ):
            entries.extend({"exercise_name": name, "weight_kg": weight,
                            "sets": 1, "reps": reps} for weight, reps in sets)
        provider_call = OneCall("log_strength_workout", {
            "confidence": .99,
            # This is the exact native call shape observed in production.
            "date_ref": {"kind": "yesterday", "value": "yesterday"},
            "fact_status": "completed", "entries": entries,
        })
        ctx = store.ActionContext(source="telegram", chat_id="1", user_id="42",
                                  message_id="291", input_text=self.TEXT)
        exec_ctx = conversation_tools.ExecContext(action_id="", source="telegram",
            chat_id="1", message_id="291", local_now=NOW)
        with patch("phase2_flags.bounded_agent_enabled", return_value=True):
            outcome = conversation_router.route(ctx, exec_ctx, local_now=NOW,
                                                gemini=provider_call)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workout_exercises").fetchone()[0], 12)
            action = conn.execute("SELECT status,error_code FROM conversation_actions WHERE message_id='291'").fetchone()
            self.assertEqual(action, ("succeeded", None))
        finally:
            conn.close()
        self.assertEqual(outcome.kind, "confirmation")
        self.assertIn("12 подход", outcome.message)


class DeterministicStrengthRoutingMatrixTests(TempDBCase):
    class NeverCallProvider:
        def generate_tool_call(self, *args, **kwargs):
            raise AssertionError("obvious strength logs must bypass the LLM")

    def _route(self, message_id, text):
        ctx, exec_ctx = context(message_id, text)
        return conversation_router.route(
            ctx, exec_ctx, local_now=NOW, gemini=self.NeverCallProvider()
        )

    def test_single_message_matrix_bypasses_llm(self):
        cases = {
            "one-exercise": "Chest press: 79x10",
            "many-exercises": (
                "Chest press: 79x10, 79x8\n"
                "Тяга сверху: 43x12, 43x10"
            ),
            "with-prefix": (
                "запиши\nChest press: 79x10\nТяга сверху: 43x12"
            ),
            "without-prefix": "Тяга сверху: 43x12, 43x10",
            "trailing-directive": "Chest press: 79x10\nзапиши эту",
            "mixed-names": (
                "Chest press: 79x10\n"
                "Вертикальная тяга: 43x12"
            ),
            "inline-date-metadata": (
                "Chest press: 79x10, 79x8  тренировка за 18 июля"
            ),
        }
        for index, (name, text) in enumerate(cases.items(), start=1):
            with self.subTest(name=name):
                outcome = self._route(f"matrix-{index}", text)
                self.assertEqual(outcome.kind, "confirmation")
                action = store.get_action(outcome.action_id)
                self.assertEqual(action["router_model"], "deterministic-strength-v2")
                self.assertEqual(action["tool_name"], "log_strength_workout")

    def test_multi_message_draft_updates_are_deterministic(self):
        first_ctx, first_exec = context(800, "запиши силовую")
        started = conversation_router.route(
            first_ctx, first_exec, local_now=NOW, gemini=self.NeverCallProvider()
        )
        self.assertEqual(started.kind, "clarification")
        pending = store.active_pending("telegram", "1", "42")

        update_ctx, update_exec = context(
            801, "Chest press: 79x10\nВертикальная тяга: 43x12"
        )
        handled = strength_draft.handle(
            pending, update_ctx, update_exec, gemini=self.NeverCallProvider()
        )
        self.assertIn(handled.kind, {"clarification", "confirmation"})
        state = json.loads(
            store.get_pending(pending["pending_id"])["partial_arguments_json"]
        )
        self.assertEqual(len(state["exercises"]), 2)


class StrengthGroupedPresentationTests(TempDBCase):
    def test_grouping_preserves_order_fractional_weights_notes_and_single_set(self):
        rows = [
            {"id": 1, "exercise_name": "Первое", "weight": 79.0, "sets": 1, "reps": 11},
            {"id": 2, "exercise_name": "Первое", "weight": 79.0, "sets": 1, "reps": 8},
            {"id": 3, "exercise_name": "Второе — левая сторона", "weight": 17.5, "sets": 1, "reps": 9},
            {"id": 4, "exercise_name": "Первое", "weight": 79.0, "sets": 1, "reps": 7},
            {"id": 5, "exercise_name": "Одиночное", "weight": 30.0, "sets": 1, "reps": 9},
        ]
        grouped, lines = strength_presentation.render_lines(rows)
        self.assertEqual(grouped["exercise_count"], 3)
        self.assertEqual(grouped["set_count"], 5)
        self.assertEqual(lines, [
            "Первое: 79×11, 79×8, 79×7",
            "Второе — левая сторона: 17.5×9",
            "Одиночное: 30×9",
        ])

    def test_confirmation_and_canonical_read_share_grouped_persisted_rows(self):
        ctx, exec_ctx = context(700, "persisted strength")
        reservation = store.reserve(ctx)
        exec_ctx.action_id = reservation.action_id
        exec_ctx.processing_token = reservation.processing_token
        exec_ctx.processing_fence = reservation.processing_fence
        args = {"resolved_date": "2026-07-17", "entries": [
            {"exercise_name": "A", "weight_kg": 10, "sets": 1, "reps": 10},
            {"exercise_name": "A", "weight_kg": 10, "sets": 1, "reps": 8},
            {"exercise_name": "B (правая) — медленно", "weight_kg": 7.5, "sets": 1, "reps": 12},
        ]}
        store.record_router(reservation.action_id, model="fixture", response_sha256=None,
            intent=C.INTENT_LOG_STRENGTH, confidence=.99, latency_ms=1,
            attempt_count=1, prompt_version="test",
            processing_token=reservation.processing_token,
            processing_fence=reservation.processing_fence)
        store.record_validated(reservation.action_id, intent=C.INTENT_LOG_STRENGTH,
            tool_name="log_strength_workout", validated_arguments=args,
            processing_token=reservation.processing_token,
            processing_fence=reservation.processing_fence)
        result = conversation_tools.execute("log_strength_workout", args, exec_ctx)
        confirmation = conversation_router._confirm_message(C.INTENT_LOG_STRENGTH, {}, result, NOW)
        conn = sqlite3.connect(self.db); conn.row_factory = sqlite3.Row
        snapshot = conversation_read_models.day_snapshot(conn, "2026-07-17"); conn.close()
        read = conversation_router._read_message(C.INTENT_GET_DAY_SNAPSHOT, {"data": snapshot})
        for line in ("A: 10×10, 10×8", "B (правая) — медленно: 7.5×12"):
            self.assertEqual(confirmation.count(line), 1)
            self.assertEqual(read.count(line), 1)
        self.assertIn("2 упражнений / 3 подходов", confirmation)
        self.assertIn("2 упражнений / 3 подходов", read)

    def test_duplicate_router_delivery_keeps_same_grouped_confirmation(self):
        entries = [
            {"exercise_name": "A", "weight_kg": 20, "sets": 1, "reps": 10},
            {"exercise_name": "A", "weight_kg": 20, "sets": 1, "reps": 8},
        ]
        call = OneCall("log_strength_workout", {"confidence": .99,
            "date_ref": {"kind": "yesterday", "value": None},
            "fact_status": "completed", "entries": entries})
        ctx = store.ActionContext(source="telegram", chat_id="1", user_id="42",
                                  message_id="dup-group", input_text="strength")
        exec_ctx = conversation_tools.ExecContext(action_id="", source="telegram",
            chat_id="1", message_id="dup-group", local_now=NOW)
        with patch("phase2_flags.bounded_agent_enabled", return_value=True):
            first = conversation_router.route(ctx, exec_ctx, local_now=NOW, gemini=call)
            duplicate = conversation_router.route(ctx, exec_ctx, local_now=NOW, gemini=call)
        self.assertEqual(duplicate.kind, "duplicate")
        self.assertEqual(duplicate.message, first.message)
        conn = sqlite3.connect(self.db)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM workout_exercises").fetchone()[0], 2)
        conn.close()

    def test_weekly_canonical_reads_use_the_same_grouping(self):
        conn = workouts_db.connect()
        workouts_db.insert_workout_row(conn, "2026-07-10", "A", 10, 1, 10,
                                       source_key="week:1")
        workouts_db.insert_workout_row(conn, "2026-07-10", "A", 10, 1, 8,
                                       source_key="week:2")
        workouts_db.insert_workout_row(conn, "2026-07-10", "B", 7.5, 1, 12,
                                       source_key="week:3")
        conn.commit()
        evidence = canonical_read_model.weekly_snapshot(conn, NOW, factors=())
        legacy = conversation_read_models.week_summary(conn, NOW)
        conn.close()
        rendered_v2 = grounded_responder.render(
            C.INTENT_GET_WEEK_SUMMARY, {"data": evidence}
        )
        rendered_v1 = conversation_router._read_message(
            C.INTENT_GET_WEEK_SUMMARY, {"data": legacy}
        )
        for rendered in (rendered_v2, rendered_v1):
            self.assertIn("2 упражнений / 3 подходов", rendered)
            self.assertIn("A: 10×10, 10×8", rendered)
            self.assertIn("B: 7.5×12", rendered)
