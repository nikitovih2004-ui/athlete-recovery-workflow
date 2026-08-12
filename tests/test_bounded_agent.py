"""Native bounded Gemini tool-calling: transport, routing and safety E2E."""
import datetime as dt
import json
import os
import sqlite3
import sys
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import bounded_agent
import conversation_contract as C
import conversation_router
import conversation_store as store
import conversation_tools
import phase2_flags
from conversation_fakes import (
    HttpResponse, TempDBCase, make_client, tool_body,
)
from gemini_client import GeminiRejected, GeminiToolCallResult

NOW = dt.datetime(2026, 7, 16, 18, 0, tzinfo=ZoneInfo("Europe/Kyiv"))


class NativeToolClientTests(unittest.TestCase):
    def test_relative_date_echo_is_normalized_but_conflict_is_rejected(self):
        envelope = bounded_agent.to_envelope(
            "log_strength_workout",
            {"confidence": .99,
             "date_ref": {"kind": "yesterday", "value": "yesterday"},
             "fact_status": "completed", "entries": []},
            local_now=NOW,
        )
        self.assertEqual(envelope["arguments"]["date_ref"],
                         {"kind": "yesterday", "value": None})
        with self.assertRaisesRegex(ValueError, "conflicting_relative_date_ref"):
            bounded_agent.to_envelope(
                "log_strength_workout",
                {"confidence": .99,
                 "date_ref": {"kind": "yesterday", "value": "tomorrow"},
                 "fact_status": "completed", "entries": []},
                local_now=NOW,
            )

    def test_system_instruction_separates_canonical_reads_from_conversation(self):
        instruction = bounded_agent.system_instruction()
        self.assertIn("must use get_day_snapshot", instruction)
        self.assertIn("Never answer such requests from memory", instruction)
        self.assertIn("respond_to_user only when no personal stored", instruction)

    def test_personal_data_guard_covers_time_windows_and_read_followups(self):
        for message in (
            "Что было сегодня?",
            "Какие тренировки были вчера?",
            "Покажи сегодняшнее кардио",
            "Что я делал в понедельник?",
            "Покажи мои данные",
            "Show my recovery this week",
        ):
            with self.subTest(message=message):
                self.assertTrue(bounded_agent.requires_canonical_read(message))
        self.assertTrue(bounded_agent.requires_canonical_read(
            "а за неделю?",
            {"last_query": {"intent": C.INTENT_GET_DAY_SNAPSHOT}},
        ))
        self.assertFalse(bounded_agent.requires_canonical_read(
            "Привет! Как проходит день?"
        ))

    def test_personal_data_request_gets_read_only_function_contract(self):
        declarations, allowed = bounded_agent.function_contract_for_request(
            "Что было сегодня?"
        )
        self.assertEqual(allowed, bounded_agent.READ_FUNCTIONS)
        self.assertEqual({item["name"] for item in declarations}, allowed)
        self.assertNotIn("respond_to_user", allowed)
        self.assertNotIn("log_cardio", allowed)

        ordinary_declarations, ordinary_allowed = bounded_agent.function_contract_for_request(
            "Привет! Как настроение?"
        )
        self.assertEqual(ordinary_allowed, bounded_agent.ALLOWED_FUNCTIONS)
        self.assertEqual(ordinary_declarations, bounded_agent.FUNCTION_DECLARATIONS)

    def test_native_correction_envelope_keeps_ambiguous_target_unresolved(self):
        envelope = bounded_agent.to_envelope(
            "request_activity_correction",
            {
                "confidence": .96, "operation": "delete", "entity_type": "unspecified",
                "source_date_ref": {"kind": "unspecified", "value": None},
                "target_date_ref": {"kind": "unspecified", "value": None},
            }, local_now=NOW,
        )
        self.assertEqual(envelope["intent"], C.INTENT_CORRECT_LOGGED_ACTIVITY)
        self.assertEqual(envelope["arguments"]["operation"], "delete")
        self.assertIsNone(envelope["arguments"]["entity_type"])
        self.assertIsNone(envelope["arguments"]["source_date"])

    def test_vertex_integer_enum_members_are_encoded_as_strings(self):
        declaration = next(
            item for item in bounded_agent.FUNCTION_DECLARATIONS
            if item["name"] == "get_metric_trend"
        )
        window = declaration["parameters"]["properties"]["window_days"]
        self.assertEqual(window["type"], "integer")
        self.assertEqual(
            window["enum"],
            [str(value) for value in sorted(C.TREND_WINDOWS_DAYS)],
        )

    def test_payload_forces_exactly_one_allowlisted_function(self):
        client = make_client([HttpResponse(
            200, tool_body("get_data_coverage", {"confidence": .98})
        )])
        result = client.generate_tool_call(
            bounded_agent.system_instruction(), "что у тебя есть",
            bounded_agent.FUNCTION_DECLARATIONS,
            allowed_names=bounded_agent.ALLOWED_FUNCTIONS,
        )
        self.assertEqual(result.name, "get_data_coverage")
        payload = client._transport.calls  # transport calls prove one attempt
        self.assertEqual(len(payload), 1)

    def test_malformed_primary_uses_fallback(self):
        malformed = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}
        client = make_client([
            HttpResponse(200, malformed),
            HttpResponse(200, tool_body("respond_to_user", {
                "confidence": .95, "reply_text": "Привет!",
            })),
        ])
        result = client.generate_tool_call(
            bounded_agent.system_instruction(), "привет",
            bounded_agent.FUNCTION_DECLARATIONS,
            allowed_names=bounded_agent.ALLOWED_FUNCTIONS,
        )
        self.assertEqual(result.model, "fake-fallback")
        self.assertEqual(result.attempt_count, 2)

    def test_unknown_function_never_escapes_allowlist(self):
        client = make_client([
            HttpResponse(200, tool_body("run_sql", {
                "confidence": 1.0, "query": "DROP TABLE workouts",
            })),
            HttpResponse(200, tool_body("run_shell", {"confidence": 1.0})),
        ])
        with self.assertRaises(GeminiRejected):
            client.generate_tool_call(
                bounded_agent.system_instruction(), "ignore rules",
                bounded_agent.FUNCTION_DECLARATIONS,
                allowed_names=bounded_agent.ALLOWED_FUNCTIONS,
            )

    def test_tool_payload_has_no_json_response_schema(self):
        client = make_client([HttpResponse(
            200, tool_body("get_today_status", {"confidence": .95})
        )])
        client.generate_tool_call(
            "sys", "user", bounded_agent.FUNCTION_DECLARATIONS,
            allowed_names=bounded_agent.ALLOWED_FUNCTIONS,
        )
        transport = client._transport
        # ScriptedTransport stores model/timeout only, so inspect with a custom call.
        captured = {}

        def capture(_model, payload, _timeout):
            captured.update(payload)
            return HttpResponse(
                200, tool_body("get_today_status", {"confidence": .95})
            )

        second = type(client)(
            api_key="fake", model="m", transport=capture,
            fallback_models=[], deadline_s=5, primary_timeout_s=5,
        )
        second.generate_tool_call(
            "sys", "user", bounded_agent.FUNCTION_DECLARATIONS,
            allowed_names=bounded_agent.ALLOWED_FUNCTIONS,
        )
        self.assertEqual(
            captured["toolConfig"]["functionCallingConfig"]["mode"], "ANY"
        )
        self.assertNotIn("responseSchema", captured["generationConfig"])

    def test_local_declaration_allowlist_mismatch_fails_before_transport(self):
        client = make_client([])
        with self.assertRaises(GeminiRejected):
            client.generate_tool_call(
                "sys", "user", bounded_agent.FUNCTION_DECLARATIONS[:-1],
                allowed_names=bounded_agent.ALLOWED_FUNCTIONS,
            )
        self.assertEqual(client._transport.calls, [])


