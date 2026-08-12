import datetime as dt
import unittest
from zoneinfo import ZoneInfo

import deterministic_reads as reads
import conversation_contract as C
import conversation_read_models as models
from unittest.mock import patch
import json


NOW = dt.datetime(2026, 7, 16, 12, tzinfo=ZoneInfo("Europe/Kyiv"))


class DeterministicReadPlannerTests(unittest.TestCase):
    def test_hrv_phrases_and_followup_are_grounded(self):
        for text in ("какой тренд HRV за последнюю неделю", "какой тренд HRV", "тренд HRV"):
            self.assertEqual(reads.plan(text, NOW)[0], C.INTENT_GET_METRIC_TREND)
            self.assertEqual(reads.plan(text, NOW)[1]["metric"], "hrv_rmssd")
        followup = reads.plan("а за месяц?", NOW, {"last_query": {"metric": "hrv_rmssd", "window_days": 7}})
        self.assertEqual(followup, (C.INTENT_GET_METRIC_TREND, {"metric": "hrv_rmssd", "window_days": 28}))

    def test_yesterday_coverage_and_supplement_ledger(self):
        self.assertEqual(reads.plan("что было вчера", NOW)[0], C.INTENT_GET_DAY_SNAPSHOT)
        self.assertEqual(reads.plan("за сколько дней накоплены данные и какие", NOW)[0], C.INTENT_GET_DATA_COVERAGE)
        self.assertEqual(reads.plan("какие сейчас записи по БАДам", NOW)[0], C.INTENT_GET_SUPPLEMENT_RECORDS)

    def test_never_classifies_mutation_or_general_text_as_read(self):
        self.assertIsNone(reads.plan("вчера сделал жим 80x8", NOW))
        self.assertIsNone(reads.plan("привет, как дела?", NOW))

    def test_yesterday_snapshot_never_copies_context_note_into_audit_data(self):
        sentinel = "PRIVATE_DAILY_CONTEXT_SENTINEL"
        raw = {"days": [{"next_morning": {"hrv_rmssd": 50},
                         "activities": {"manual_strength": [{"raw_text": sentinel}], "manual_cardio": []},
                         "supplements": [{"raw_text": sentinel}], "context": {"notes": sentinel}}],
               "completeness": {}}
        with patch.object(models.CRM, "range_snapshot", return_value=raw):
            snapshot = models.day_snapshot(object(), "2026-07-15")
        self.assertNotIn(sentinel, json.dumps(snapshot, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
