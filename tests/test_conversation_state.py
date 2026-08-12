"""Store tests: dedup ledger, concurrency, pending lifecycle, migration."""
import os
import sqlite3
import sys
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import conversation_contract as C
import conversation_store as store
from conversation_fakes import TempDBCase


def _ctx(message_id="m1", user_id="42"):
    return store.ActionContext(source="telegram", chat_id="1", message_id=message_id,
                               user_id=user_id, input_text="hello")


class ReservationTests(TempDBCase):
    def test_first_reserve_is_new(self):
        res = store.reserve(_ctx())
        self.assertTrue(res.is_new)

    def test_duplicate_message_reserves_once(self):
        first = store.reserve(_ctx("dup"))
        second = store.reserve(_ctx("dup"))
        self.assertTrue(first.is_new)
        self.assertFalse(second.is_new)
        self.assertEqual(first.action_id, second.action_id)

    def test_concurrent_duplicate_single_winner(self):
        store.connect().close()  # ensure tables before threads race
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: store.reserve(_ctx("race")), range(2)))
        new_flags = [r.is_new for r in results]
        self.assertEqual(new_flags.count(True), 1)
        self.assertEqual({r.action_id for r in results}.__len__(), 1)


class AuditTransitionTests(TempDBCase):
    def test_terminal_statuses(self):
        a = store.reserve(_ctx("a")).action_id
        store.mark_noop(a, C.INTENT_GENERAL_CONVERSATION, 0.9)
        self.assertEqual(store.get_action(a)["status"], C.ACTION_NOOP)

        b = store.reserve(_ctx("b")).action_id
        store.mark_failed(b, "router_unavailable")
        self.assertEqual(store.get_action(b)["status"], C.ACTION_FAILED)

    def test_router_metadata_stored_as_hashes(self):
        a = store.reserve(_ctx("h")).action_id
        store.record_router(a, model="m", response_sha256="deadbeef",
                            intent="log_cardio", confidence=0.95)
        row = store.get_action(a)
        self.assertEqual(row["router_response_sha256"], "deadbeef")
        self.assertEqual(row["router_schema_version"], C.SCHEMA_VERSION)


class PendingTests(TempDBCase):
    def test_create_and_fetch_active(self):
        origin = store.reserve(_ctx("p1")).action_id
        pid = store.create_pending(origin, _ctx("p1"), "log_supplement",
                                   {"items": []}, ["taken"])
        active = store.active_pending("telegram", "1", "42")
        self.assertEqual(active["pending_id"], pid)

    def test_only_one_active_per_user(self):
        o1 = store.reserve(_ctx("p1")).action_id
        o2 = store.reserve(_ctx("p2")).action_id
        first = store.create_pending(o1, _ctx("p1"), "log_supplement", {}, [])
        store.create_pending(o2, _ctx("p2"), "log_cardio", {}, [])
        active = store.active_pending("telegram", "1", "42")
        self.assertNotEqual(active["pending_id"], first)  # first was superseded

    def test_resolve_is_one_use(self):
        origin = store.reserve(_ctx("p1")).action_id
        pid = store.create_pending(origin, _ctx("p1"), "log_supplement", {}, [])
        self.assertTrue(store.resolve_pending(pid, "later-action"))
        self.assertFalse(store.resolve_pending(pid, "again"))
        self.assertIsNone(store.active_pending("telegram", "1", "42"))

    def test_atomic_resolution_claim_has_one_winner(self):
        origin = store.reserve(_ctx("origin-claim")).action_id
        pid = store.create_pending(origin, _ctx("origin-claim"),
                                   "log_supplement", {}, [])
        barrier = threading.Barrier(2)

        def claim(action_id):
            barrier.wait()
            return store.claim_pending_for_resolution(pid, action_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("reply-a", "reply-b")))
        self.assertEqual(sorted(results), [False, True])

    def test_failed_claim_can_be_released(self):
        origin = store.reserve(_ctx("origin-release")).action_id
        pid = store.create_pending(origin, _ctx("origin-release"),
                                   "log_supplement", {}, [])
        self.assertTrue(store.claim_pending_for_resolution(pid, "reply-a"))
        self.assertTrue(store.release_pending_claim(pid, "reply-a"))
        self.assertIsNotNone(store.active_pending("telegram", "1", "42"))

    def test_expired_pending_not_active(self):
        origin = store.reserve(_ctx("p1")).action_id
        store.create_pending(origin, _ctx("p1"), "log_supplement", {}, [],
                             ttl_minutes=-1)
        self.assertIsNone(store.active_pending("telegram", "1", "42"))

    def test_pending_isolated_by_user(self):
        o1 = store.reserve(_ctx("p1", user_id="42")).action_id
        store.create_pending(o1, _ctx("p1", user_id="42"), "log_supplement", {}, [])
        self.assertIsNone(store.active_pending("telegram", "1", "99"))


class MigrationTests(TempDBCase):
    def test_idempotent(self):
        conn = store.connect()
        store.ensure_conversation_tables(conn)
        store.ensure_conversation_tables(conn)  # no error on repeat
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertIn("conversation_actions", tables)
        self.assertIn("pending_actions", tables)

    def test_does_not_touch_domain_tables(self):
        store.connect().close()
        conn = sqlite3.connect(self.db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        # A bare conversation migration must not create WHOOP/activity tables.
        self.assertNotIn("workout_exercises", tables)
        self.assertNotIn("recovery", tables)


if __name__ == "__main__":
    unittest.main()
