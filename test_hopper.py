"""
Server cache and server hopper tests. Run:  python -m unittest test_hopper -v

No Windows, Roblox or network: fetch is scripted, and the RejoinManager runs on test_phase6's fake
machine (instant clock, fake process list).
"""

from __future__ import annotations

import unittest

import net
import server_finder as sf
import server_hopper as sh
from server_finder import Found, Server, ServerCache
from test_joins import Fetch, page, raw
from test_phase6 import World, make_accounts, make_manager, tick_for


def srv(n, playing, maximum=10):
    return Server(f"job-{n:08d}", playing, maximum, 50)


def jid(n):
    return f"job-{n:08d}"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------
class CacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cache = ServerCache(ttl=120, stale_ttl=900, cooldown=90, min_refresh=10, clock=self.clock)

    def test_second_call_makes_no_request(self):
        fetch = Fetch(page([raw(1, 3)]))
        a = self.cache.get(555, fetch=fetch)
        b = self.cache.get(555, fetch=fetch)       # a second outcome would be popped from an empty list
        self.assertFalse(a.cached)
        self.assertTrue(b.cached)
        self.assertEqual(len(fetch.urls), 1)
        self.assertEqual([s.job_id for s in b.servers], [jid(1)])

    def test_places_are_cached_separately(self):
        fetch = Fetch(page([raw(1, 3)]), page([raw(2, 4)]))
        self.cache.get(1, fetch=fetch)
        self.assertEqual(self.cache.get(2, fetch=fetch).servers[0].job_id, jid(2))
        self.assertEqual(len(fetch.urls), 2)

    def test_expires_after_ttl(self):
        fetch = Fetch(page([raw(1, 3)]), page([raw(2, 4)]))
        self.cache.get(555, fetch=fetch)
        self.clock.t += 121
        fresh = self.cache.get(555, fetch=fetch)
        self.assertFalse(fresh.cached)
        self.assertEqual(fresh.servers[0].job_id, jid(2))

    def test_force_is_held_back_by_min_refresh(self):
        fetch = Fetch(page([raw(1, 3)]), page([raw(2, 4)]))
        self.cache.get(555, fetch=fetch)
        self.clock.t += 5
        self.assertTrue(self.cache.get(555, force=True, fetch=fetch).cached)    # too soon: no request
        self.clock.t += 6
        self.assertFalse(self.cache.get(555, force=True, fetch=fetch).cached)   # now it refreshes
        self.assertEqual(len(fetch.urls), 2)

    def test_rate_limit_serves_stale_list_and_waits(self):
        limited = net.HttpError(429)
        fetch = Fetch(page([raw(1, 3)]), limited)
        self.cache.get(555, fetch=fetch)
        self.clock.t += 200                                   # expired, so a refresh is tried
        got = self.cache.get(555, fetch=fetch)
        self.assertTrue(got.stale)
        self.assertEqual(got.servers[0].job_id, jid(1))
        self.assertGreater(self.cache.cooldown_left(), 0)
        self.clock.t += 30
        self.assertTrue(self.cache.get(555, fetch=fetch).stale)   # still cooling down: NO new request
        self.assertEqual(len(fetch.urls), 2)

    def test_rate_limit_with_nothing_cached_raises_and_blocks_other_games(self):
        fetch = Fetch(net.HttpError(429))
        with self.assertRaises(net.HttpError):
            self.cache.get(555, fetch=fetch)
        with self.assertRaises(net.HttpError) as ctx:         # cooling down: raises without asking Roblox
            self.cache.get(777, fetch=fetch)
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(len(fetch.urls), 1)
        self.assertIn("rate-limiting", sf.describe_error(ctx.exception))

    def test_requests_resume_after_cooldown(self):
        fetch = Fetch(net.HttpError(429), page([raw(1, 3)]))
        with self.assertRaises(net.HttpError):
            self.cache.get(555, fetch=fetch)
        self.clock.t += 91
        self.assertEqual(self.cache.get(555, fetch=fetch).servers[0].job_id, jid(1))

    def test_other_errors_fall_back_to_stale_but_do_not_start_a_cooldown(self):
        fetch = Fetch(page([raw(1, 3)]), net.HttpError(500))
        self.cache.get(555, fetch=fetch)
        self.clock.t += 200
        self.assertTrue(self.cache.get(555, fetch=fetch).stale)
        self.assertEqual(self.cache.cooldown_left(), 0)

    def test_too_old_even_for_stale_raises(self):
        fetch = Fetch(page([raw(1, 3)]), net.HttpError(500))
        self.cache.get(555, fetch=fetch)
        self.clock.t += 2000
        with self.assertRaises(net.HttpError):
            self.cache.get(555, fetch=fetch)

    def test_invalidate(self):
        fetch = Fetch(page([raw(1, 3)]), page([raw(2, 3)]))
        self.cache.get(555, fetch=fetch)
        self.cache.invalidate(555)
        self.assertFalse(self.cache.get(555, fetch=fetch).cached)

    def test_partial_list_is_not_trusted_for_the_full_ttl(self):
        fetch = Fetch(page([raw(1, 3)], cursor="next"), net.HttpError(500), page([raw(2, 3)]))
        first = self.cache.get(555, fetch=fetch)
        self.assertTrue(first.partial)
        self.clock.t += 40                                    # 25% of 120s = 30s
        self.assertFalse(self.cache.get(555, fetch=fetch).cached)

    def test_bad_place_id(self):
        for bad in (0, -1, True, "5"):
            with self.assertRaises(ValueError):
                sf.find_servers_cached(bad, use=self.cache, fetch=Fetch())

    def test_ttl_zero_disables_reuse(self):
        self.cache.ttl = 0
        fetch = Fetch(page([raw(1, 3)]), page([raw(2, 3)]))
        self.cache.get(555, fetch=fetch)
        self.clock.t += 1
        self.assertFalse(self.cache.get(555, fetch=fetch).cached)


