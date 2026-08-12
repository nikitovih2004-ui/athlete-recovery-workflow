"""Local end-to-end: real handler entrypoint, fake Telegram bot + fake Gemini."""
import os
import datetime as dt
import sqlite3
import sys
import types
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import conversation_contract as C
import conversation_router
import conversation_store as store
import telegram_bot
import canonical_read_model
from gemini_client import GeminiUnavailable
from conversation_fakes import (
    FakeBot, TempDBCase, envelope, message, single_response, make_client,
    HttpResponse,
)


class E2ETests(TempDBCase):
    def setUp(self):
        super().setUp()
        conn = store.connect()
        telegram_bot.phase2_store.migrate(conn)
        telegram_bot.morning_context.ensure_table(conn)
        conn.commit()
        conn.close()
        self._real_bot = telegram_bot.bot
        self.bot = FakeBot()
        telegram_bot.bot = self.bot
        self._image_patch = patch.object(
            telegram_bot,
            "_prepare_cardio_image",
            side_effect=lambda value: (value, "image/jpeg"),
        )
        self._image_patch.start()

    def tearDown(self):
        self._image_patch.stop()
        telegram_bot.bot = self._real_bot
        super().tearDown()

    def _send(self, msg, gemini):
        with patch.object(telegram_bot, "_build_gemini_client", return_value=gemini):
            telegram_bot.route_via_conversation(msg, msg.text)

    def _count(self, table):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()

    def test_strength_end_to_end(self):
        payload = envelope(C.INTENT_LOG_STRENGTH, {
            "date_ref": {"kind": "today", "value": None}, "fact_status": "completed",
            "entries": [{"exercise_name": "жим", "weight_kg": 80, "sets": 4, "reps": 8}]})
        self._send(message(1, "жим 80 4х8"), single_response(payload))
        self.assertEqual(self._count("workout_exercises"), 1)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertIn("силов", self.bot.sent[0].text.lower())

    def test_outage_reports_and_writes_nothing(self):
        client = make_client([HttpResponse(503, None), HttpResponse(500, None)])
        self._send(message(2, "магний 400"), client)
        self.assertEqual(self._count("supplements_log"), 0)
        self.assertIn("недоступен", self.bot.sent[0].text)

    def test_duplicate_message_single_write(self):
        payload = envelope(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": "09:00",
            "items": [{"name": "креатин", "dose_text": "5г", "taken": True}]})
        msg = message(3, "5г креатина")
        self._send(msg, single_response(payload))
        first_text = self.bot.sent[-1].text
        self._send(msg, single_response(payload))  # same message id
        self.assertEqual(self._count("supplements_log"), 1)
        self.assertEqual(self.bot.sent[-1].text, first_text)
        row = store.get_action(store.reserve(store.ActionContext(
            source="telegram", chat_id="1", message_id="3", input_text="5г креатина"
        )).action_id)
        self.assertEqual(row["reply_delivery_status"], "delivered")

    def test_live_duplicate_is_not_replayed_as_terminal(self):
        msg = message(30, "ещё обрабатывается")
        store.reserve(store.ActionContext(
            source="telegram", chat_id="1", user_id="42", message_id="30",
            input_text=msg.text,
        ))

        class NoGemini:
            def generate(self, *args, **kwargs):
                raise AssertionError("live duplicate must not call Gemini")

        self._send(msg, NoGemini())
        self.assertIn("обрабатывается", self.bot.sent[-1].text)
        conn = sqlite3.connect(self.db)
        try:
            self.assertIsNone(conn.execute(
                "SELECT response_text FROM conversation_actions WHERE message_id='30'"
            ).fetchone()[0])
        finally:
            conn.close()

    def test_router_propagates_action_fence_into_tool_context(self):
        payload = envelope(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": "09:00",
            "items": [{"name": "creatine", "dose_text": "5 g", "taken": True}],
        })
        captured = {}
        real_execute = conversation_router.tools.execute

        def capture(tool_name, args, exec_ctx):
            captured["token"] = exec_ctx.processing_token
            captured["fence"] = exec_ctx.processing_fence
            return real_execute(tool_name, args, exec_ctx)

        with patch.object(conversation_router.tools, "execute", side_effect=capture):
            self._send(message(31, "creatine 5 g taken"), single_response(payload))
        self.assertTrue(captured["token"])
        self.assertEqual(captured["fence"], 1)

    def test_stale_worker_cannot_overwrite_reclaimed_action_with_failure(self):
        ctx = store.ActionContext(
            source="telegram", chat_id="1", message_id="32", input_text="same",
        )
        start = dt.datetime(2026, 7, 13, 8, 0, tzinfo=dt.timezone.utc)
        old = store.reserve(ctx, now=start, lease=dt.timedelta(seconds=1))
        current = store.reserve(ctx, now=start + dt.timedelta(seconds=2))
        with self.assertRaises(store.ActionLeaseLost):
            store.mark_tool_failed(
                old.action_id, tool_name="log_supplement",
                processing_token=old.processing_token,
                processing_fence=old.processing_fence,
            )
        self.assertEqual(store.get_action(current.action_id)["status"], C.ACTION_RECEIVED)

    def test_succeeded_audit_action_cannot_be_deleted_or_rewritten(self):
        payload = envelope(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": "09:00",
            "items": [{"name": "creatine", "dose_text": "5 g", "taken": True}],
        })
        self._send(message(33, "creatine 5 g taken"), single_response(payload))
        conn = store.connect()
        try:
            action_id = conn.execute(
                "SELECT action_id FROM conversation_actions WHERE message_id='33'"
            ).fetchone()[0]
            with self.assertRaisesRegex(sqlite3.IntegrityError, "delete blocked"):
                conn.execute("DELETE FROM conversation_actions WHERE action_id=?", (action_id,))
            conn.rollback()
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                conn.execute(
                    "UPDATE conversation_actions SET result_json='{}' WHERE action_id=?",
                    (action_id,),
                )
            conn.rollback()
            conn.execute(
                "UPDATE conversation_actions SET reply_delivery_status='delivered' "
                "WHERE action_id=?", (action_id,),
            )
            conn.commit()
        finally:
            conn.close()

    def test_integrity_monitor_alerts_once_for_same_corruption(self):
        payload = envelope(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": "09:00",
            "items": [{"name": "creatine", "dose_text": "5 g", "taken": True}],
        })
        self._send(message(34, "creatine 5 g taken"), single_response(payload))
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE supplements_log SET dosage='tampered'")
        conn.commit()
        conn.close()
        telegram_bot._last_integrity_alert_signature = None
        telegram_bot._last_integrity_alert_at = None
        before = len(self.bot.sent)
        now = dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.timezone.utc)
        self.assertFalse(telegram_bot.run_data_integrity_monitor(now=now))
        self.assertFalse(telegram_bot.run_data_integrity_monitor(now=now))
        self.assertEqual(len(self.bot.sent), before + 1)
        self.assertIn("целостности", self.bot.sent[-1].text)

    def test_photo_cardio_uses_audited_atomic_path(self):
        conn = store.connect()
        telegram_bot.phase2_store.migrate(conn)
        telegram_bot.morning_context.ensure_table(conn)
        conn.close()
        msg = types.SimpleNamespace(
            chat=types.SimpleNamespace(id="1"),
            from_user=types.SimpleNamespace(id="42"), message_id="35",
            photo=[types.SimpleNamespace(file_id="photo-35")],
        )
        outcome = telegram_bot.persist_photo_cardio(msg, {
            "type": "Run", "duration": 30, "distance": 5,
            "avg_hr": 145, "calories": 300,
            "confidence": .95,
            "zone_0": "00:30", "zone_1": "05:00", "zone_2": "10:00",
            "zone_3": "10:00", "zone_4": "04:00", "zone_5": "00:30",
        }, "2026-07-13", telegram_bot.get_kiev_time())
        self.assertTrue(outcome.write_committed)
        self.assertEqual(self._count("cardio_exercises"), 1)
        self.assertTrue(__import__("data_integrity").audit_database(self.db)["ok"])

    def test_production_strength_fallback_is_atomic_replayable_and_provenanced(self):
        text = (
            "\u0436\u0438\u043c \u043d\u0430 \u043f\u043b\u0435\u0447\u0438 \u0432 \u0442\u0440\u0435\u043d\u0430\u0436\u0451\u0440\u0435: 100\u00d710, 100\u00d77\n"
            "\u0436\u0438\u043c \u043d\u0430\u0434 \u0433\u043e\u043b\u043e\u0432\u043e\u0439 \u043d\u0430 \u0442\u0440\u0438\u0446\u0435\u043f\u0441: 52\u00d79, 52\u00d77\n"
            "\u043c\u043e\u043b\u043e\u0442\u043a\u0438 \u0441\u0438\u0434\u044f: 17.5\u00d78, 17.5\u00d76\n"
            "\u043c\u0430\u0445\u0438 \u0441 \u0433\u0430\u043d\u0442\u0435\u043b\u044f\u043c\u0438 \u0441\u0442\u043e\u044f: 12.5\u00d715, 12.5\u00d713\n"
            "\u0442\u0440\u0438\u0446\u0435\u043f\u0441 \u0432 \u0442\u0440\u0435\u043d\u0430\u0436\u0451\u0440\u0435: 43\u00d710, 43\u00d78\n"
            "\u0431\u0438\u0446\u0435\u043f\u0441 \u0432 \u0421\u043a\u043e\u0442\u0442\u0435: 41\u00d710, 41\u00d76"
        )
        msg = message(80, text)
        class Down:
            def generate(self, *a, **k): raise GeminiUnavailable("down")
        self._send(msg, Down())
        self.assertEqual(self._count("workout_exercises"), 12)
        self._send(msg, Down())
        self.assertEqual(self._count("workout_exercises"), 12)
        conn = sqlite3.connect(self.db)
        try:
            action = conn.execute("SELECT action_id FROM conversation_actions WHERE message_id='80'").fetchone()[0]
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM action_domain_links WHERE action_id=?", (action,)).fetchone()[0], 12)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM domain_events WHERE action_id=?", (action,)).fetchone()[0], 12)
            actual = conn.execute(
                "SELECT exercise_name, weight, reps FROM workout_exercises "
                "ORDER BY id"
            ).fetchall()
            self.assertEqual(actual, [
                ("жим на плечи в тренажёре", 100.0, 10),
                ("жим на плечи в тренажёре", 100.0, 7),
                ("жим над головой на трицепс", 52.0, 9),
                ("жим над головой на трицепс", 52.0, 7),
                ("молотки сидя", 17.5, 8),
                ("молотки сидя", 17.5, 6),
                ("махи с гантелями стоя", 12.5, 15),
                ("махи с гантелями стоя", 12.5, 13),
                ("трицепс в тренажёре", 43.0, 10),
                ("трицепс в тренажёре", 43.0, 8),
                ("бицепс в Скотте", 41.0, 10),
                ("бицепс в Скотте", 41.0, 6),
            ])
        finally: conn.close()
        conn = sqlite3.connect(self.db)
        try:
            snapshot = canonical_read_model.range_snapshot(
                conn, dt.date.today(), dt.date.today()
            )
            names = {
                row["exercise_name"]
                for row in snapshot["days"][0]["activities"]["manual_strength"]
            }
            self.assertEqual(len(names), 6)
        finally:
            conn.close()
        report = __import__("data_integrity").audit_database(self.db)
        self.assertTrue(report["ok"], report["issues"])

    def test_whoop_walking_contract_to_audited_cardio_and_duplicate(self):
        parsed = {"type": "walking", "duration": 28 + 43/60, "avg_hr": 136.0,
                  "calories": 198.0, "strain": 7.5, "steps": 1787,
                  "max_hr": None, "confidence": .95}
        msg = types.SimpleNamespace(chat=types.SimpleNamespace(id="1"), from_user=types.SimpleNamespace(id="42"),
                                    message_id="81", photo=[types.SimpleNamespace(file_id="whoop")])
        first = telegram_bot.persist_photo_cardio(msg, parsed, "2026-07-16", telegram_bot.get_kiev_time())
        second = telegram_bot.persist_photo_cardio(msg, parsed, "2026-07-16", telegram_bot.get_kiev_time())
        self.assertTrue(first.write_committed)
        self.assertEqual(self._count("cardio_exercises"), 1)
        self.assertEqual(second.kind, "duplicate")
        conn = sqlite3.connect(self.db)
        try:
            action = conn.execute(
                "SELECT action_id FROM conversation_actions WHERE message_id='81'"
            ).fetchone()[0]
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM action_domain_links WHERE action_id=?",
                (action,),
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM domain_events WHERE action_id=?",
                (action,),
            ).fetchone()[0], 1)
            snapshot = canonical_read_model.range_snapshot(
                conn, "2026-07-16", "2026-07-16"
            )
            rows = snapshot["days"][0]["activities"]["manual_cardio"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["strain"], 7.5)
            self.assertEqual(rows[0]["steps"], 1787)
            self.assertEqual(rows[0]["avg_hr"], 136)
            self.assertEqual(rows[0]["calories"], 198)
        finally:
            conn.close()
        report = __import__("data_integrity").audit_database(self.db)
        self.assertTrue(report["ok"], report["issues"])

    def test_photo_handler_reserves_before_provider_uses_message_date_and_replays(self):
        stamp = int(dt.datetime(2026, 7, 15, 21, tzinfo=dt.timezone.utc).timestamp())
        msg = types.SimpleNamespace(
            chat=types.SimpleNamespace(id="1", type="private"),
            from_user=types.SimpleNamespace(id="42"), message_id="82", date=stamp,
            photo=[types.SimpleNamespace(file_id="whoop-82")],
        )
        self.bot.get_file = lambda _file_id: types.SimpleNamespace(file_path="safe")
        self.bot.download_file = lambda _path: b"\xff\xd8\xff"
        extracted = ({"type": "walking", "duration": 28 + 43/60,
                      "avg_hr": 136.0, "calories": 198.0,
                      "strain": 7.5, "steps": 1787, "max_hr": None,
                      "confidence": .95},
                     {"model": "fallback", "attempt_count": 2, "latency_ms": 9,
                      "response_sha256": "a" * 64})
        with patch.object(telegram_bot, "parse_photo_cardio_contract",
                          return_value=extracted) as parse, \
             patch.object(telegram_bot, "is_authorized_message", return_value=True):
            telegram_bot.handle_photo(msg)
            telegram_bot.handle_photo(msg)
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(self._count("cardio_exercises"), 1)
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT date FROM cardio_exercises"
            ).fetchone()
            action = conn.execute(
                "SELECT router_model, attempt_count, latency_ms, reply_delivery_status "
                "FROM conversation_actions WHERE message_id='82'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "2026-07-16")
        self.assertEqual(action, ("fallback", 2, 9, "delivered"))
        conn = sqlite3.connect(self.db)
        try:
            action_id = conn.execute(
                "SELECT action_id FROM conversation_actions WHERE message_id='82'"
            ).fetchone()[0]
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM action_domain_links WHERE action_id=?",
                (action_id,),
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM domain_events WHERE action_id=?",
                (action_id,),
            ).fetchone()[0], 1)
            snapshot = canonical_read_model.range_snapshot(
                conn, "2026-07-16", "2026-07-16"
            )
            cardio = snapshot["days"][0]["activities"]["manual_cardio"][0]
            self.assertEqual(
                (cardio["strain"], cardio["avg_hr"], cardio["calories"], cardio["steps"]),
                (7.5, 136, 198.0, 1787),
            )
        finally:
            conn.close()

    def test_photo_send_failure_retries_without_provider_or_tool_replay(self):
        stamp = int(dt.datetime(2026, 7, 15, 21, tzinfo=dt.timezone.utc).timestamp())
        msg = types.SimpleNamespace(
            chat=types.SimpleNamespace(id="1", type="private"),
            from_user=types.SimpleNamespace(id="42"), message_id="83", date=stamp,
            photo=[types.SimpleNamespace(file_id="whoop-83")],
        )
        self.bot.get_file = lambda _file_id: types.SimpleNamespace(file_path="safe")
        self.bot.download_file = lambda _path: b"\xff\xd8\xff"
        extracted = ({"type": "walking", "duration": 28 + 43/60,
                      "avg_hr": 136.0, "calories": 198.0,
                      "strain": 7.5, "steps": 1787, "max_hr": None,
                      "confidence": .95},
                     {"model": "fallback", "attempt_count": 2, "latency_ms": 9,
                      "response_sha256": "c" * 64})
        real_send = self.bot.send_message
        calls = {"count": 0}

        def fail_once(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("injected send failure")
            return real_send(*args, **kwargs)

        self.bot.send_message = fail_once
        with patch.object(telegram_bot, "parse_photo_cardio_contract",
                          return_value=extracted) as parse, \
             patch.object(telegram_bot, "is_authorized_message", return_value=True):
            telegram_bot.handle_photo(msg)
        self.assertEqual(self._count("cardio_exercises"), 1)
        self.assertEqual(parse.call_count, 1)
        with patch.object(telegram_bot, "TG_CHAT", "1"):
            self.assertEqual(telegram_bot.retry_pending_conversation_responses(), 1)
        self.assertEqual(self._count("cardio_exercises"), 1)
        self.assertEqual(parse.call_count, 1)
        conn = sqlite3.connect(self.db)
        try:
            action = conn.execute(
                "SELECT reply_delivery_status FROM conversation_actions "
                "WHERE source='telegram-photo' AND message_id='83'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(action[0], "delivered")
        self.assertTrue(__import__("data_integrity").audit_database(self.db)["ok"])

    def test_exact_strength_tool_failure_rolls_back_entire_batch(self):
        text = "жим: 100x10, 100x7\nтяга: 80x8, 80x6"
        msg = message(88, text)
        class Down:
            def generate(self, *a, **k):
                raise GeminiUnavailable("down")
        real_link = telegram_bot.workouts_db.link_action_domain
        calls = {"count": 0}
        def fail_second(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise sqlite3.IntegrityError("injected")
            return real_link(*args, **kwargs)
        with patch.object(telegram_bot.workouts_db, "link_action_domain",
                          side_effect=fail_second):
            self._send(msg, Down())
        self.assertEqual(self._count("workout_exercises"), 0)
        self.assertEqual(self._count("action_domain_links"), 0)
        self.assertEqual(self._count("domain_events"), 0)

    def test_exact_cardio_provenance_failure_rolls_back_row(self):
        msg = types.SimpleNamespace(
            chat=types.SimpleNamespace(id="1"),
            from_user=types.SimpleNamespace(id="42"), message_id="89",
            photo=[types.SimpleNamespace(file_id="whoop-89")],
        )
        with patch.object(
            telegram_bot.workouts_db, "link_action_domain",
            side_effect=sqlite3.IntegrityError("injected"),
        ):
            outcome = telegram_bot.persist_photo_cardio(
                msg, {"type": "walking", "duration": 28 + 43/60,
                      "avg_hr": 136, "calories": 198, "strain": 7.5,
                      "steps": 1787, "max_hr": None, "confidence": .95},
                "2026-07-16", telegram_bot.get_kiev_time(),
            )
        self.assertEqual(outcome.kind, "tool_failed")
        self.assertEqual(self._count("cardio_exercises"), 0)
        self.assertEqual(self._count("action_domain_links"), 0)
        self.assertEqual(self._count("domain_events"), 0)

    def test_out_of_range_optional_cardio_values_do_not_block_valid_core(self):
        parsed = telegram_bot.cardio_extraction.validate({
            "activity_type": "walking", "duration_seconds": 600,
            "strain": 99, "avg_hr_bpm": 120, "max_hr_bpm": 999,
            "calories_kcal": None, "distance_km": 5000, "steps": 999999,
            "activity_confidence": .9, "duration_confidence": .9,
            "effort_confidence": .9,
        })
        msg = types.SimpleNamespace(
            chat=types.SimpleNamespace(id="1"),
            from_user=types.SimpleNamespace(id="42"), message_id="94",
            photo=[types.SimpleNamespace(file_id="whoop-94")],
        )
        outcome = telegram_bot.persist_photo_cardio(
            msg, parsed, "2026-07-16", telegram_bot.get_kiev_time()
        )
        self.assertTrue(outcome.write_committed)
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT duration, avg_hr, strain, max_hr, distance, steps "
                "FROM cardio_exercises"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (10.0, 120, None, None, None, None))

    def test_subthreshold_photo_confidence_never_writes(self):
        msg = types.SimpleNamespace(
            chat=types.SimpleNamespace(id="1"),
            from_user=types.SimpleNamespace(id="42"), message_id="95",
            photo=[types.SimpleNamespace(file_id="whoop-95")],
        )
        outcome = telegram_bot.persist_photo_cardio(
            msg, {"type": "walking", "duration": 10, "avg_hr": 120,
                  "calories": None, "strain": 4.0, "steps": 1000,
                  "max_hr": None, "confidence": .89},
            "2026-07-16", telegram_bot.get_kiev_time(),
        )
        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(self._count("cardio_exercises"), 0)
        self.assertEqual(self._count("action_domain_links"), 0)
        self.assertEqual(self._count("domain_events"), 0)

    def test_cardio_integrity_detects_new_metric_tampering(self):
        msg = types.SimpleNamespace(
            chat=types.SimpleNamespace(id="1"),
            from_user=types.SimpleNamespace(id="42"), message_id="90",
            photo=[types.SimpleNamespace(file_id="whoop-90")],
        )
        outcome = telegram_bot.persist_photo_cardio(
            msg, {"type": "walking", "duration": 10, "avg_hr": 120,
                  "calories": None, "strain": 7.5, "steps": 1787,
                  "max_hr": None, "confidence": .95},
            "2026-07-16", telegram_bot.get_kiev_time(),
        )
        self.assertTrue(outcome.write_committed)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE cardio_exercises SET strain=8.5")
        conn.commit()
        conn.close()
        report = __import__("data_integrity").audit_database(self.db)
        self.assertFalse(report["ok"])
        self.assertIn(
            "row_hash_mismatch", {issue["code"] for issue in report["issues"]}
        )

    def test_strength_incomplete_reply_merges_preserved_entries_once(self):
        first = message(84, "жим: 100x10\nтяга:")
        class Down:
            def generate(self, *a, **k):
                raise GeminiUnavailable("down")
        self._send(first, Down())
        pending = store.active_pending("telegram", "1", "42")
        question_id = self.bot.sent[-1].message_id
        self.assertIsNotNone(pending)
        self._send(
            message(85, "тяга: 80x8", reply_to_message_id=question_id),
            Down(),
        )
        self.assertEqual(self._count("workout_exercises"), 2)
        self.assertIsNone(store.active_pending("telegram", "1", "42"))
        self._send(
            message(85, "тяга: 80x8", reply_to_message_id=question_id),
            Down(),
        )
        self.assertEqual(self._count("workout_exercises"), 2)

    def test_photo_duration_clarification_preserves_values_and_writes_once(self):
        stamp = int(dt.datetime(2026, 7, 16, 8, tzinfo=dt.timezone.utc).timestamp())
        photo = types.SimpleNamespace(
            chat=types.SimpleNamespace(id="1", type="private"),
            from_user=types.SimpleNamespace(id="42"), message_id="86", date=stamp,
            photo=[types.SimpleNamespace(file_id="whoop-86")],
        )
        self.bot.get_file = lambda _file_id: types.SimpleNamespace(file_path="safe")
        self.bot.download_file = lambda _path: b"\xff\xd8\xff"
        err = telegram_bot.cardio_extraction.CardioExtractionError(
            "duration",
            {"activity_type": "walking", "duration_seconds": None,
             "strain": 7.5, "avg_hr_bpm": 136, "max_hr_bpm": None,
             "calories_kcal": 198, "distance_km": None, "steps": 1787,
             "activity_confidence": .9, "duration_confidence": .1,
             "effort_confidence": .9},
        )
        err.model = "fallback"
        err.attempt_count = 2
        err.latency_ms = 9
        err.response_sha256 = "b" * 64
        with patch.object(telegram_bot, "parse_photo_cardio_contract",
                          side_effect=err), \
             patch.object(telegram_bot, "is_authorized_message", return_value=True):
            telegram_bot.handle_photo(photo)
        question_id = self.bot.sent[-1].message_id
        pending = store.active_pending("telegram-photo", "1", "42")
        self.assertIsNotNone(pending)
        class NoGemini:
            def generate(self, *a, **k):
                raise AssertionError("clarification must be deterministic")
        self._send(
            message(87, "28:43", reply_to_message_id=question_id),
            NoGemini(),
        )
        self._send(
            message(87, "28:43", reply_to_message_id=question_id),
            NoGemini(),
        )
        self.assertEqual(self._count("cardio_exercises"), 1)
        conn = sqlite3.connect(self.db)
        try:
            row = conn.execute(
                "SELECT duration, strain, avg_hr, calories, steps "
                "FROM cardio_exercises"
            ).fetchone()
            action = conn.execute(
                "SELECT action_id FROM conversation_actions WHERE message_id='87'"
            ).fetchone()[0]
            self.assertAlmostEqual(row[0], 28 + 43/60, places=3)
            self.assertEqual(row[1:], (7.5, 136, 198.0, 1787))
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM action_domain_links WHERE action_id=?",
                (action,),
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM domain_events WHERE action_id=?",
                (action,),
            ).fetchone()[0], 1)
        finally:
            conn.close()
        self.assertTrue(__import__("data_integrity").audit_database(self.db)["ok"])

    def test_photo_clarification_pending_and_write_roll_back_together(self):
        class NoGemini:
            def generate(self, *args, **kwargs):
                raise AssertionError("clarification must stay deterministic")

        photo_ctx = store.ActionContext(
            source="telegram-photo", chat_id="1", user_id="42",
            message_id="96", input_text="photo:whoop-96",
        )
        photo_res = store.reserve(photo_ctx)
        store.mark_clarification(
            photo_res.action_id, C.INTENT_LOG_CARDIO, .95,
            processing_token=photo_res.processing_token,
            processing_fence=photo_res.processing_fence,
        )
        pending_id = store.create_pending(
            photo_res.action_id, photo_ctx, C.INTENT_LOG_CARDIO,
            {"activity_type": "walking", "duration_seconds": None,
             "strain": 7.5, "avg_hr_bpm": 136, "max_hr_bpm": None,
             "calories_kcal": 198, "distance_km": None, "steps": 1787,
             "activity_confidence": .95, "duration_confidence": .1,
             "effort_confidence": .95, "_target_date": "2026-07-16"},
            ["duration"],
        )
        store.set_clarification_message_id(pending_id, 1960)
        with patch.object(
            telegram_bot.phase2_store, "finalize_pending_resolution",
            return_value=False,
        ):
            self._send(message(
                97, "28:43", reply_to_message_id=1960
            ), NoGemini())
        self.assertEqual(self._count("cardio_exercises"), 0)
        self.assertEqual(self._count("action_domain_links"), 0)
        self.assertEqual(self._count("domain_events"), 0)
        pending = store.active_pending("telegram-photo", "1", "42")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["status"], C.PENDING_OPEN)

    def test_reply_selects_photo_pending_when_text_pending_also_exists(self):
        text_ctx = store.ActionContext(
            source="telegram", chat_id="1", user_id="42",
            message_id="91", input_text="partial text",
        )
        text_res = store.reserve(text_ctx)
        store.mark_clarification(
            text_res.action_id, C.INTENT_LOG_STRENGTH, 1.0,
            processing_token=text_res.processing_token,
            processing_fence=text_res.processing_fence,
        )
        text_pending = store.create_pending(
            text_res.action_id, text_ctx, C.INTENT_LOG_STRENGTH,
            {"date_ref": {"kind": "today", "value": None},
             "fact_status": "completed", "entries": []},
            ["entries"],
        )
        store.set_clarification_message_id(text_pending, 1901)

        photo_ctx = store.ActionContext(
            source="telegram-photo", chat_id="1", user_id="42",
            message_id="92", input_text="photo:whoop-92",
        )
        photo_res = store.reserve(photo_ctx)
        store.mark_clarification(
            photo_res.action_id, C.INTENT_LOG_CARDIO, 1.0,
            processing_token=photo_res.processing_token,
            processing_fence=photo_res.processing_fence,
        )
        photo_pending = store.create_pending(
            photo_res.action_id, photo_ctx, C.INTENT_LOG_CARDIO,
            {"activity_type": "walking", "duration_seconds": None,
             "strain": 7.5, "avg_hr_bpm": 136, "max_hr_bpm": None,
             "calories_kcal": 198, "distance_km": None, "steps": 1787,
             "activity_confidence": .9, "duration_confidence": .1,
             "effort_confidence": .9, "_target_date": "2026-07-16"},
            ["duration"],
        )
        store.set_clarification_message_id(photo_pending, 1902)

        class NoGemini:
            def generate(self, *a, **k):
                raise AssertionError("pending reply must not call provider")
        self._send(message(93, "28:43", reply_to_message_id=1902), NoGemini())
        self.assertEqual(self._count("cardio_exercises"), 1)
        self.assertIsNotNone(store.active_pending("telegram", "1", "42"))
        self.assertIsNone(store.active_pending("telegram-photo", "1", "42"))

    def test_malformed_photo_extract_clarifies_and_writes_nothing(self):
        msg = types.SimpleNamespace(
            chat=types.SimpleNamespace(id="1", type="private"),
            from_user=types.SimpleNamespace(id="42"), message_id="83",
            date=int(dt.datetime.now(dt.timezone.utc).timestamp()),
            photo=[types.SimpleNamespace(file_id="whoop-83")],
        )
        self.bot.get_file = lambda _file_id: types.SimpleNamespace(file_path="safe")
        self.bot.download_file = lambda _path: b"\xff\xd8\xff"
        error = telegram_bot.cardio_extraction.CardioExtractionError("duration")
        with patch.object(telegram_bot, "parse_photo_cardio_contract",
                          side_effect=error), \
             patch.object(telegram_bot, "is_authorized_message", return_value=True):
            telegram_bot.handle_photo(msg)
        self.assertEqual(self._count("cardio_exercises"), 0)
        self.assertIn("длитель", self.bot.sent[-1].text.casefold())

    def test_duplicate_outage_replays_same_safe_response(self):
        msg = message(4, "непонятное сообщение")
        self._send(msg, make_client([HttpResponse(503, None), HttpResponse(500, None)]))
        first_text = self.bot.sent[-1].text

        class NoGemini:
            def generate(self, *args, **kwargs):
                raise AssertionError("duplicate must not call Gemini")

        self._send(msg, NoGemini())
        self.assertEqual(self.bot.sent[-1].text, first_text)

    def test_failed_clarification_send_leaves_no_orphan_pending(self):
        class FailingBot(FakeBot):
            def send_message(self, *args, **kwargs):
                raise RuntimeError("telegram unavailable")

        self.bot = FailingBot()
        telegram_bot.bot = self.bot
        amb = envelope(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": None,
            "items": [{"name": "магний", "dose_text": "400мг", "taken": None}]})
        self._send(message(12, "магний 400"), single_response(amb))
        self.assertIsNone(store.active_pending("telegram", "1", "42"))
        conn = sqlite3.connect(self.db)
        try:
            actions = conn.execute(
                "SELECT reply_delivery_status FROM conversation_actions WHERE message_id='12'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(actions[0], "failed")

    def test_clarification_then_reply_resolves_and_writes(self):
        # 1) ambiguous supplement -> clarification question
        amb = envelope(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": None,
            "items": [{"name": "магний", "dose_text": "400мг", "taken": None}]})
        self._send(message(10, "магний 400"), single_response(amb))
        question_id = self.bot.sent[-1].message_id
        active = store.active_pending("telegram", "1", "42")
        self.assertIsNotNone(active)
        self.assertEqual(str(active["clarification_question_message_id"]), str(question_id))

        # 2) user replies to that exact question with an explicit fact
        fact = envelope(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": None,
            "items": [{"name": "магний", "dose_text": "400мг", "taken": True}]})
        self._send(message(11, "да, принял", reply_to_message_id=question_id),
                   single_response(fact))
        self.assertEqual(self._count("supplements_log"), 1)
        self.assertIsNone(store.active_pending("telegram", "1", "42"))

    def test_missing_fact_status_clarifies_then_yes_reply_writes_once(self):
        payload = envelope(C.INTENT_LOG_STRENGTH, {
            "date_ref": {"kind": "today", "value": None},
            "entries": [{"exercise_name": "жим", "weight_kg": 80, "sets": 4, "reps": 8}]})
        self._send(message(20, "жим 80 4х8"), single_response(payload))
        self.assertEqual(self._count("workout_exercises"), 0)
        question_id = self.bot.sent[-1].message_id
        self.assertIn("выполнил", self.bot.sent[-1].text)

        class _RaisingGemini:
            def generate(self, *a, **k):
                raise AssertionError("resolving a fact-status reply must not call Gemini")

        self._send(message(21, "да, выполнил", reply_to_message_id=question_id),
                   _RaisingGemini())
        self.assertEqual(self._count("workout_exercises"), 1)
        self.assertIsNone(store.active_pending("telegram", "1", "42"))

    def test_flag_parsing(self):
        with patch.dict(os.environ, {"CONVERSATIONAL_ROUTER_ENABLED": "true"}):
            self.assertTrue(telegram_bot.router_enabled())
        with patch.dict(os.environ, {"CONVERSATIONAL_ROUTER_ENABLED": "false"}):
            self.assertFalse(telegram_bot.router_enabled())
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(telegram_bot.router_enabled())
            self.assertFalse(telegram_bot.legacy_destructive_text_enabled())

    def test_router_off_refuses_untracked_legacy_write(self):
        msg = message(40, "legacy workout")
        with patch.dict(os.environ, {"CONVERSATIONAL_ROUTER_ENABLED": "false"}), \
                patch.object(telegram_bot, "parse_with_gemini",
                             side_effect=AssertionError("legacy parser must stay unreachable")):
            telegram_bot.handle_free_text(msg)
        self.assertEqual(self._count("workout_exercises"), 0)
        self.assertIn("данные не изменены", self.bot.sent[-1].text)


if __name__ == "__main__":
    unittest.main()