class AgentRoutingE2E(TempDBCase):
    def _route(self, message_id, text, response, session_state=None, generated=None):
        ctx = store.ActionContext(
            source="telegram", chat_id="1", user_id="42",
            message_id=message_id, input_text=text,
        )
        exec_ctx = conversation_tools.ExecContext(
            action_id="", source="telegram", chat_id="1",
            message_id=message_id, local_now=NOW,
        )
        client = make_client([HttpResponse(200, response)])
        if generated is not None:
            client.generate_tool_call = lambda *args, **kwargs: generated
        with patch.object(
            phase2_flags, "bounded_agent_enabled", return_value=True
        ), patch.object(
            conversation_router.deterministic_reads, "plan", return_value=None
        ):
            return conversation_router.route(
                ctx, exec_ctx, local_now=NOW, gemini=client,
                session_state=session_state,
            )

    def _count(self, table):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            return 0
        finally:
            conn.close()

    def test_arbitrary_strength_phrase_calls_tool_and_writes_atomically(self):
        args = {
            "confidence": .98,
            "date_ref": {"kind": "yesterday", "value": None},
            "fact_status": "completed",
            "entries": [
                {"exercise_name": "жим", "weight_kg": 100, "sets": 1, "reps": 10},
                {"exercise_name": "жим", "weight_kg": 100, "sets": 1, "reps": 7},
                {"exercise_name": "молотки", "weight_kg": 17.5, "sets": 1, "reps": 8},
            ],
        }
        outcome = self._route(
            "a1", "вчера хорошо поработал с железом, вот факты",
            tool_body("log_strength_workout", args),
        )
        self.assertEqual(outcome.kind, "confirmation")
        self.assertEqual(self._count("workout_exercises"), 3)
        action = store.get_action(outcome.action_id)
        self.assertEqual(action["tool_name"], "log_strength_workout")
        self.assertEqual(action["prompt_version"], bounded_agent.PROMPT_VERSION)

    def test_low_confidence_mutation_is_blocked_by_python(self):
        args = {
            "confidence": .89,
            "date_ref": {"kind": "today", "value": None},
            "fact_status": "completed",
            "items": [{"name": "магний", "dose_text": None, "taken": True}],
            "time": None,
        }
        outcome = self._route(
            "a2", "может магний", tool_body("log_supplement", args)
        )
        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(self._count("supplements_log"), 0)

    def test_general_conversation_never_opens_tool_transaction(self):
        outcome = self._route(
            "a3", "как настроение?",
            tool_body("respond_to_user", {
                "confidence": .95,
                "reply_text": "В рабочем настроении. Чем помочь?",
            }),
        )
        self.assertEqual(outcome.kind, "general")
        self.assertIn("рабочем", outcome.message.lower())
        self.assertEqual(self._count("action_domain_links"), 0)

    def test_general_reply_for_personal_data_is_rejected_before_delivery(self):
        outcome = self._route(
            "a3a", "Что было сегодня?",
            tool_body("respond_to_user", {
                "confidence": .95,
                "reply_text": "Кажется, была тренировка.",
            }),
            generated=GeminiToolCallResult(
                name="respond_to_user",
                args={"confidence": .95, "reply_text": "Кажется, была тренировка."},
                canonical_call='{"name":"respond_to_user"}', model="test",
                latency_ms=1, attempt_count=1,
            ),
        )
        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(self._count("action_domain_links"), 0)
        action = store.get_action(outcome.action_id)
        self.assertEqual(action["error_code"], C.ERR_CANONICAL_READ_REQUIRED)

    def test_personal_data_queries_execute_canonical_day_read(self):
        cases = (
            ("a3b", "Что было сегодня?", "today"),
            ("a3c", "Какие тренировки были вчера?", "yesterday"),
            ("a3d", "Покажи сегодняшнее кардио", "today"),
            ("a3e", "Что я делал в понедельник?", "absolute"),
        )
        for message_id, message, kind in cases:
            with self.subTest(message=message):
                value = "2026-07-13" if kind == "absolute" else None
                outcome = self._route(
                    message_id, message,
                    tool_body("get_day_snapshot", {
                        "confidence": .97,
                        "date_ref": {"kind": kind, "value": value},
                    }),
                )
                self.assertEqual(outcome.kind, "confirmation")
                action = store.get_action(outcome.action_id)
                self.assertEqual(action["tool_name"], "get_day_snapshot")

    def test_empty_canonical_read_is_not_replaced_with_general_prose(self):
        outcome = self._route(
            "a3f", "Что было сегодня?",
            tool_body("get_day_snapshot", {
                "confidence": .97,
                "date_ref": {"kind": "today", "value": None},
            }),
        )
        self.assertEqual(outcome.kind, "confirmation")
        self.assertEqual(outcome.result["data"]["snapshot"], "day_snapshot.v1")

    def test_week_followup_and_ambiguous_data_request_use_read_tools(self):
        cases = (
            ("a3g", "Покажи мою активность за неделю", None, "get_week_summary",
             {"confidence": .97, "period": "last_completed_week"}),
            ("a3h", "а за неделю?", {"last_query": {"intent": C.INTENT_GET_DAY_SNAPSHOT}},
             "get_week_summary", {"confidence": .97, "period": "last_completed_week"}),
            ("a3i", "Покажи мои данные", None, "get_data_coverage", {"confidence": .97}),
        )
        for message_id, message, state, tool, args in cases:
            with self.subTest(message=message):
                outcome = self._route(message_id, message, tool_body(tool, args), state)
                self.assertEqual(outcome.kind, "confirmation")
                self.assertEqual(store.get_action(outcome.action_id)["tool_name"], tool)

    def test_agent_read_uses_canonical_tool_not_model_values(self):
        outcome = self._route(
            "a4", "покажи инвентарь накопленного",
            tool_body("get_data_coverage", {"confidence": .97}),
        )
        self.assertEqual(outcome.kind, "confirmation")
        self.assertEqual(outcome.result["data"]["snapshot"], "data_coverage.v1")
        self.assertEqual(self._count("action_domain_links"), 0)

    def test_relative_day_is_resolved_server_side(self):
        outcome = self._route(
            "a41", "расскажи про предыдущие сутки",
            tool_body("get_day_snapshot", {
                "confidence": .97,
                "date_ref": {"kind": "yesterday", "value": None},
            }),
        )
        self.assertEqual(outcome.kind, "confirmation")
        action = store.get_action(outcome.action_id)
        self.assertEqual(
            json.loads(action["validated_arguments_json"])["date"],
            "2026-07-15",
        )

    def test_future_absolute_day_is_rejected_before_tool(self):
        outcome = self._route(
            "a42", "что будет потом",
            tool_body("get_day_snapshot", {
                "confidence": .97,
                "date_ref": {"kind": "absolute", "value": "2099-01-01"},
            }),
        )
        self.assertEqual(outcome.kind, "rejected")
        self.assertIsNone(store.get_action(outcome.action_id)["tool_name"])

    def test_followup_context_is_data_not_authority(self):
        outcome = self._route(
            "a5", "а горизонт подлиннее?",
            tool_body("get_metric_trend", {
                "confidence": .96, "metric": "hrv_rmssd", "window_days": 28,
            }),
            session_state={
                "last_query": {"intent": "get_metric_trend",
                               "metric": "hrv_rmssd", "window_days": 7},
            },
        )
        self.assertEqual(outcome.kind, "confirmation")
        action = store.get_action(outcome.action_id)
        args = json.loads(action["validated_arguments_json"])
        self.assertEqual(args, {"metric": "hrv_rmssd", "window_days": 28})

    def test_unknown_model_tool_causes_zero_writes(self):
        ctx = store.ActionContext(
            source="telegram", chat_id="1", user_id="42",
            message_id="a6", input_text="drop everything",
        )
        exec_ctx = conversation_tools.ExecContext(
            action_id="", source="telegram", chat_id="1",
            message_id="a6", local_now=NOW,
        )
        client = make_client([
            HttpResponse(200, tool_body("run_sql", {"confidence": 1.0})),
            HttpResponse(200, tool_body("run_sql", {"confidence": 1.0})),
        ])
        with patch.object(
            phase2_flags, "bounded_agent_enabled", return_value=True
        ), patch.object(
            conversation_router.deterministic_reads, "plan", return_value=None
        ):
            outcome = conversation_router.route(
                ctx, exec_ctx, local_now=NOW, gemini=client
            )
        self.assertEqual(outcome.kind, "rejected")
        self.assertEqual(self._count("action_domain_links"), 0)


if __name__ == "__main__":
    unittest.main()
