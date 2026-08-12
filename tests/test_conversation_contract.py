"""Unit tests: strict envelope, semantic validation, dates, facts, confidence."""
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
from conversation_fakes import envelope

KYIV = ZoneInfo("Europe/Kyiv")


def _v(intent, arguments, *, confidence=0.95, now=None, reply_evening_date=None,
       reply_text=None):
    now = now or dt.datetime(2026, 7, 12, 15, 0, tzinfo=KYIV)
    env = V.validate_envelope(envelope(intent, arguments, confidence=confidence,
                                       reply_text=reply_text))
    assert env.ok, (env.error_code, env.error_detail)
    return V.validate(env, local_now=now, reply_evening_date=reply_evening_date)


class EnvelopeTests(unittest.TestCase):
    def test_unknown_keys_rejected(self):
        obj = envelope(C.INTENT_GET_TODAY_STATUS)
        obj["surprise"] = 1
        self.assertEqual(V.validate_envelope(obj).error_code, C.ERR_UNKNOWN_KEYS)

    def test_missing_keys_rejected(self):
        obj = envelope(C.INTENT_GET_TODAY_STATUS)
        del obj["confidence"]
        self.assertEqual(V.validate_envelope(obj).error_code, C.ERR_MISSING_KEYS)

    def test_bad_schema_version(self):
        obj = envelope(C.INTENT_GET_TODAY_STATUS, schema_version="v0")
        self.assertEqual(V.validate_envelope(obj).error_code, C.ERR_SCHEMA_VERSION)

    def test_unknown_intent(self):
        obj = envelope("drop_tables")
        self.assertEqual(V.validate_envelope(obj).error_code, C.ERR_UNKNOWN_INTENT)

    def test_confidence_must_be_number(self):
        obj = envelope(C.INTENT_GET_TODAY_STATUS, confidence="high")
        self.assertEqual(V.validate_envelope(obj).error_code, C.ERR_BAD_CONFIDENCE)

    def test_bool_is_not_confidence(self):
        obj = envelope(C.INTENT_GET_TODAY_STATUS, confidence=True)
        self.assertEqual(V.validate_envelope(obj).error_code, C.ERR_BAD_CONFIDENCE)


