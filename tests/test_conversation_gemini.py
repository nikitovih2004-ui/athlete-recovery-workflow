"""Bounded Gemini client: fallback, transient retry, permanent/safety fail-closed."""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import gemini_client
from gemini_client import (
    GeminiClient, GeminiRejected, GeminiSafetyBlock, GeminiUnavailable, HttpResponse,
    _TransientTransport, _allocate_call_timeout,
)
from conversation_fakes import make_client, ok_body, envelope


VALID = envelope("get_today_status", {})


class _ElapsingClock:
    """Fake clock that only advances when `consume` is called - lets a test
    assert exactly how much of the deadline a scripted call used up."""

    def __init__(self):
        self.t = 0.0

    def consume(self, seconds):
        self.t += seconds

    def __call__(self):
        return self.t


def _elapsing_transport(clock, steps):
    """Transport where each step is (elapsed_seconds, outcome). `outcome` is
    an HttpResponse, the string 'transient', or an Exception instance. The
    clock advances by elapsed_seconds before the outcome is applied, so a
    scripted timeout can simulate consuming its full allotted budget."""
    queue = list(steps)
    calls = []

    def transport(model, payload, timeout):
        calls.append((model, timeout))
        elapsed, outcome = queue.pop(0)
        clock.consume(elapsed)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "transient":
            raise _TransientTransport("scripted transient")
        return outcome

    transport.calls = calls
    return transport


class GeminiClientTests(unittest.TestCase):
    def test_primary_success(self):
        client = make_client([HttpResponse(200, ok_body(VALID))])
        result = client.generate("sys", "user")
        self.assertEqual(result.model, "fake-primary")
        self.assertEqual(result.attempt_count, 1)

    def test_transient_falls_back_once(self):
        client = make_client([
            HttpResponse(503, None),
            HttpResponse(200, ok_body(VALID)),
        ])
        result = client.generate("sys", "user")
        self.assertEqual(result.model, "fake-fallback")
        self.assertEqual(result.attempt_count, 2)

    def test_network_error_falls_back(self):
        client = make_client(["transient", HttpResponse(200, ok_body(VALID))])
        result = client.generate("sys", "user")
        self.assertEqual(result.model, "fake-fallback")

    def test_permanent_400_not_retried(self):
        client = make_client([HttpResponse(400, None), HttpResponse(200, ok_body(VALID))])
        with self.assertRaises(GeminiRejected):
            client.generate("sys", "user")

    def test_auth_403_not_retried(self):
        client = make_client([HttpResponse(403, None)])
        with self.assertRaises(GeminiRejected):
            client.generate("sys", "user")

    def test_safety_block_raises(self):
        body = {"promptFeedback": {"blockReason": "SAFETY"}}
        client = make_client([HttpResponse(200, body)])
        with self.assertRaises(GeminiSafetyBlock):
            client.generate("sys", "user")

    def test_all_transient_exhausts_to_unavailable(self):
        client = make_client([HttpResponse(500, None), HttpResponse(503, None)])
        with self.assertRaises(GeminiUnavailable):
            client.generate("sys", "user")

    def test_missing_api_key_unavailable(self):
        client = GeminiClient(api_key="", model="m", transport=lambda *a: None)
        with self.assertRaises(GeminiUnavailable):
            client.generate("sys", "user")

    def test_only_primary_plus_one_fallback(self):
        # Three transient steps but the client must stop after 2 models.
        client = make_client(
            [HttpResponse(503, None), HttpResponse(503, None), HttpResponse(200, ok_body(VALID))],
            fallbacks=("fb1", "fb2"),
        )
        with self.assertRaises(GeminiUnavailable):
            client.generate("sys", "user")


class ResponseSchemaTests(unittest.TestCase):
    """arguments must no longer be a bare free object, and fact_status must
    be a required enum - the schema gap that let a real workout's
    fact_status go silently missing."""

    def _arguments_schema(self):
        return gemini_client.RESPONSE_SCHEMA["properties"]["arguments"]

    def test_arguments_is_no_longer_a_bare_free_object(self):
        args_schema = self._arguments_schema()
        self.assertIn("properties", args_schema)
        self.assertGreater(len(args_schema["properties"]), 1)

    def test_arguments_schema_requires_fact_status(self):
        args_schema = self._arguments_schema()
        self.assertIn("fact_status", args_schema.get("required", []))

    def test_fact_status_enum_has_all_five_values(self):
        args_schema = self._arguments_schema()
        enum = set(args_schema["properties"]["fact_status"]["enum"])
        self.assertEqual(
            enum, {"completed", "current", "planned", "unknown", "not_applicable"}
        )


class AllocateCallTimeoutTests(unittest.TestCase):
    """Pure-function tests for the budget-allocation policy itself."""

    def test_primary_capped_below_full_deadline(self):
        timeout = _allocate_call_timeout(0, 0.0, deadline_s=35.0,
                                          primary_timeout_s=18.0, fallback_min_timeout_s=13.0)
        self.assertEqual(timeout, 18.0)

    def test_fallback_gets_remaining_when_above_minimum(self):
        # Primary consumed exactly its 18s cap.
        timeout = _allocate_call_timeout(1, 18.0, deadline_s=35.0,
                                          primary_timeout_s=18.0, fallback_min_timeout_s=13.0)
        self.assertEqual(timeout, 17.0)
        self.assertGreaterEqual(timeout, 13.0)

    def test_fallback_skipped_when_remaining_below_minimum(self):
        # Only 10s left, but the fallback needs at least 13s to be worthwhile.
        timeout = _allocate_call_timeout(1, 25.0, deadline_s=35.0,
                                          primary_timeout_s=18.0, fallback_min_timeout_s=13.0)
        self.assertIsNone(timeout)


