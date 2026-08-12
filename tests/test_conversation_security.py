"""Security unit tests: injection, allowlist, strict JSON, no SQL/tool escape."""
import datetime as dt
import os
import sys
import unittest
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import conversation_contract as C
import conversation_validation as V
import conversation_tools as tools
from conversation_fakes import envelope

KYIV = ZoneInfo("Europe/Kyiv")
NOW = dt.datetime(2026, 7, 12, 15, 0, tzinfo=KYIV)


class StrictJsonTests(unittest.TestCase):
    def test_fenced_json_rejected(self):
        with self.assertRaises(V.RouterParseError):
            V.parse_router_json("```json\n{\"a\":1}\n```")

    def test_prose_wrapped_rejected(self):
        with self.assertRaises(V.RouterParseError):
            V.parse_router_json("Here you go: {\"a\":1}")

    def test_non_object_root_rejected(self):
        with self.assertRaises(V.RouterParseError):
            V.parse_router_json("[1,2,3]")

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(V.RouterParseError) as ctx:
            V.parse_router_json('{"intent":"a","intent":"b"}')
        self.assertEqual(ctx.exception.code, C.ERR_DUPLICATE_JSON_KEYS)

    def test_truncated_json_rejected(self):
        with self.assertRaises(V.RouterParseError):
            V.parse_router_json('{"intent": "log_cardio"')

    def test_deeply_nested_is_handled(self):
        payload = "{" + '"a":' * 5000 + "1" + "}" * 1
        # Either rejected as malformed or as non-envelope later; must never crash.
        try:
            V.parse_router_json(payload)
        except V.RouterParseError:
            pass


class InjectionTests(unittest.TestCase):
    def test_injected_instruction_is_just_text(self):
        # A message telling the model to run SQL still must produce a known intent
        # or be rejected — it can never smuggle a tool or SQL through the envelope.
        obj = envelope("DROP TABLE workouts; --", {})
        self.assertEqual(V.validate_envelope(obj).error_code, C.ERR_UNKNOWN_INTENT)

    def test_unsupported_destructive_never_actions(self):
        env = V.validate_envelope(envelope(
            C.INTENT_UNSUPPORTED_REQUEST, {"reason": "destructive_action"}))
        res = V.validate(env, local_now=NOW)
        self.assertTrue(res.ok)
        self.assertIsNone(res.tool)

    def test_clarification_cannot_open_transaction(self):
        env = V.validate_envelope(envelope(C.INTENT_NEEDS_CLARIFICATION, {
            "candidate_intent": "log_supplement", "missing_fields": ["taken"],
            "question": "Принял или планируешь?"}))
        res = V.validate(env, local_now=NOW)
        self.assertTrue(res.ok)
        self.assertIsNone(res.tool)
        self.assertEqual(res.clarification["question"], C.MSG_SUPPLEMENT_STATUS_CLARIFICATION)

    def test_model_authored_causal_clarification_is_not_surfaced(self):
        env = V.validate_envelope(envelope(C.INTENT_NEEDS_CLARIFICATION, {
            "candidate_intent": "log_supplement", "missing_fields": ["taken"],
            "question": "Magnesium causes better recovery, correct?"}))
        res = V.validate(env, local_now=NOW)
        self.assertTrue(res.ok)
        self.assertEqual(res.clarification["question"], C.MSG_SUPPLEMENT_STATUS_CLARIFICATION)

    def test_unallowlisted_generic_clarification_is_rejected(self):
        env = V.validate_envelope(envelope(C.INTENT_NEEDS_CLARIFICATION, {
            "candidate_intent": "log_supplement", "missing_fields": ["diagnosis"],
            "question": "Это точно лечит бессонницу?"}))
        res = V.validate(env, local_now=NOW)
        self.assertFalse(res.ok)

    def test_valid_marker_cannot_smuggle_raw_missing_field_text(self):
        env = V.validate_envelope(envelope(C.INTENT_NEEDS_CLARIFICATION, {
            "candidate_intent": "log_supplement",
            "missing_fields": ["taken", "raw_secret_user_text"],
            "question": "Принял?"}))
        res = V.validate(env, local_now=NOW)
        self.assertFalse(res.ok)

    def test_candidate_intent_must_be_known(self):
        env = V.validate_envelope(envelope(C.INTENT_NEEDS_CLARIFICATION, {
            "candidate_intent": "rm_rf", "missing_fields": [], "question": "?"}))
        res = V.validate(env, local_now=NOW)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, C.ERR_UNKNOWN_INTENT)


class ToolAllowlistTests(unittest.TestCase):
    def test_unknown_tool_raises(self):
        ctx = tools.ExecContext(action_id="a", source="t", chat_id="1",
                                message_id="1", local_now=NOW)
        with self.assertRaises(tools.ToolError):
            tools.execute("run_shell", {}, ctx)

    def test_every_mapped_tool_is_in_allowlist(self):
        for intent, tool in C.INTENT_TO_TOOL.items():
            self.assertIn(tool, C.ALLOWED_TOOLS)
            self.assertIn(tool, tools._TOOLS)

    def test_non_actioning_intents_have_no_tool(self):
        for intent in C.NON_ACTIONING_INTENTS:
            self.assertIsNone(C.tool_for_intent(intent))


if __name__ == "__main__":
    unittest.main()