class FactVsPlanTests(unittest.TestCase):
    def test_completed_workout_writes(self):
        res = _v(C.INTENT_LOG_STRENGTH, {
            "date_ref": {"kind": "today", "value": None},
            "fact_status": "completed",
            "entries": [{"exercise_name": "жим", "weight_kg": 80, "sets": 4, "reps": 8}],
        })
        self.assertTrue(res.ok)
        self.assertEqual(res.tool, "log_strength_workout")

    def test_plan_workout_not_a_fact(self):
        res = _v(C.INTENT_LOG_STRENGTH, {
            "date_ref": {"kind": "today", "value": None},
            "fact_status": "planned",
            "entries": [{"exercise_name": "жим", "weight_kg": 80, "sets": 4, "reps": 8}],
        })
        # Planned is a safe terminal no-write with its own explanation, not a
        # hard reject - the entries are simply never validated/written.
        self.assertTrue(res.ok)
        self.assertIsNone(res.tool)
        self.assertIsNone(res.clarification)
        self.assertEqual(res.reply_text, C.MSG_PLANNED_WORKOUT)

    def test_current_workout_writes(self):
        res = _v(C.INTENT_LOG_STRENGTH, {
            "date_ref": {"kind": "today", "value": None},
            "fact_status": "current",
            "entries": [{"exercise_name": "жим", "weight_kg": 80, "sets": 4, "reps": 8}],
        })
        self.assertTrue(res.ok)
        self.assertEqual(res.tool, "log_strength_workout")

    def test_missing_fact_status_clarifies_and_preserves_original_arguments(self):
        original_args = {
            "date_ref": {"kind": "today", "value": None},
            "entries": [{"exercise_name": "жим", "weight_kg": 80, "sets": 4, "reps": 8}],
        }  # no fact_status key at all
        res = _v(C.INTENT_LOG_STRENGTH, dict(original_args))
        self.assertTrue(res.ok)
        self.assertIsNone(res.tool)
        self.assertIsNotNone(res.clarification)
        self.assertEqual(res.clarification["question"], C.MSG_FACT_STATUS_CLARIFICATION)
        self.assertEqual(res.clarification["missing_fields"], ["fact_status"])
        self.assertEqual(res.arguments["entries"], original_args["entries"])

    def test_unknown_fact_status_clarifies(self):
        res = _v(C.INTENT_LOG_CARDIO, {
            "date_ref": {"kind": "today", "value": None}, "fact_status": "unknown",
            "activity_type": "бег", "duration_minutes": 30,
        })
        self.assertTrue(res.ok)
        self.assertIsNone(res.tool)
        self.assertIsNotNone(res.clarification)

    def test_not_applicable_workout_clarifies(self):
        res = _v(C.INTENT_LOG_STRENGTH, {
            "date_ref": {"kind": "today", "value": None}, "fact_status": "not_applicable",
            "entries": [{"exercise_name": "жим", "weight_kg": 80, "sets": 4, "reps": 8}],
        })
        self.assertTrue(res.ok)
        self.assertIsNone(res.tool)
        self.assertIsNotNone(res.clarification)

    def test_not_applicable_non_workout_intent_passes(self):
        res = _v(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "fact_status": "not_applicable",
            "time": None, "items": [{"name": "магний", "dose_text": "400мг", "taken": True}],
        })
        self.assertTrue(res.ok)
        self.assertEqual(res.tool, "log_supplement")

    def test_supplement_taken_false_is_kept(self):
        res = _v(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None},
            "time": None,
            "items": [{"name": "магний", "dose_text": "400мг", "taken": False}],
        })
        self.assertTrue(res.ok)
        self.assertFalse(res.arguments["items"][0]["taken"])

    def test_supplement_unknown_status_clarifies(self):
        res = _v(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None},
            "time": None,
            "items": [{"name": "магний", "dose_text": "400мг", "taken": None}],
        })
        self.assertTrue(res.ok)
        self.assertIsNotNone(res.clarification)
        self.assertIsNone(res.tool)


class DateResolutionTests(unittest.TestCase):
    def test_workout_before_5am_is_yesterday(self):
        now = dt.datetime(2026, 7, 12, 3, 0, tzinfo=KYIV)
        res = _v(C.INTENT_LOG_CARDIO, {
            "date_ref": {"kind": "unspecified", "value": None},
            "fact_status": "completed", "activity_type": "бег",
            "duration_minutes": 30,
        }, now=now)
        self.assertEqual(res.resolved_date, "2026-07-11")

    def test_supplement_before_13_is_yesterday(self):
        now = dt.datetime(2026, 7, 12, 9, 0, tzinfo=KYIV)
        res = _v(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "unspecified", "value": None}, "time": None,
            "items": [{"name": "креатин", "dose_text": "5г", "taken": True}],
        }, now=now)
        self.assertEqual(res.resolved_date, "2026-07-11")

    def test_absolute_date_used(self):
        res = _v(C.INTENT_LOG_CARDIO, {
            "date_ref": {"kind": "absolute", "value": "2026-07-09"},
            "fact_status": "completed", "activity_type": "бег", "duration_minutes": 30,
        })
        self.assertEqual(res.resolved_date, "2026-07-09")

    def test_future_date_rejected(self):
        res = _v(C.INTENT_LOG_CARDIO, {
            "date_ref": {"kind": "absolute", "value": "2027-01-01"},
            "fact_status": "completed", "activity_type": "бег", "duration_minutes": 30,
        })
        self.assertEqual(res.error_code, C.ERR_FUTURE_DATE)

    def test_too_old_date_rejected(self):
        res = _v(C.INTENT_LOG_CARDIO, {
            "date_ref": {"kind": "absolute", "value": "2020-01-01"},
            "fact_status": "completed", "activity_type": "бег", "duration_minutes": 30,
        })
        self.assertEqual(res.error_code, C.ERR_DATE_TOO_OLD)

    def test_ambiguous_date_clarifies(self):
        res = _v(C.INTENT_LOG_CARDIO, {
            "date_ref": {"kind": "ambiguous", "value": None},
            "fact_status": "completed", "activity_type": "бег", "duration_minutes": 30,
        })
        self.assertIsNotNone(res.clarification)

    def test_daily_context_needs_anchor_without_reply(self):
        res = _v(C.INTENT_SAVE_DAILY_CONTEXT, {
            "date_ref": {"kind": "unspecified", "value": None}, "notes": "стресс",
        })
        self.assertIsNotNone(res.clarification)

    def test_daily_context_reply_wins(self):
        res = _v(C.INTENT_SAVE_DAILY_CONTEXT, {
            "date_ref": {"kind": "unspecified", "value": None}, "notes": "стресс",
        }, reply_evening_date="2026-07-11")
        self.assertTrue(res.ok)
        self.assertEqual(res.resolved_date, "2026-07-11")


