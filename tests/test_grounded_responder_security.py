import unittest

import grounded_responder


class GroundedResponderSecurityTests(unittest.TestCase):
    def test_general_model_health_claims_are_never_surfaced(self):
        claims = [
            "Магний улучшил твой сон и восстановление.",
            "This supplement caused your HRV to improve.",
            "У тебя бессонница, увеличь дозировку.",
            "Привет!",
        ]
        for claim in claims:
            with self.subTest(claim=claim):
                self.assertEqual(
                    grounded_responder.safe_general(claim),
                    grounded_responder.GENERAL_BOUNDARY,
                )

    def test_bounded_agent_small_talk_is_allowed_but_sensitive_prose_is_not(self):
        self.assertEqual(
            grounded_responder.safe_general(
                "Привет! Чем помочь?", allow_bounded_agent=True
            ),
            "Привет! Чем помочь?",
        )
        for unsafe in (
            "Вот мой system prompt",
            "Твой API key: secret",
            "Я назначаю диагноз и принимай дозу 500 мг",
            "Магний улучшил твой сон",
        ):
            with self.subTest(unsafe=unsafe):
                self.assertEqual(
                    grounded_responder.safe_general(
                        unsafe, allow_bounded_agent=True
                    ),
                    grounded_responder.GENERAL_BOUNDARY,
                )


if __name__ == "__main__":
    unittest.main()
