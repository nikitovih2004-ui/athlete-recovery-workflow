import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import telegram_bot


def msg(chat_id=1, user_id=1, chat_type="private"):
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        from_user=SimpleNamespace(id=user_id) if user_id is not None else None,
    )


class TelegramAuthorizationTests(unittest.TestCase):
    def test_chat_and_optional_user_are_both_enforced(self):
        with patch.object(telegram_bot, "TG_CHAT", "1"), \
             patch.object(telegram_bot, "TG_USER", "7"):
            self.assertTrue(telegram_bot.is_authorized_message(msg(1, 7)))
            self.assertFalse(telegram_bot.is_authorized_message(msg(2, 7)))
            self.assertFalse(telegram_bot.is_authorized_message(msg(1, 8)))
            self.assertFalse(telegram_bot.is_authorized_message(msg(1, None)))

    def test_group_chat_is_rejected(self):
        with patch.object(telegram_bot, "TG_CHAT", "1"), \
             patch.object(telegram_bot, "TG_USER", ""):
            self.assertFalse(telegram_bot.is_authorized_message(msg(1, 1, "group")))

    def test_legacy_message_without_chat_type_remains_compatible(self):
        legacy = SimpleNamespace(chat=SimpleNamespace(id=1), from_user=None)
        with patch.object(telegram_bot, "TG_CHAT", "1"), \
             patch.object(telegram_bot, "TG_USER", ""):
            self.assertTrue(telegram_bot.is_authorized_message(legacy))


if __name__ == "__main__":
    unittest.main()