class ConfidenceGateTests(unittest.TestCase):
    def test_mutation_below_090_blocked(self):
        res = _v(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": None,
            "items": [{"name": "креатин", "dose_text": "5г", "taken": True}],
        }, confidence=0.89)
        self.assertEqual(res.error_code, C.ERR_LOW_CONFIDENCE)

    def test_read_below_080_blocked(self):
        res = _v(C.INTENT_GET_TODAY_STATUS, {}, confidence=0.79)
        self.assertEqual(res.error_code, C.ERR_LOW_CONFIDENCE)

    def test_general_below_070_blocked(self):
        res = _v(C.INTENT_GENERAL_CONVERSATION, {"topic": "x"}, confidence=0.69,
                 reply_text="ок")
        self.assertEqual(res.error_code, C.ERR_LOW_CONFIDENCE)


class MultiSetFormatTests(unittest.TestCase):
    def test_two_sets_same_weight_different_reps_become_two_entries(self):
        # "30x9, 30x8": two real sets at the same weight, different reps.
        # Must become two entries with the values exactly as given - never
        # merged, summed, or invented.
        res = _v(C.INTENT_LOG_STRENGTH, {
            "date_ref": {"kind": "today", "value": None}, "fact_status": "completed",
            "entries": [
                {"exercise_name": "жим", "weight_kg": 30, "sets": 1, "reps": 9},
                {"exercise_name": "жим", "weight_kg": 30, "sets": 1, "reps": 8},
            ],
        })
        self.assertTrue(res.ok)
        entries = res.arguments["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual((entries[0]["weight_kg"], entries[0]["reps"]), (30, 9))
        self.assertEqual((entries[1]["weight_kg"], entries[1]["reps"]), (30, 8))


class LimitsTests(unittest.TestCase):
    def test_too_many_entries(self):
        res = _v(C.INTENT_LOG_STRENGTH, {
            "date_ref": {"kind": "today", "value": None}, "fact_status": "completed",
            "entries": [{"exercise_name": "жим", "weight_kg": 80, "sets": 1, "reps": 5}]
                       * (C.MAX_WORKOUT_ENTRIES + 1),
        })
        self.assertEqual(res.error_code, C.ERR_TOO_MANY_ITEMS)

    def test_empty_items_rejected(self):
        res = _v(C.INTENT_LOG_SUPPLEMENT, {
            "date_ref": {"kind": "today", "value": None}, "time": None, "items": [],
        })
        self.assertEqual(res.error_code, C.ERR_EMPTY_LIST)

    def test_absurd_weight_rejected(self):
        res = _v(C.INTENT_LOG_STRENGTH, {
            "date_ref": {"kind": "today", "value": None}, "fact_status": "completed",
            "entries": [{"exercise_name": "жим", "weight_kg": 99999, "sets": 1, "reps": 5}],
        })
        self.assertEqual(res.error_code, C.ERR_VALUE_OUT_OF_RANGE)

    def test_input_too_long(self):
        self.assertEqual(
            V.validate_input("x" * (C.MAX_INPUT_CHARS + 1)).error_code,
            C.ERR_INPUT_TOO_LONG,
        )

    def test_control_chars_rejected(self):
        self.assertEqual(V.validate_input("hi\x00there").error_code,
                         C.ERR_INPUT_CONTROL_CHARS)


if __name__ == "__main__":
    unittest.main()
