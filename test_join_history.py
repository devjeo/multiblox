"""
Join history and move-on-join tests. Run:  python -m unittest test_join_history -v

No Windows, Roblox or network needed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import join_history as jh
from test_hopper import tick_for  # noqa: F401  (shares the fake machine)
from test_phase6 import World, make_accounts, make_manager


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "join_history.json"
        self.t = 1000.0
        self.hist = jh.JoinHistory(self.path, clock=self.tick)

    def tick(self):
        self.t += 1
        return self.t

    def test_newest_first_and_survives_a_restart(self):
        self.hist.add(1, "alice", "Alice")
        self.hist.add(2, "bob")
        again = jh.JoinHistory(self.path)
        self.assertEqual([e.name for e in again.entries], ["bob", "alice"])
        self.assertEqual(again.entries[1].display_name, "Alice")

    def test_a_repeat_moves_up_and_is_not_duplicated(self):
        self.hist.add(1, "alice")
        self.hist.add(2, "bob")
        self.hist.add(1, "alice")
        self.assertEqual([e.user_id for e in self.hist.entries], [1, 2])

    def test_renamed_user_keeps_one_entry_with_the_new_name(self):
        self.hist.add(1, "alice")
        self.hist.add(1, "alice_new")
        self.assertEqual([(e.user_id, e.name) for e in self.hist.entries], [(1, "alice_new")])

    def test_trims_to_the_maximum(self):
        for n in range(1, jh.MAX_ENTRIES + 6):
            self.hist.add(n, f"user{n}")
        self.assertEqual(len(self.hist.entries), jh.MAX_ENTRIES)
        self.assertEqual(self.hist.entries[0].user_id, jh.MAX_ENTRIES + 5)
        self.assertIsNone(self.hist.get(1))

    def test_remove_and_clear(self):
        self.hist.add(1, "alice")
        self.hist.add(2, "bob")
        self.hist.remove(1)
        self.assertEqual([e.user_id for e in jh.JoinHistory(self.path).entries], [2])
        self.hist.clear()
        self.assertEqual(jh.JoinHistory(self.path).entries, [])

    def test_bad_input_is_ignored(self):
        self.hist.add(0, "x")
        self.hist.add(-5, "x")
        self.hist.add(3, "")
        self.assertEqual(self.hist.entries, [])

    def test_damaged_file_never_raises(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(jh.JoinHistory(self.path).entries, [])
        self.path.write_text(json.dumps([{"userId": "x"}, 5, {"userId": 7, "name": "ok", "lastUsed": 3},
                                         {"userId": 7, "name": "dupe"}]), encoding="utf-8")
        self.assertEqual([e.name for e in jh.JoinHistory(self.path).entries], ["ok"])

    def test_no_cookie_or_server_fields_are_written(self):
        self.hist.add(1, "alice", "Alice")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(saved[0]), {"userId", "name", "displayName", "lastUsed"})

    def test_title(self):
        self.assertEqual(jh.Entry(1, "alice", "Alice").title, "Alice (@alice)")
        self.assertEqual(jh.Entry(1, "alice", "alice").title, "alice")
        self.assertEqual(jh.Entry(1, "alice").title, "alice")


class MoveOnJoinTests(unittest.TestCase):
    """A running account is closed completely and relaunched into the exact job ID."""

    def setUp(self):
        self.world = World()

    def manager(self, accounts):
        return make_manager(self.world, accounts)[0]

    def test_running_account_is_closed_then_launched_into_the_target_server(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, "jobAAAAAAAA")
        old_pid = mgr.clients[a.id].pid
        client = mgr.hop(a, 222, "friendJOB-123", strict=True)
        self.assertNotIn(old_pid, self.world.pids)
        self.assertEqual(self.world.launches[-1], (a.id, 222, "friendJOB-123"))
        self.assertEqual(client.strict_job, "friendJOB-123")   # it must really get into THAT server

    def test_plain_hop_is_not_strict(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, "jobAAAAAAAA")
        self.assertIsNone(mgr.hop(a, 111, "jobBBBBBBBB").strict_job)


if __name__ == "__main__":
    unittest.main()
