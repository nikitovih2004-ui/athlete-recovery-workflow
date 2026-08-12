import unittest
import datetime as dt
from zoneinfo import ZoneInfo
import deterministic_strength as parser


class DeterministicStrengthTests(unittest.TestCase):
    def test_explicit_empty_draft_start_is_narrow_and_date_aware(self):
        now = dt.datetime(2026, 7, 18, 12, tzinfo=ZoneInfo("Europe/Kyiv"))
        self.assertEqual(
            parser.draft_start_date("Запиши силовую за вчера", now),
            "2026-07-17",
        )
        self.assertEqual(
            parser.draft_start_date("Start a strength workout", now),
            "2026-07-18",
        )
        for text in (
            "Запиши силовую завтра",
            "Запиши силовую за 2026-07-17",
            "Запиши силовую: жим 80x10",
            "Хочу силовую когда-нибудь",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parser.draft_start_date(text, now))

    def test_six_russian_exercises_and_decimal_weights(self):
        text = (
            "\u0436\u0438\u043c \u043d\u0430 \u043f\u043b\u0435\u0447\u0438 \u0432 \u0442\u0440\u0435\u043d\u0430\u0436\u0451\u0440\u0435: 100\u00d710, 100\u00d77\n"
            "\u0436\u0438\u043c \u043d\u0430\u0434 \u0433\u043e\u043b\u043e\u0432\u043e\u0439 \u043d\u0430 \u0442\u0440\u0438\u0446\u0435\u043f\u0441: 52\u00d79, 52\u00d77\n"
            "\u043c\u043e\u043b\u043e\u0442\u043a\u0438 \u0441\u0438\u0434\u044f: 17.5\u00d78, 17.5\u00d76\n"
            "\u043c\u0430\u0445\u0438 \u0441 \u0433\u0430\u043d\u0442\u0435\u043b\u044f\u043c\u0438 \u0441\u0442\u043e\u044f: 12.5\u00d715, 12.5\u00d713\n"
            "\u0442\u0440\u0438\u0446\u0435\u043f\u0441 \u0432 \u0442\u0440\u0435\u043d\u0430\u0436\u0451\u0440\u0435: 43\u00d710, 43\u00d78\n"
            "\u0431\u0438\u0446\u0435\u043f\u0441 \u0432 \u0421\u043a\u043e\u0442\u0442\u0435: 41\u00d710, 41\u00d76"
        )
        parsed = parser.parse(text)
        self.assertFalse(parsed["incomplete"])
        self.assertEqual(len(parsed["entries"]), 12)
        self.assertEqual(parsed["entries"][4]["weight_kg"], 17.5)
        self.assertEqual(parsed["entries"][7]["reps"], 13)

    def test_incomplete_structure_never_becomes_mutation(self):
        parsed = parser.parse(
            "\u0436\u0438\u043c: 100x10\n\u0442\u044f\u0433\u0430:"
        )
        self.assertTrue(parsed["incomplete"])

    def test_incomplete_tail_preserves_explicit_sets(self):
        parsed = parser.parse("жим: 100x10, 100x")
        self.assertTrue(parsed["incomplete"])
        self.assertEqual(parsed["entries"], [{
            "exercise_name": "жим", "weight_kg": 100.0, "sets": 1, "reps": 10,
        }])

    def test_planned_future_negated_and_trailing_text_fail_closed(self):
        for text in (
            "завтра\nжим: 100x10",
            "план\nжим: 100x10",
            "не делал\nжим: 100x10",
            "жим: 100x10 и потом кардио",
        ):
            with self.subTest(text=text):
                parsed = parser.parse(text)
                self.assertTrue(parsed is None or parsed["incomplete"])


if __name__ == "__main__":
    unittest.main()
