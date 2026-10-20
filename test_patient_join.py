"""
Patient join tests (a full server: the client stays open and waits, nothing is closed). Run:  python -m unittest test_patient_join -v

No Windows, Roblox or network needed. test_phase6's fake machine stands in for the PC and Roblox.
"""

from __future__ import annotations

import unittest

import autorejoin
from test_phase6 import World, make_accounts, make_manager, tick_for

JOB = "friendJOB12345"


class PatientTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.full = True                      # the target server is full until a test opens it
        self.events = []

    def manager(self, accounts):
        mgr, _ = make_manager(self.world, accounts, notify_fn=lambda *a: self.events.append(a))
        base = self.world.launch

        def launch(account, place, job, extra=None):
            base(account, place, job)
            if job == JOB and not self.full:   # Roblox lets the join through: presence shows that server
                self.world.presence[account.id]["gameId"] = JOB
        mgr.launch_fn = launch
        return mgr

    def test_a_full_server_leaves_the_client_open_and_untouched_for_as_long_as_it_takes(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, JOB, strict=True, patient=True)
        pid = mgr.clients[a.id].pid
        tick_for(mgr, self.world, 3600)                   # an hour of "server full"
        self.assertEqual(self.world.kills, [], "nothing is ever closed while it waits")
        self.assertEqual(len(self.world.launches), 1, "nothing is relaunched either")
        self.assertEqual(self.world.pids, {pid})
        self.assertEqual(mgr.clients[a.id].state, "running")
        self.assertFalse(mgr.clients[a.id].strict_ok)

    def test_not_being_in_a_game_while_queued_is_not_a_disconnect(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, JOB, strict=True, patient=True)
        self.world.presence[a.id] = {"userPresenceType": 1}      # online, waiting: not "in game"
        tick_for(mgr, self.world, 1800)
        self.assertEqual(self.world.kills, [])
        self.assertEqual(len(self.world.launches), 1)
        self.assertNotIn("disconnect", [e[0] for e in self.events])

    def test_a_log_marker_while_queued_is_ignored(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, JOB, strict=True, patient=True)
        c = mgr.clients[a.id]
        c.marker_at, c.marker_name = self.world.t - 100, "disconnected"
        self.world.presence[a.id] = {"userPresenceType": 1}
        tick_for(mgr, self.world, 200)
        self.assertEqual(self.world.kills, [])
        self.assertEqual(len(self.world.launches), 1)

    def test_it_gets_in_when_a_slot_opens_and_then_behaves_normally(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, JOB, strict=True, patient=True)
        tick_for(mgr, self.world, 600)
        self.world.presence[a.id] = {"userPresenceType": 2, "placeId": 111, "gameId": JOB}   # your turn
        tick_for(mgr, self.world, 60)
        c = mgr.clients[a.id]
        self.assertTrue(c.strict_ok)
        self.assertEqual(len(self.world.launches), 1, "the same client got in: no relaunch")
        self.assertEqual(self.world.kills, [])
        self.world.crash(a)                               # once in, a crash is an ordinary rejoin
        tick_for(mgr, self.world, 60)
        self.assertGreater(len(self.world.launches), 1)

    def test_a_client_that_exits_by_itself_is_launched_into_the_same_server_again(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, JOB, strict=True, patient=True)
        self.world.crash(a)                               # the process is gone (not closed by this app)
        tick_for(mgr, self.world, 120)
        self.assertGreater(len(self.world.launches), 1)
        self.assertTrue(all(job == JOB for _, _, job in self.world.launches), "never any other server")
        self.assertIn(a.id, mgr.clients)

    def test_repeated_exits_are_not_limited_by_the_rejoin_attempt_cap(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, JOB, strict=True, patient=True)
        for _ in range(autorejoin.MAX_ATTEMPTS + 3):
            self.world.crash(a)
            tick_for(mgr, self.world, 120)
        self.assertGreater(len(self.world.launches), autorejoin.MAX_ATTEMPTS + 1)
        self.assertNotEqual(mgr.clients[a.id].state, "gave_up")

    def test_stop_ends_the_waiting(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, JOB, strict=True, patient=True)
        tick_for(mgr, self.world, 200)
        self.assertTrue(mgr.stop_account(a))
        n = len(self.world.launches)
        tick_for(mgr, self.world, 600)
        self.assertEqual(len(self.world.launches), n)
        self.assertEqual(self.world.pids, set())
        self.assertNotIn(a.id, mgr.clients)

    def test_a_failing_relaunch_after_an_exit_keeps_waiting(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, JOB, strict=True, patient=True)
        self.world.crash(a)
        self.world.fail_next.append(RuntimeError("network blip"))
        tick_for(mgr, self.world, 300)
        self.assertIn(a.id, mgr.clients)
        self.assertNotEqual(mgr.clients[a.id].state, "gave_up")
        self.assertGreater(len(self.world.launches), 1)

    def test_not_patient_still_closes_after_the_timeout(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, JOB, strict=True)               # the Server finder's behaviour is unchanged
        tick_for(mgr, self.world, autorejoin.STRICT_JOIN_SECONDS + 15)
        self.assertEqual(len(self.world.launches), 1)
        self.assertNotIn(a.id, mgr.clients)

    def test_a_new_join_replaces_a_waiting_account_without_a_second_client(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, JOB, strict=True, patient=True)
        tick_for(mgr, self.world, 100)
        mgr.hop(a, 111, "otherJOB12345", strict=True, patient=True)
        tick_for(mgr, self.world, 300)
        self.assertEqual(len(self.world.pids), 1)
        self.assertEqual(mgr.clients[a.id].strict_job, "otherJOB12345")


if __name__ == "__main__":
    unittest.main()