class TimeoutBudgetPolicyTests(unittest.TestCase):
    """Integration-level tests: the full generate() call under the new policy."""

    def _client(self, transport, **overrides):
        kwargs = dict(deadline_s=35.0, primary_timeout_s=18.0, fallback_min_timeout_s=13.0)
        kwargs.update(overrides)
        return GeminiClient(api_key="fake-key", model="primary",
                            fallback_models=["fallback"], transport=transport, **kwargs)

    # 1. Primary timing out leaves a guaranteed budget for the fallback.
    def test_primary_timeout_leaves_guaranteed_fallback_budget(self):
        clock = _ElapsingClock()
        transport = _elapsing_transport(clock, [
            (18.0, "transient"),
            (2.0, HttpResponse(200, ok_body(VALID))),
        ])
        client = self._client(transport, clock=clock)
        client.generate("sys", "user")
        self.assertEqual(transport.calls[0], ("primary", 18.0))
        fallback_model, fallback_timeout = transport.calls[1]
        self.assertEqual(fallback_model, "fallback")
        self.assertGreaterEqual(fallback_timeout, 13.0)
        self.assertEqual(fallback_timeout, 17.0)

    # 2. The fallback can succeed after the primary timed out.
    def test_fallback_succeeds_after_primary_timeout(self):
        clock = _ElapsingClock()
        transport = _elapsing_transport(clock, [
            (18.0, "transient"),
            (3.0, HttpResponse(200, ok_body(VALID))),
        ])
        client = self._client(transport, clock=clock)
        result = client.generate("sys", "user")
        self.assertEqual(result.model, "fallback")
        self.assertEqual(result.attempt_count, 2)

    # 3. A successful primary call never triggers the fallback.
    def test_primary_success_skips_fallback(self):
        clock = _ElapsingClock()
        transport = _elapsing_transport(clock, [(2.0, HttpResponse(200, ok_body(VALID)))])
        client = self._client(transport, clock=clock)
        result = client.generate("sys", "user")
        self.assertEqual(result.model, "primary")
        self.assertEqual(len(transport.calls), 1)

    # 4a. Both attempts timing out raises GeminiUnavailable with zero writes
    # possible (no GeminiResult is ever returned).
    def test_both_calls_timeout_raises_unavailable(self):
        clock = _ElapsingClock()
        transport = _elapsing_transport(clock, [(18.0, "transient"), (17.0, "transient")])
        client = self._client(transport, clock=clock)
        with self.assertRaises(GeminiUnavailable):
            client.generate("sys", "user")
        self.assertEqual(len(transport.calls), 2)

    # 4b. If the primary eats so much of the deadline that the remainder is
    # below the guaranteed minimum, the fallback is skipped outright instead
    # of being started on a doomed budget.
    def test_insufficient_remaining_budget_skips_fallback_call(self):
        clock = _ElapsingClock()
        # Primary "times out" after consuming 25s of the 35s deadline -
        # only 10s remain, below the 13s fallback minimum.
        transport = _elapsing_transport(clock, [(25.0, "transient")])
        client = self._client(transport, clock=clock)
        with self.assertRaises(GeminiUnavailable) as ctx:
            client.generate("sys", "user")
        self.assertEqual(len(transport.calls), 1, "fallback must not be called")
        self.assertIn("skipped", str(ctx.exception.detail))

    # 5. Total elapsed time never exceeds the deadline by more than a small
    # margin, even in the worst case (primary consumes its full cap, then
    # the fallback consumes its full remaining allotment).
    def test_total_elapsed_stays_within_deadline_margin(self):
        clock = _ElapsingClock()
        transport = _elapsing_transport(clock, [(18.0, "transient"), (17.0, "transient")])
        client = self._client(transport, clock=clock)
        with self.assertRaises(GeminiUnavailable):
            client.generate("sys", "user")
        self.assertLessEqual(clock.t, 35.0 + 0.001)

    # 6. Permanent failures (auth/config/schema) never trigger a fallback call.
    def test_permanent_failures_never_invoke_fallback(self):
        cases = [
            ("HTTP 400", HttpResponse(400, None)),
            ("HTTP 401", HttpResponse(401, None)),
            ("HTTP 403", HttpResponse(403, None)),
            ("empty body", HttpResponse(200, None)),
            ("no candidates", HttpResponse(200, {"candidates": []})),
        ]
        for label, first_response in cases:
            with self.subTest(label):
                clock = _ElapsingClock()
                transport = _elapsing_transport(clock, [(1.0, first_response)])
                client = self._client(transport, clock=clock)
                with self.assertRaises(GeminiRejected):
                    client.generate("sys", "user")
                self.assertEqual(len(transport.calls), 1)

    # 7. Transient HTTP statuses and transport-level timeouts all use the fallback.
    def test_transient_statuses_and_timeouts_use_fallback(self):
        transient_first_steps = [429, 500, 502, 503, 504, "transient"]
        for first in transient_first_steps:
            with self.subTest(first=first):
                clock = _ElapsingClock()
                first_outcome = "transient" if first == "transient" else HttpResponse(first, None)
                transport = _elapsing_transport(clock, [
                    (1.0, first_outcome),
                    (1.0, HttpResponse(200, ok_body(VALID))),
                ])
                client = self._client(transport, clock=clock)
                result = client.generate("sys", "user")
                self.assertEqual(result.model, "fallback")
                self.assertEqual(len(transport.calls), 2)


if __name__ == "__main__":
    unittest.main()
