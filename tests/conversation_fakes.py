"""Deterministic fakes for the conversation router tests.

No network, no real Telegram. A fake Gemini client returns scripted transport
responses (or raises); fake Telegram objects mimic the fields the handler reads.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gemini_client import GeminiClient, HttpResponse, _TransientTransport


class TempDBCase(unittest.TestCase):
    """Base case that points daily_log/workouts_db at a throwaway SQLite file.

    conversation_store.connect() flows through daily_log.connect(), so this is
    all that is needed to isolate audit + pending + domain rows per test.
    """

    def setUp(self):
        import daily_log
        import workouts_db
        self.temp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.temp.name, "whoop.db")
        self._dl = daily_log.DB_PATH
        self._wd = workouts_db.DB_PATH
        daily_log.DB_PATH = self.db
        workouts_db.DB_PATH = self.db

    def tearDown(self):
        import daily_log
        import workouts_db
        daily_log.DB_PATH = self._dl
        workouts_db.DB_PATH = self._wd
        self.temp.cleanup()


def envelope(intent, arguments=None, *, confidence=0.95, requires_confirmation=False,
             reply_text=None, schema_version="conversation_router_v1"):
    return {
        "schema_version": schema_version,
        "intent": intent,
        "confidence": confidence,
        "requires_confirmation": requires_confirmation,
        "arguments": arguments if arguments is not None else {},
        "reply_text": reply_text,
    }


def ok_body(payload_dict_or_text):
    """Wrap a router JSON payload as a Gemini 200 generateContent body."""
    if isinstance(payload_dict_or_text, (dict, list)):
        text = json.dumps(payload_dict_or_text, ensure_ascii=False)
    else:
        text = payload_dict_or_text
    return {"candidates": [{"content": {"parts": [{"text": text}]},
                            "finishReason": "STOP"}]}


def tool_body(name, args):
    """Wrap one native Gemini function call."""
    return {
        "candidates": [{
            "content": {
                "role": "model",
                "parts": [{"functionCall": {"name": name, "args": args}}],
            },
            "finishReason": "STOP",
        }],
    }


class ScriptedTransport:
    """Callable transport for GeminiClient. Each queued step is either an
    HttpResponse, a raised transport error marker, or a GeminiError instance."""

    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = []

    def __call__(self, model, payload, timeout):
        self.calls.append((model, timeout))
        step = self._steps.pop(0) if self._steps else self._steps_default()
        if isinstance(step, Exception):
            raise step
        if step == "transient":
            raise _TransientTransport("scripted transient")
        return step

    def _steps_default(self):
        raise _TransientTransport("no more scripted steps")


def make_client(steps, *, model="fake-primary", fallbacks=("fake-fallback",),
                deadline_s=5.0, primary_timeout_s=5.0, fallback_min_timeout_s=0.0):
    return GeminiClient(
        api_key="fake-key", model=model, fallback_models=list(fallbacks),
        transport=ScriptedTransport(steps), deadline_s=deadline_s,
        primary_timeout_s=primary_timeout_s,
        fallback_min_timeout_s=fallback_min_timeout_s, clock=_StepClock(),
    )


class _StepClock:
    """Monotonic-ish clock that advances a little each call so deadlines behave."""

    def __init__(self, step=0.001):
        self._t = 0.0
        self._step = step

    def __call__(self):
        self._t += self._step
        return self._t


def single_response(router_payload):
    """A client that returns one scripted valid/invalid router payload."""
    return make_client([HttpResponse(200, ok_body(router_payload))])


class FakeSentMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class FakeBot:
    """Records sent messages and hands back incrementing message ids."""

    def __init__(self, start_id=1000):
        self.sent = []
        self._next = start_id

    def send_message(self, chat_id, text, **kwargs):
        self._next += 1
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, message_id=self._next))
        return FakeSentMessage(self._next)


def message(message_id, text, *, chat_id=1, user_id=42, reply_to_message_id=None):
    reply = SimpleNamespace(message_id=reply_to_message_id) if reply_to_message_id else None
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id),
        reply_to_message=reply,
    )