# --------------------------------------------------------------------------
# RejoinManager.hop
# --------------------------------------------------------------------------
class ManagerHopTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.events = []
        world = self.world
        real_launch, real_kill = world.launch, world.kill

        def launch(account, place_id, job_id):
            alive = [p for p, o in world.owner.items() if o == account.id and p in world.pids]
            self.events.append(("launch", len(alive)))     # how many clients this account had at launch time
            real_launch(account, place_id, job_id)

        def kill(pid):
            self.events.append(("kill", pid))
            real_kill(pid)
        world.launch, world.kill = launch, kill

    def manager(self, accounts):
        return make_manager(self.world, accounts)[0]

    def test_old_client_is_gone_before_the_new_one_starts(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, "jobAAAAAAAA")
        old_pid = mgr.clients[a.id].pid
        client = mgr.hop(a, 111, "jobBBBBBBBB")
        self.assertEqual(self.events[-2:], [("kill", old_pid), ("launch", 0)])   # zero clients at launch
        self.assertNotEqual(client.pid, old_pid)
        self.assertNotIn(old_pid, self.world.pids)
        self.assertIs(mgr.clients[a.id], client)
        self.assertEqual(self.world.launches[-1], (a.id, 111, "jobBBBBBBBB"))

    def test_old_client_is_not_rejoined_after_a_hop(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, "jobAAAAAAAA")
        mgr.hop(a, 111, "jobBBBBBBBB")
        n = len(self.world.launches)
        tick_for(mgr, self.world, 120)
        self.assertEqual(len(self.world.launches), n)          # no spurious "disconnect" relaunch

    def test_hop_launches_when_nothing_is_running(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        client = mgr.hop(a, 111, "jobBBBBBBBB")
        self.assertEqual(client.state, "running")

    def test_old_client_that_will_not_close_blocks_the_launch(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, "jobAAAAAAAA")
        old = mgr.clients[a.id]
        self.world.kill = lambda pid: None                      # taskkill refused
        mgr.kill_fn = self.world.kill
        launches = len(self.world.launches)
        with self.assertRaises(RuntimeError):
            mgr.hop(a, 111, "jobBBBBBBBB", close_timeout=3)
        self.assertEqual(len(self.world.launches), launches)    # never a second client
        self.assertIs(mgr.clients[a.id], old)
        self.assertFalse(old.cancelled)                         # it is watched again

    def test_failed_launch_hands_the_account_to_auto_rejoin(self):
        [a] = accounts = make_accounts(1, rejoin=True)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, "jobAAAAAAAA")
        self.world.fail_next.append(RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            mgr.hop(a, 111, "jobBBBBBBBB")
        c = mgr.clients[a.id]
        self.assertEqual(c.state, "waiting_retry")
        tick_for(mgr, self.world, 60)                           # the retry timer fires
        self.assertEqual(mgr.clients[a.id].state, "running")

    def test_failed_launch_without_auto_rejoin_stays_closed(self):
        [a] = accounts = make_accounts(1, rejoin=False)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, "jobAAAAAAAA")
        self.world.fail_next.append(RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            mgr.hop(a, 111, "jobBBBBBBBB")
        self.assertEqual(mgr.clients[a.id].state, "closed")
        launches = len(self.world.launches)
        tick_for(mgr, self.world, 120)
        self.assertEqual(len(self.world.launches), launches)


# --------------------------------------------------------------------------
# ServerHopper
# --------------------------------------------------------------------------
class HopperTests(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.accounts = make_accounts(2)
        self.mgr = make_manager(self.world, self.accounts)[0]
        self.servers = [srv(1, 9), srv(2, 8), srv(3, 5), srv(4, 1)]
        self.finds = []
        self.logs = []
        self.hopper = sh.ServerHopper(self.mgr, log_fn=self.logs.append, find_fn=self.find,
                                      clock=self.world.clock, sleep=self.world.sleep)

    def find(self, place_id, force=False):
        self.finds.append((place_id, force))
        return Found(list(self.servers), 1, cached=not force)

    def hop_all(self):
        self.world.t += 10_000
        self.hopper.tick()

    def test_hops_to_a_different_server_and_closes_the_old_game(self):
        a = self.accounts[0]
        self.mgr.launch(a, 111, jid(1))
        old_pid = self.mgr.clients[a.id].pid
        self.hopper.enable(a, interval_minutes=30)
        self.hop_all()
        self.assertNotIn(old_pid, self.world.pids)
        self.assertEqual(len(self.world.pids), 1)
        self.assertEqual(self.world.launches[-1][2], jid(2))      # best = fullest with room, not the current one
        self.assertEqual(self.hopper.snapshot()[0]["hops"], 1)

    def test_never_returns_to_a_server_it_already_used(self):
        a = self.accounts[0]
        self.mgr.launch(a, 111, jid(1))
        self.hopper.enable(a, interval_minutes=30)
        for _ in range(3):
            self.hop_all()
        self.assertEqual([l[2] for l in self.world.launches[1:]], [jid(2), jid(3), jid(4)])

    def test_two_accounts_are_spread_over_different_servers(self):
        for a in self.accounts:
            self.mgr.launch(a, 111, jid(1))
            self.hopper.enable(a, interval_minutes=30)
        self.hop_all()
        jobs = [l[2] for l in self.world.launches[2:]]
        self.assertEqual(len(set(jobs)), 2)

    def test_rotation_restarts_when_every_server_has_been_used(self):
        a = self.accounts[0]
        self.servers = [srv(1, 5), srv(2, 4)]
        self.mgr.launch(a, 111, jid(1))
        self.hopper.enable(a, interval_minutes=30)
        for _ in range(4):
            self.hop_all()
        jobs = [l[2] for l in self.world.launches[1:]]
        self.assertEqual(len(jobs), 4)
        self.assertTrue(all(x != y for x, y in zip([jid(1)] + jobs, jobs)))   # never the same server twice in a row

    def test_stays_put_when_there_is_no_other_server(self):
        a = self.accounts[0]
        self.servers = [srv(1, 5)]
        self.mgr.launch(a, 111, jid(1))
        pid = self.mgr.clients[a.id].pid
        self.hopper.enable(a, interval_minutes=30)
        self.hop_all()
        self.assertIn(pid, self.world.pids)                       # NOT closed
        self.assertEqual(len(self.world.launches), 1)
        self.assertIn("staying", self.hopper.snapshot()[0]["last"])

    def test_servers_with_one_seat_left_are_skipped(self):
        a = self.accounts[0]
        self.servers = [srv(1, 5), srv(2, 9)]                     # 9/10: one seat, the list is old
        self.mgr.launch(a, 111, jid(1))
        self.hopper.enable(a, interval_minutes=30)
        self.hop_all()
        self.assertEqual(len(self.world.launches), 1)

    def test_a_used_up_cached_list_triggers_one_forced_refresh(self):
        a = self.accounts[0]
        self.servers = [srv(1, 5)]
        self.mgr.launch(a, 111, jid(1))
        self.hopper.enable(a, interval_minutes=30)
        self.hop_all()
        self.assertEqual(self.finds, [(111, False), (111, True)])

    def test_stopping_the_account_ends_hopping(self):
        a = self.accounts[0]
        self.mgr.launch(a, 111, jid(1))
        self.hopper.enable(a, interval_minutes=30)
        self.mgr.stop_account(a)
        self.hop_all()
        self.assertFalse(self.hopper.is_enabled(a))
        self.assertEqual(len(self.world.launches), 1)             # not brought back

    def test_private_server_accounts_are_skipped(self):
        a = self.accounts[0]
        self.mgr.launch_fn = lambda acc, place, job, extra=None: self.world.launch(acc, place, job)
        self.mgr.launch(a, 111, None, extra={"accessCode": "x"})
        self.hopper.enable(a, interval_minutes=30)
        self.hop_all()
        self.assertFalse(self.hopper.is_enabled(a))
        self.assertEqual(len(self.world.launches), 1)

    def test_waits_while_the_client_is_not_running(self):
        a = self.accounts[0]
        self.mgr.launch(a, 111, jid(1))
        self.mgr.clients[a.id].state = "rejoining"
        self.hopper.enable(a, interval_minutes=30)
        self.hop_all()
        self.assertEqual(len(self.world.launches), 1)
        self.assertTrue(self.hopper.is_enabled(a))

    def test_waits_while_the_app_is_launching_it(self):
        a = self.accounts[0]
        self.mgr.launch(a, 111, jid(1))
        self.hopper.busy_fn = lambda acct: True
        self.hopper.enable(a, interval_minutes=30)
        self.hop_all()
        self.assertEqual(len(self.world.launches), 1)

    def test_not_running_account_is_launched_right_away(self):
        a = self.accounts[0]
        self.hopper.enable(a, interval_minutes=30, place_id=111)
        self.world.t += 1
        self.hopper.tick()
        self.assertEqual(self.world.launches, [(a.id, 111, jid(2))])
        self.assertEqual(self.mgr.clients[a.id].state, "running")

    def test_one_shot_removes_itself(self):
        a = self.accounts[0]
        self.mgr.launch(a, 111, jid(1))
        self.hopper.enable(a, interval_minutes=30, first_in=0, repeat=False)
        self.hopper.tick()
        self.assertFalse(self.hopper.is_enabled(a))
        self.assertEqual(len(self.world.launches), 2)

    def test_hop_now(self):
        a = self.accounts[0]
        self.mgr.launch(a, 111, jid(1))
        self.hopper.enable(a, interval_minutes=30)
        self.hopper.tick()
        self.assertEqual(len(self.world.launches), 1)             # not due yet
        self.hopper.hop_now(a)
        self.hopper.tick()
        self.assertEqual(len(self.world.launches), 2)

    def test_three_failures_in_a_row_switch_hopping_off(self):
        a = self.accounts[0]
        self.mgr.launch(a, 111, jid(1))
        self.hopper.enable(a, interval_minutes=30)
        for _ in range(sh.MAX_FAILURES):
            self.world.fail_next.append(RuntimeError("boom"))
            self.hop_all()
            self.mgr.clients[a.id].state = "running"              # pretend auto-rejoin recovered
        self.assertFalse(self.hopper.is_enabled(a))

    def test_interval_is_clamped_and_mode_checked(self):
        a = self.accounts[0]
        plan = self.hopper.enable(a, interval_minutes=0.1)
        self.assertEqual(plan.interval, sh.MIN_INTERVAL_MINUTES * 60)
        with self.assertRaises(ValueError):
            self.hopper.enable(a, interval_minutes=10, mode="random")

    def test_mode_is_respected(self):
        a = self.accounts[0]
        self.mgr.launch(a, 111, jid(1))
        self.hopper.enable(a, interval_minutes=30, mode="emptiest")
        self.hop_all()
        self.assertEqual(self.world.launches[-1][2], jid(4))

    # ---- history -------------------------------------------------------
    def test_every_hop_is_in_the_history_newest_first(self):
        a = self.accounts[0]
        self.mgr.launch(a, 111, jid(1))
        self.hopper.enable(a, interval_minutes=30)
        self.hop_all()
        self.hop_all()
        hist = self.hopper.history(111)
        self.assertEqual([h["job_id"] for h in hist], [jid(3), jid(2), jid(1)])
        self.assertEqual(hist[0]["who"], a.username)
        self.assertTrue(self.hopper.was_visited(111, jid(1)))      # the server it started in counts too
        self.assertFalse(self.hopper.was_visited(111, jid(4)))

    def test_a_server_joined_from_the_finder_is_not_hopped_into(self):
        a = self.accounts[0]
        self.mgr.launch(a, 111, jid(1))
        self.hopper.record_visit(111, jid(2), "someone")            # e.g. clicked in the Server finder
        self.hopper.enable(a, interval_minutes=30)
        self.hop_all()
        self.assertEqual(self.world.launches[-1][2], jid(3))        # jid(2) is the best, but it was visited

    def test_history_is_per_game_and_can_be_cleared(self):
        self.hopper.record_visit(111, jid(1), "a")
        self.hopper.record_visit(222, jid(1), "b")
        self.assertEqual(len(self.hopper.history(111)), 1)
        self.assertEqual(len(self.hopper.history()), 2)
        self.hopper.clear_history(111)
        self.assertEqual([h["place_id"] for h in self.hopper.history()], [222])
        self.hopper.clear_history()
        self.assertEqual(self.hopper.history(), [])

    def test_history_is_capped(self):
        for n in range(sh.HISTORY_LIMIT + 25):
            self.hopper.record_visit(111, jid(n), "a")
        hist = self.hopper.history(111)
        self.assertEqual(len(hist), sh.HISTORY_LIMIT)
        self.assertEqual(hist[0]["job_id"], jid(sh.HISTORY_LIMIT + 24))   # the oldest were dropped

    def test_revisit_moves_to_the_newest_position(self):
        self.hopper.record_visit(111, jid(1), "a")
        self.hopper.record_visit(111, jid(2), "a")
        self.hopper.record_visit(111, jid(1), "b")
        self.assertEqual([h["job_id"] for h in self.hopper.history(111)], [jid(1), jid(2)])

    def test_used_up_list_frees_old_servers_but_keeps_the_history_visible(self):
        a = self.accounts[0]
        self.servers = [srv(1, 5), srv(2, 4), srv(3, 3), srv(4, 2)]
        self.mgr.launch(a, 111, jid(1))
        self.hopper.enable(a, interval_minutes=30)
        for _ in range(6):
            self.hop_all()
        jobs = [l[2] for l in self.world.launches[1:]]
        self.assertEqual(len(jobs), 6)                              # it keeps going after every server was visited
        # half the list (2 servers) stays blocked, so it never bounces straight back
        for prev, nxt in zip([jid(1)] + jobs, jobs):
            self.assertNotEqual(prev, nxt)
        for before, after in zip(jobs, jobs[2:]):
            self.assertNotEqual(before, after)
        self.assertGreaterEqual(len(self.hopper.history(111)), 4)   # nothing was erased from view

    def test_snapshot_says_whether_a_plan_repeats(self):
        a, b = self.accounts
        self.hopper.enable(a, interval_minutes=30)
        self.hopper.enable(b, interval_minutes=30, repeat=False)
        flags = {r["username"]: r["repeat"] for r in self.hopper.snapshot()}
        self.assertEqual(flags, {a.username: True, b.username: False})


if __name__ == "__main__":
    unittest.main()
