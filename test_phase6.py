"""
Phase 6 tests. Run:  python -m unittest test_phase6 -v

Everything here runs WITHOUT Roblox and without Windows: the RejoinManager takes
its clock, sleep, process list, launcher and presence lookup as parameters, so a
fake "world" stands in for the real machine and time moves instantly.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import autorejoin
from account_store import Account, CookieError, clean_cookie
from autorejoin import MAX_ATTEMPTS, RejoinManager
from launcher import LaunchError, build_launch_uri, request_auth_ticket
from places import PlaceStore, parse_place_input


# --------------------------------------------------------------------------
# Fake machine
# --------------------------------------------------------------------------
class FakeStore:
    def __init__(self, accounts):
        self.accounts = accounts
        self.saves = 0

    def save(self):
        self.saves += 1

    def get_cookie(self, account):
        return "fake-cookie"


class World:
    """A pretend PC: a clock, a set of running client PIDs, and fake Roblox presence."""

    def __init__(self):
        self.t = 1000.0
        self.pids: set[int] = set()
        self.next_pid = 5000
        self.owner: dict[int, str] = {}          # pid -> account id
        self.created: dict[int, datetime] = {}   # pid -> creation time
        self.launches: list[tuple[str, int, str | None]] = []
        self.kills: list[int] = []
        self.presence: dict[str, dict | None] = {}  # account id -> presence dict
        self.on_sleep = None
        self.fail_next: list[Exception] = []

    def clock(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        if self.on_sleep:
            self.on_sleep()

    def list_pids(self):
        return set(self.pids)

    def kill(self, pid):
        self.kills.append(pid)
        self.pids.discard(pid)

    def start_client(self, account):
        pid = self.next_pid
        self.next_pid += 1
        self.pids.add(pid)
        self.owner[pid] = account.id
        self.created[pid] = datetime.now(timezone.utc)
        self.presence[account.id] = {"userPresenceType": 2, "placeId": 111, "gameId": "job-" + str(pid)}
        return pid

    def launch(self, account, place_id, job_id):
        if self.fail_next:
            raise self.fail_next.pop(0)
        self.launches.append((account.id, place_id, job_id))
        self.start_client(account)

    def crash(self, account):
        for pid, owner in list(self.owner.items()):
            if owner == account.id:
                self.pids.discard(pid)


def make_accounts(n, rejoin=True):
    return [Account(id=f"acc{i}", username=f"user{i}", userId=100 + i, encryptedCookie="x",
                    autoRejoinEnabled=rejoin) for i in range(1, n + 1)]


def make_manager(world, accounts, **kw):
    store = FakeStore(accounts)
    mgr = RejoinManager(
        store=store, launch_fn=world.launch, list_pids_fn=world.list_pids, kill_fn=world.kill,
        log_fn=lambda m: None, log_dir=Path(tempfile.gettempdir()) / "nope-no-logs",
        creation_fn=lambda pid: world.created.get(pid),
        presence_fn=lambda acc: world.presence.get(acc.id),
        clock=world.clock, sleep=world.sleep, spawn=lambda fn, *a: fn(*a), **kw)
    return mgr, store


def tick_for(mgr, world, seconds, step=1.0):
    end = world.t + seconds
    while world.t < end:
        world.t += step
        mgr._tick()


# --------------------------------------------------------------------------
# Rejoin behaviour
# --------------------------------------------------------------------------
class RejoinTests(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def test_crash_rejoins_same_server_first(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, "jobAAAAAAAA")
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(len(self.world.launches), 2)
        self.assertEqual(self.world.launches[1], (a.id, 111, "jobAAAAAAAA"))
        self.assertEqual(mgr.clients[a.id].state, "running")

    def test_second_attempt_accepts_any_server(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, "jobAAAAAAAA")
        self.world.crash(a)
        tick_for(mgr, self.world, 3)      # attempt 1 (same server)
        self.world.crash(a)
        tick_for(mgr, self.world, 3)      # attempt 2
        self.assertEqual(self.world.launches[2][2], None)

    def test_gives_up_after_max_attempts_then_resume(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, None)
        for _ in range(MAX_ATTEMPTS):
            self.world.crash(a)
            tick_for(mgr, self.world, 3)
        self.assertEqual(len(self.world.launches), 1 + MAX_ATTEMPTS)
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(mgr.clients[a.id].state, "gave_up")
        self.assertEqual(len(self.world.launches), 1 + MAX_ATTEMPTS)  # no extra launch
        self.assertTrue(mgr.resume(a))
        self.assertEqual(mgr.clients[a.id].state, "running")

    def test_attempt_window_forgets_old_attempts(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, None)
        for _ in range(MAX_ATTEMPTS):
            self.world.crash(a)
            tick_for(mgr, self.world, 3)
        self.world.t += autorejoin.ATTEMPT_WINDOW_SECONDS + 5
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(mgr.clients[a.id].state, "running")

    def test_rejoin_off_leaves_client_closed(self):
        [a] = accounts = make_accounts(1, rejoin=False)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, None)
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(mgr.clients[a.id].state, "closed")
        self.assertEqual(len(self.world.launches), 1)

    def test_fatal_launch_error_gives_up_immediately(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, None)
        self.world.fail_next.append(LaunchError("cookie rejected", fatal=True))
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(mgr.clients[a.id].state, "gave_up")

    def test_transient_launch_error_retries_later(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, None)
        self.world.fail_next.append(LaunchError("rate limited"))
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(mgr.clients[a.id].state, "waiting_retry")
        tick_for(mgr, self.world, autorejoin.RETRY_DELAY_SECONDS + 3)
        self.assertEqual(mgr.clients[a.id].state, "running")

    def test_six_accounts_crash_together(self):
        accounts = make_accounts(6)
        mgr, _ = make_manager(self.world, accounts)
        for a in accounts:
            mgr.launch(a, 111, None)
        for a in accounts:
            self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(len(self.world.launches), 12)
        pids = [c.pid for c in mgr.clients.values()]
        self.assertEqual(len(set(pids)), 6, "every account must track its own, distinct client")
        for a in accounts:  # and each PID really belongs to that account
            self.assertEqual(self.world.owner[mgr.clients[a.id].pid], a.id)

    def test_presence_not_in_game_triggers_rejoin_after_two_misses(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, None)
        self.world.presence[a.id] = {"userPresenceType": 1}  # online, not in a game (client still open)
        tick_for(mgr, self.world, autorejoin.PRESENCE_POLL_SECONDS * 3)
        self.assertGreaterEqual(len(self.world.launches), 2)

    def test_unknown_presence_is_never_a_miss(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, None)
        self.world.presence[a.id] = None  # network down / rate limited
        tick_for(mgr, self.world, autorejoin.PRESENCE_POLL_SECONDS * 5)
        self.assertEqual(len(self.world.launches), 1)

    def test_presence_follows_teleports(self):
        [a] = accounts = make_accounts(1)
        mgr, store = make_manager(self.world, accounts)
        mgr.launch(a, 111, None)
        pid = mgr.clients[a.id].pid
        self.world.presence[a.id] = {"userPresenceType": 2, "placeId": 222, "gameId": "newjob-123456"}
        tick_for(mgr, self.world, autorejoin.PRESENCE_POLL_SECONDS + 2)
        self.assertEqual((a.savedPlaceId, a.savedJobId), (222, "newjob-123456"))
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(self.world.launches[-1], (a.id, 222, "newjob-123456"))
        self.assertNotEqual(mgr.clients[a.id].pid, pid)

    def test_one_broken_client_does_not_starve_the_others(self):
        accounts = make_accounts(3)
        mgr, _ = make_manager(self.world, accounts)
        for a in accounts:
            mgr.launch(a, 111, None)
        real_presence = mgr.presence_fn

        def flaky(acc):
            if acc.id == "acc1":
                raise RuntimeError("boom")      # e.g. an unexpected error while reading one account
            return real_presence(acc)
        mgr.presence_fn = flaky
        mgr.creation_fn = lambda pid: (_ for _ in ()).throw(RuntimeError("no creation time"))
        self.world.crash(accounts[2])           # acc3 dies AFTER the broken acc1 in iteration order
        tick_for(mgr, self.world, 3)
        self.assertEqual(len(self.world.launches), 4, "acc3 must still be rejoined")

    # ---- stop / cancel (these two exposed real bugs) -----------------
    def test_stop_during_rejoin_cooldown_cancels_the_relaunch(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, None)
        self.world.crash(a)
        self.world.on_sleep = lambda: mgr.stop_account(a)   # user clicks Stop mid-cooldown
        tick_for(mgr, self.world, 3)
        self.world.on_sleep = None
        self.assertEqual(len(self.world.launches), 1, "a stopped account must not be relaunched")
        self.assertNotIn(a.id, mgr.clients)

    def test_stop_while_client_is_starting_kills_the_new_client(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, None)
        old = mgr.clients[a.id]
        self.world.crash(a)
        orig_launch = self.world.launch

        def launch_then_stop(account, place, job):
            orig_launch(account, place, job)
            mgr.stop_account(account)                       # Stop lands right after the launch
        mgr.launch_fn = launch_then_stop
        tick_for(mgr, self.world, 3)
        self.assertTrue(old.cancelled)
        self.assertEqual(self.world.pids, set(), "no orphan client may be left running")

    def test_relaunching_an_account_cancels_its_old_tracker(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, None)
        old = mgr.clients[a.id]
        self.world.crash(a)
        mgr.launch(a, 111, None)
        self.assertTrue(old.cancelled)

    # ---- teleports: Roblox starts a NEW client process (child of the old one) ----
    def _family_manager(self, accounts):
        parents = {}   # pid -> parent pid; anything not listed was started by the app (parent 999)
        mgr, _ = make_manager(self.world, accounts,
                              family_fn=lambda: {p: parents.get(p, 999) for p in self.world.pids})
        return mgr, parents

    def _teleport(self, parents, old, keep_old=True):
        new = self.world.next_pid
        self.world.next_pid += 1
        self.world.pids.add(new)
        parents[new] = old
        if not keep_old:
            self.world.pids.discard(old)
        return new

    def test_teleport_is_followed_without_a_spurious_rejoin(self):
        [a] = accounts = make_accounts(1)
        mgr, parents = self._family_manager(accounts)
        mgr.launch(a, 111, None)
        old = mgr.clients[a.id].pid
        new = self._teleport(parents, old, keep_old=False)   # old closes, new carries on
        tick_for(mgr, self.world, 3)
        self.assertEqual(len(self.world.launches), 1, "a teleport is not a disconnect")
        self.assertEqual(mgr.clients[a.id].pid, new)
        self.assertEqual(mgr.clients[a.id].state, "running")

    def test_stop_closes_the_teleport_successor_too(self):
        [a] = accounts = make_accounts(1)
        mgr, parents = self._family_manager(accounts)
        mgr.launch(a, 111, None)
        old = mgr.clients[a.id].pid
        new = self._teleport(parents, old)                   # Stop lands while both exist
        grand = self._teleport(parents, new)
        self.assertTrue(mgr.stop_account(a))
        self.assertEqual(self.world.pids, set(), "no successor may be left running")
        self.assertEqual(set(self.world.kills), {old, new, grand})

    def test_stop_finds_a_successor_whose_parent_already_exited(self):
        [a] = accounts = make_accounts(1)
        mgr, parents = self._family_manager(accounts)
        mgr.launch(a, 111, None)
        old = mgr.clients[a.id].pid
        self._teleport(parents, old, keep_old=False)         # tracker has not ticked yet
        self.assertTrue(mgr.stop_account(a))
        self.assertEqual(self.world.pids, set())

    def test_other_accounts_launch_does_not_claim_a_teleport_successor(self):
        a, b = accounts = make_accounts(2)
        mgr, parents = self._family_manager(accounts)
        mgr.launch(a, 111, None)
        pid_a = mgr.clients[a.id].pid
        orig = self.world.launch

        def launch_during_teleport(account, place, job):
            self._teleport(parents, pid_a)                   # a's client teleports mid-launch of b
            orig(account, place, job)
        mgr.launch_fn = launch_during_teleport
        mgr.launch(b, 111, None)
        self.assertNotEqual(mgr.clients[b.id].pid, mgr.clients[a.id].pid)
        self.assertEqual(self.world.owner[mgr.clients[b.id].pid], b.id)

    # ---- strict joins (Join friend): reach THAT server or get closed, never retried elsewhere ----
    def test_strict_join_that_never_arrives_is_closed_not_retried(self):
        [a] = accounts = make_accounts(1)               # auto-rejoin ON: must still not retry
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, "friendJOB12345", strict=True)   # presence says "in game", but another server
        tick_for(mgr, self.world, autorejoin.STRICT_JOIN_SECONDS + 15)
        self.assertEqual(self.world.pids, set(), "the client must be closed")
        self.assertEqual(len(self.world.launches), 1, "no retry into any server")
        self.assertNotIn(a.id, mgr.clients)

    def test_strict_join_that_arrives_keeps_running(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, "friendJOB12345", strict=True)
        self.world.presence[a.id]["gameId"] = "friendJOB12345"
        tick_for(mgr, self.world, autorejoin.STRICT_JOIN_SECONDS + 60)
        self.assertEqual(len(self.world.pids), 1)
        self.assertTrue(mgr.clients[a.id].strict_ok)
        self.assertEqual(mgr.clients[a.id].state, "running")

    def test_strict_join_whose_client_exits_early_is_not_rejoined(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, "friendJOB12345", strict=True)
        self.world.crash(a)
        tick_for(mgr, self.world, 5)
        self.assertEqual(len(self.world.launches), 1)
        self.assertNotIn(a.id, mgr.clients)

    def test_ordinary_launch_is_unaffected_by_strict_logic(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, "friendJOB12345")             # not strict
        tick_for(mgr, self.world, autorejoin.STRICT_JOIN_SECONDS + 15)
        self.assertEqual(len(self.world.pids), 1)

    # ---- restart recovery -------------------------------------------
    def test_session_records_only_include_live_trackers(self):
        accounts = make_accounts(2)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(accounts[0], 111, "jobAAAAAAAA")
        mgr.launch(accounts[1], 111, None)
        mgr.stop_account(accounts[1])
        recs = mgr.session_records()
        self.assertEqual([r["accountId"] for r in recs], ["acc1"])
        self.assertEqual(recs[0]["jobId"], "jobAAAAAAAA")
        json.dumps(recs)  # must be JSON-safe

    def test_adopt_reattaches_to_surviving_client(self):
        [a] = accounts = make_accounts(1)
        mgr1, _ = make_manager(self.world, accounts)
        mgr1.launch(a, 111, "jobAAAAAAAA")
        rec = mgr1.session_records()[0]
        # "app restarts": brand-new manager, same machine
        mgr2, _ = make_manager(self.world, accounts)
        self.assertTrue(mgr2.adopt(a, rec["placeId"], rec["jobId"], rec["pid"], rec["createdUtc"]))
        self.assertEqual(mgr2.clients[a.id].state, "running")
        self.world.crash(a)
        tick_for(mgr2, self.world, 3)   # and the new manager rejoins it
        self.assertEqual(len(self.world.launches), 2)

    def test_adopt_refuses_recycled_pid(self):
        [a] = accounts = make_accounts(1)
        mgr1, _ = make_manager(self.world, accounts)
        mgr1.launch(a, 111, None)
        rec = mgr1.session_records()[0]
        self.world.created[rec["pid"]] += timedelta(hours=3)  # same PID, different process
        mgr2, _ = make_manager(self.world, accounts)
        self.assertFalse(mgr2.adopt(a, 111, None, rec["pid"], rec["createdUtc"]))

    def test_adopt_refuses_dead_pid(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        self.assertFalse(mgr.adopt(a, 111, None, 99999, None))

    def test_recover_relaunches_after_crash(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.recover(a, 111, "jobAAAAAAAA")
        self.assertEqual(self.world.launches, [(a.id, 111, "jobAAAAAAAA")])
        self.assertEqual(mgr.clients[a.id].state, "running")


# --------------------------------------------------------------------------
# sessions.py
# --------------------------------------------------------------------------
class SessionTests(unittest.TestCase):
    def setUp(self):
        import sessions
        self.sessions = sessions
        self.tmp = Path(tempfile.mkdtemp())
        self.world = World()

    def test_roundtrip_and_clean_flag(self):
        s = self.sessions.SessionStore(self.tmp / "s.json")
        self.assertEqual(s.load(), ([], True))               # nothing saved yet = clean
        recs = [{"accountId": "acc1", "placeId": 1, "jobId": None, "pid": 5, "createdUtc": None}]
        self.assertTrue(s.save(recs, clean=False))
        self.assertFalse(s.save(recs, clean=False), "unchanged state must not rewrite the file")
        self.assertEqual(self.sessions.SessionStore(self.tmp / "s.json").load(), (recs, False))
        s.save(recs, clean=True)
        self.assertTrue(self.sessions.SessionStore(self.tmp / "s.json").load()[1])

    def test_corrupt_file_is_harmless(self):
        p = self.tmp / "s.json"
        p.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.sessions.SessionStore(p).load(), ([], True))

    def _restore(self, records, clean, accounts, rejoin_after_crash=True):
        mgr, _ = make_manager(self.world, accounts)
        counts = self.sessions.restore(mgr, FakeStore(accounts), records, clean,
                                       rejoin_after_crash=rejoin_after_crash, log=lambda m: None)
        return mgr, counts

    def test_restore_adopts_live_and_recovers_dead_after_unclean_exit(self):
        accounts = make_accounts(2)
        live = self.world.start_client(accounts[0])
        recs = [
            {"accountId": "acc1", "placeId": 111, "jobId": None, "pid": live,
             "createdUtc": self.world.created[live].isoformat()},
            {"accountId": "acc2", "placeId": 111, "jobId": "jobBBBBBBBB", "pid": 4242, "createdUtc": None},
        ]
        mgr, counts = self._restore(recs, clean=False, accounts=accounts)
        self.assertEqual(counts, {"adopted": 1, "recovered": 1, "dropped": 0})
        self.assertEqual(self.world.launches, [("acc2", 111, "jobBBBBBBBB")])

    def test_restore_does_not_relaunch_after_clean_exit(self):
        accounts = make_accounts(1)
        recs = [{"accountId": "acc1", "placeId": 111, "jobId": None, "pid": 4242, "createdUtc": None}]
        mgr, counts = self._restore(recs, clean=True, accounts=accounts)
        self.assertEqual(counts, {"adopted": 0, "recovered": 0, "dropped": 1})
        self.assertEqual(self.world.launches, [])

    def test_restore_respects_auto_rejoin_off_and_removed_accounts(self):
        accounts = make_accounts(1, rejoin=False)
        recs = [{"accountId": "acc1", "placeId": 111, "jobId": None, "pid": 4242, "createdUtc": None},
                {"accountId": "ghost", "placeId": 111, "jobId": None, "pid": 1, "createdUtc": None}]
        mgr, counts = self._restore(recs, clean=False, accounts=accounts)
        self.assertEqual(counts["recovered"], 0)
        self.assertEqual(counts["dropped"], 2)
        self.assertEqual(self.world.launches, [])


# --------------------------------------------------------------------------
# safelog.py
# --------------------------------------------------------------------------
class RedactTests(unittest.TestCase):
    def test_redacts_cookie_ticket_and_long_tokens(self):
        from safelog import redact
        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you.|_" + "A1B2" * 60
        for text in (f"Cookie: .ROBLOSECURITY={cookie}", f"oops {cookie} oops",
                     "roblox-player:1+launchmode:play+gameinfo:" + "Zq9" * 40 + "+launchtime:1",
                     "token " + "x" * 120):
            out = redact(text)
            self.assertIn("<redacted>", out)
            self.assertNotIn("A1B2A1B2", out)
            self.assertNotIn("Zq9Zq9Zq9", out)
            self.assertNotIn("xxxxxxxxxxxxxxxxxxxx", out)

    def test_leaves_normal_log_lines_alone(self):
        from safelog import redact
        line = "[rejoin] user1: relaunched (attempt 2/5, any server) job 6f1c2f4e-9a3b-4c1d-8e7f-0123456789ab"
        self.assertEqual(redact(line), line)


# --------------------------------------------------------------------------
# places.py / account_store.py / launcher.py helpers
# --------------------------------------------------------------------------
class HelperTests(unittest.TestCase):
    def test_parse_place_input(self):
        self.assertEqual(parse_place_input("920587237"), (920587237, None))
        self.assertEqual(parse_place_input("https://www.roblox.com/games/920587237/Adopt-Me"),
                         (920587237, "Adopt Me"))
        self.assertEqual(parse_place_input("https://www.roblox.com/es/games/920587237/Adopt-Me?x=1")[0], 920587237)
        self.assertEqual(parse_place_input("https://www.roblox.com/games/start?placeId=555&x=1"), (555, None))
        self.assertEqual(parse_place_input("hello"), (None, None))

    def test_place_store_find_and_persistence(self):
        path = Path(tempfile.mkdtemp()) / "places.json"
        ps = PlaceStore(path)
        ps.add(920587237, "Adopt Me")
        ps.add(2753915549, "Blox Fruits")
        self.assertEqual(ps.find("2").name, "Blox Fruits")             # list number
        self.assertEqual(ps.find("920587237").name, "Adopt Me")        # place ID
        self.assertEqual(ps.find("blox").name, "Blox Fruits")          # partial name
        self.assertIsNone(ps.find("zzz"))
        _, was_update = ps.add(920587237, "Adopt Me!")
        self.assertTrue(was_update)
        self.assertEqual(PlaceStore(path).find("1").name, "Adopt Me!")

    def test_clean_cookie_variants(self):
        for raw in ('abc123', ' abc123 ', '"abc123"', '.ROBLOSECURITY=abc123', '.ROBLOSECURITY=abc123;',
                    ".roblosecurity='abc123';"):
            self.assertEqual(clean_cookie(raw), "abc123", raw)

    def test_launch_uri_with_and_without_job(self):
        any_server = build_launch_uri("TICKET", 111)
        self.assertIn("gameinfo:TICKET", any_server)
        self.assertIn("RequestGame", any_server)
        self.assertNotIn("RequestGameJob", any_server)
        one_server = build_launch_uri("TICKET", 111, "abcd-1234-job")
        self.assertIn("RequestGameJob", one_server)
        self.assertIn("gameId%3Dabcd-1234-job", one_server)

    def test_auth_ticket_csrf_handshake(self):
        calls = []

        class Resp:
            headers = {"rbx-authentication-ticket": "TICKET123"}
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=0):
            calls.append(dict(req.header_items()))
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 403, "csrf", {"x-csrf-token": "CSRF1"}, None)
            return Resp()

        with mock.patch("launcher.urllib.request.urlopen", fake_urlopen):
            self.assertEqual(request_auth_ticket("c"), "TICKET123")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].get("X-csrf-token"), "CSRF1")

    def test_auth_ticket_rejected_cookie_is_fatal(self):
        def fake_urlopen(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
        with mock.patch("launcher.urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(LaunchError) as cm:
                request_auth_ticket("c")
        self.assertTrue(cm.exception.fatal)

    def test_auth_ticket_rate_limit_is_not_fatal(self):
        def fake_urlopen(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 429, "slow", {}, None)
        with mock.patch("launcher.urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(LaunchError) as cm:
                request_auth_ticket("c")
        self.assertFalse(cm.exception.fatal)

    def test_log_file_matching_picks_closest_unclaimed(self):
        d = Path(tempfile.mkdtemp())
        t0 = datetime(2026, 10, 15, 12, 0, 0, tzinfo=timezone.utc)
        a = d / "0.1_20261015T120002Z_Player_AAAA_last.log"
        b = d / "0.1_20261015T120030Z_Player_BBBB_last.log"
        a.write_text("x"); b.write_text("x")
        self.assertEqual(autorejoin.find_log_for_process(d, t0, set()), a)
        self.assertEqual(autorejoin.find_log_for_process(d, t0, {a}), b)
        self.assertIsNone(autorejoin.find_log_for_process(d, t0 + timedelta(hours=1), set()))

    def test_read_new_lines_handles_partial_lines_and_truncation(self):
        p = Path(tempfile.mkdtemp()) / "l.log"
        p.write_bytes(b"one\ntwo\npart")
        lines, off = autorejoin.read_new_lines(p, 0)
        self.assertEqual(lines, ["one", "two"])
        with open(p, "ab") as f:
            f.write(b"ial\n")
        lines, off = autorejoin.read_new_lines(p, off)
        self.assertEqual(lines, ["partial"])
        p.write_bytes(b"new\n")                       # Roblox rotated / truncated the file
        lines, off = autorejoin.read_new_lines(p, off)
        self.assertEqual(lines, ["new"])


# --------------------------------------------------------------------------
# diagnostics.py
# --------------------------------------------------------------------------
class DiagnosticsTests(unittest.TestCase):
    def test_offline_run_never_crashes_and_formats(self):
        import diagnostics
        results = diagnostics.run_all(store=None, holder=None, include_network=False)
        self.assertTrue(results)
        report = diagnostics.format_report(results)
        self.assertIn("Summary:", report)
        self.assertNotIn("crashed", report)

    def test_a_crashing_check_is_reported_not_raised(self):
        import diagnostics
        with mock.patch.object(diagnostics, "check_python", side_effect=RuntimeError("boom")):
            results = diagnostics.run_all(include_network=False)
        self.assertTrue(any(r.level == "FAIL" and "crashed" in r.detail for r in results))

    def test_network_check_classifies_responses(self):
        import diagnostics
        answers = iter([(401, {}), (404, {}), (403, {"X-CSRF-TOKEN": "t"})])
        with mock.patch.object(diagnostics, "_probe", lambda url, method: next(answers)):
            levels = [r.level for r in diagnostics.check_network()]
        self.assertEqual(levels, ["OK", "FAIL", "OK"])      # 401 expected; 404 = endpoint moved
        with mock.patch.object(diagnostics, "_probe", lambda url, method: (None, "dns failure")):
            self.assertTrue(all(r.level == "FAIL" for r in diagnostics.check_network()))

    def test_report_redacts_secrets(self):
        import diagnostics
        cookie = "_|WARNING:-DO-NOT-SHARE-THIS.--abc.|_" + "F00D" * 50
        report = diagnostics.format_report([diagnostics.Result("FAIL", "x", f"saw {cookie}")])
        self.assertNotIn("F00DF00D", report)

    def test_corrupt_data_file_is_a_failure(self):
        import diagnostics
        tmp = Path(tempfile.mkdtemp())
        (tmp / "places.json").write_text("{oops", encoding="utf-8")
        with mock.patch.object(diagnostics, "DATA_DIR", tmp):
            results = diagnostics.check_data_files()
        self.assertEqual([r.level for r in results if r.name == "places.json"], ["FAIL"])


if __name__ == "__main__":
    unittest.main()