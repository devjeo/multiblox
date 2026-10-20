"""
Server history tests. Run:  python -m unittest test_server_history -v

No Windows, Roblox or network needed. The manager runs on test_phase6's fake machine.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import autorejoin
import server_history as sh
from test_phase6 import World, make_accounts, make_manager, tick_for

JOB1, JOB2 = "aaaaaaaa-1111", "bbbbbbbb-2222"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "server_history.json"
        self.t = 100.0
        self.h = sh.ServerHistory(self.path, clock=self.tick)

    def tick(self):
        self.t += 1
        return self.t

    def test_games_are_mixed_newest_first_and_survive_a_restart(self):
        self.h.add(111, JOB1, "Game A", "alice")
        self.h.add(222, JOB2, "Game B", "bob")
        again = sh.ServerHistory(self.path)
        self.assertEqual([(r.game, r.job_id) for r in again.list()], [("Game B", JOB2), ("Game A", JOB1)])

    def test_accounts_in_one_server_share_one_row(self):
        self.h.add(111, JOB1, "Game A", "alice")
        self.h.add(111, JOB1, "Game A", "bob")
        self.h.add(111, JOB1, "Game A", "alice")
        [row] = self.h.list()
        self.assertEqual(row.accounts, ["alice", "bob"])
        self.assertLess(row.first_at, row.last_at)

    def test_a_revisit_moves_to_the_top(self):
        self.h.add(111, JOB1, "A", "alice")
        self.h.add(111, JOB2, "A", "alice")
        self.h.add(111, JOB1, "A", "alice")
        self.assertEqual([r.job_id for r in self.h.list()], [JOB1, JOB2])

    def test_same_job_in_two_places_is_two_rows(self):
        self.h.add(111, JOB1, "A", "alice")
        self.h.add(222, JOB1, "B", "alice")
        self.assertEqual(len(self.h.list()), 2)

    def test_later_name_replaces_an_empty_one(self):
        self.h.add(111, JOB1, "", "alice")
        self.h.add(111, JOB1, "Game A", "alice")
        self.assertEqual(self.h.list()[0].game, "Game A")

    def test_trims_to_the_maximum(self):
        for n in range(sh.MAX_ROWS + 5):
            self.h.add(111, f"job-{n:08d}", "A", "alice")
        self.assertEqual(len(self.h.list()), sh.MAX_ROWS)
        self.assertIsNone(self.h.get(111, "job-00000000"))

    def test_remove_and_clear(self):
        self.h.add(111, JOB1, "A", "alice")
        self.h.add(111, JOB2, "A", "alice")
        self.h.remove(111, JOB1)
        self.assertEqual([r.job_id for r in sh.ServerHistory(self.path).list()], [JOB2])
        self.h.clear()
        self.assertEqual(sh.ServerHistory(self.path).list(), [])

    def test_clear_skips_servers_an_account_is_in_right_now(self):
        self.h.add(111, JOB1, "A", "alice")
        self.h.add(111, JOB2, "A", "bob")
        self.h.add(222, "cccccccc-3333", "B", "bob")
        removed = self.h.clear(keep={(111, JOB1)})
        self.assertEqual(removed, 2)
        self.assertEqual([r.job_id for r in sh.ServerHistory(self.path).list()], [JOB1])   # also on disk
        self.assertEqual(self.h.clear(keep={(111, JOB1)}), 0)                               # nothing left to clear
        self.assertEqual(self.h.clear(), 1)                                                 # nobody online: all go
        self.assertEqual(self.h.list(), [])

    def test_remove_skips_a_server_an_account_is_in_right_now(self):
        self.h.add(111, JOB1, "A", "alice")
        self.assertFalse(self.h.remove(111, JOB1, keep={(111, JOB1)}))
        self.assertEqual(len(self.h.list()), 1)
        self.assertTrue(self.h.remove(111, JOB1))
        self.assertEqual(self.h.list(), [])

    def test_bad_input_and_damaged_files_are_safe(self):
        self.assertFalse(self.h.add(0, JOB1, "A", "x"))
        self.assertFalse(self.h.add(111, "short", "A", "x"))
        self.path.write_text("not json", encoding="utf-8")
        self.assertEqual(sh.ServerHistory(self.path).list(), [])
        self.path.write_text(json.dumps([{"placeId": 1, "jobId": "x"}, 3, {"placeId": 5, "jobId": JOB1, "game": "G"}]),
                             encoding="utf-8")
        self.assertEqual([r.game for r in sh.ServerHistory(self.path).list()], ["G"])

    def test_no_cookie_fields_are_written(self):
        self.h.add(111, JOB1, "A", "alice")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(saved[0]), {"placeId", "jobId", "game", "accounts", "firstAt", "lastAt"})


class RecordingTests(unittest.TestCase):
    """RejoinManager reports a server only once presence confirms the client is in it."""

    def setUp(self):
        self.world = World()
        self.seen = []

    def manager(self, accounts):
        return make_manager(self.world, accounts, on_server_fn=lambda *a: self.seen.append(a))[0]

    def test_random_join_is_reported_once_with_the_real_server(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)                                   # no job ID asked for
        self.world.presence[a.id] = {"userPresenceType": 2, "placeId": 111, "gameId": "real-job-1234",
                                     "lastLocation": "Game A"}
        tick_for(mgr, self.world, 200)                             # several presence polls
        self.assertEqual(self.seen, [(a, 111, "real-job-1234", "Game A")])

    def test_a_teleport_reports_the_new_server_too(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.world.presence[a.id] = {"userPresenceType": 2, "placeId": 111, "gameId": "job-one-1234"}
        tick_for(mgr, self.world, 100)
        self.world.presence[a.id] = {"userPresenceType": 2, "placeId": 222, "gameId": "job-two-1234",
                                     "lastLocation": "Game B"}
        tick_for(mgr, self.world, 100)
        self.assertEqual([(s[1], s[2]) for s in self.seen], [(111, "job-one-1234"), (222, "job-two-1234")])

    # ---- live: where is each account RIGHT NOW (RejoinManager.live_servers) ----
    def test_live_servers_lists_only_confirmed_running_accounts(self):
        a, b = accounts = make_accounts(2)
        mgr = self.manager(accounts)
        self.assertEqual(mgr.live_servers(), {})
        mgr.launch(a, 111, JOB1)
        self.assertEqual(mgr.live_servers(), {})                   # launched but presence has not confirmed yet
        self.world.presence[a.id] = {"userPresenceType": 2, "placeId": 111, "gameId": JOB1, "lastLocation": "Game A"}
        mgr.launch(b, 111, JOB1)
        self.world.presence[b.id] = {"userPresenceType": 2, "placeId": 111, "gameId": JOB1}
        tick_for(mgr, self.world, 200)
        self.assertEqual(sorted(mgr.live_servers()[(111, JOB1)]["names"]), ["user1", "user2"])

    def test_live_servers_follows_a_teleport_and_forgets_a_stopped_account(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.world.presence[a.id] = {"userPresenceType": 2, "placeId": 111, "gameId": JOB1}
        tick_for(mgr, self.world, 100)
        self.assertEqual(list(mgr.live_servers()), [(111, JOB1)])
        self.world.presence[a.id] = {"userPresenceType": 2, "placeId": 222, "gameId": JOB2}
        tick_for(mgr, self.world, 100)
        self.assertEqual(list(mgr.live_servers()), [(222, JOB2)])  # moved, not in both
        mgr.stop_account(a)
        self.assertEqual(mgr.live_servers(), {})

    def test_a_new_join_is_confirmed_within_seconds_not_a_poll_period(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.world.presence[a.id] = {"userPresenceType": 2, "placeId": 111, "gameId": JOB1}
        tick_for(mgr, self.world, autorejoin.LOCATION_START_SECONDS + autorejoin.LOCATION_FAST_SECONDS + 2)
        self.assertLess(autorejoin.LOCATION_START_SECONDS + autorejoin.LOCATION_FAST_SECONDS + 2,
                        autorejoin.PRESENCE_POLL_SECONDS)
        self.assertEqual(list(mgr.live_servers()), [(111, JOB1)])
        self.assertEqual([(s[1], s[2]) for s in self.seen], [(111, JOB1)])

    def test_leaving_the_game_drops_it_from_live_without_a_rejoin(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.world.presence[a.id] = {"userPresenceType": 2, "placeId": 111, "gameId": JOB1}
        tick_for(mgr, self.world, 30)
        self.assertEqual(len(mgr.live_servers()), 1)
        self.world.presence[a.id] = {"userPresenceType": 1}          # left the game, client still open
        tick_for(mgr, self.world, autorejoin.LOCATION_LIVE_SECONDS + 2)
        self.assertEqual(mgr.live_servers(), {})
        self.assertEqual(len(self.world.launches), 1, "an extra location check must never cause a rejoin")

    def test_the_extra_checks_do_not_speed_up_disconnect_detection(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.world.presence[a.id] = {"userPresenceType": 1}
        tick_for(mgr, self.world, autorejoin.PRESENCE_POLL_SECONDS + 5)      # only ONE regular poll so far
        self.assertEqual(len(self.world.launches), 1)

    def test_an_old_server_seen_early_in_a_new_join_is_not_taken_as_the_new_one(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, JOB2, strict=False)
        self.world.presence[a.id] = {"userPresenceType": 2, "placeId": 111, "gameId": JOB1}   # stale: the old server
        tick_for(mgr, self.world, 30)        # extra checks only (the first regular poll is at 45 s)
        self.assertEqual(mgr.live_servers(), {})
        self.assertEqual(self.seen, [])

    def test_not_in_a_game_reports_nothing(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.world.presence[a.id] = {"userPresenceType": 1}
        tick_for(mgr, self.world, 100)
        self.assertEqual(self.seen, [])

    def test_a_failing_callback_never_breaks_the_manager(self):
        [a] = accounts = make_accounts(1)
        mgr = make_manager(self.world, accounts, on_server_fn=lambda *x: 1 / 0)[0]
        mgr.launch(a, 111, None)
        self.world.presence[a.id] = {"userPresenceType": 2, "placeId": 111, "gameId": "real-job-1234"}
        tick_for(mgr, self.world, 100)
        self.assertEqual(mgr.clients[a.id].state, "running")


if __name__ == "__main__":
    unittest.main()
