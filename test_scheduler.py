"""
Scheduler tests, on the fake machine from test_phase6 (no Windows, no Roblox, no real time).
Run:  python -m unittest test_scheduler -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import diagnostics
import scheduler as sc
from settings import DEFAULTS
from test_phase6 import World, make_accounts, make_manager, tick_for

# 2026-10-14 is a Wednesday (weekday 2); 2026-10-17 a Saturday (5).
WED = datetime(2026, 10, 14)


def at(day: datetime, hh: int, mm: int, ss: int = 0) -> datetime:
    return day.replace(hour=hh, minute=mm, second=ss)


def rule(**kw) -> sc.Rule:
    base = dict(targetType="group", target="farm", action="start", time="08:00", days=[0, 1, 2, 3, 4],
                placeId=555)
    base.update(kw)
    return sc.Rule(**base)


class DueTests(unittest.TestCase):
    def test_matching_day_runs(self):
        self.assertEqual(len(sc.due_rules([rule()], at(WED, 8, 0, 10), 30)), 1)

    def test_non_matching_day_skips(self):
        saturday = datetime(2026, 10, 17)
        self.assertEqual(sc.due_rules([rule()], at(saturday, 8, 0, 10), 30), [])

    def test_every_day_rule(self):
        r = rule(days=list(range(7)))
        self.assertEqual(len(sc.due_rules([r], at(datetime(2026, 10, 17), 8, 1), 30)), 1)

    def test_within_window_runs_once(self):
        r = rule()
        self.assertEqual(len(sc.due_rules([r], at(WED, 8, 20), 30)), 1)   # woke up 20 minutes late
        r.lastRun = at(WED, 8, 20).isoformat()
        self.assertEqual(sc.due_rules([r], at(WED, 8, 21), 30), [])

    def test_outside_window_skips(self):
        self.assertEqual(sc.due_rules([rule()], at(WED, 8, 31), 30), [])
        self.assertEqual(sc.due_rules([rule()], at(WED, 7, 59), 30), [])   # not yet

    def test_midnight_candidate_from_yesterday(self):
        r = rule(time="23:50", days=[2])   # Wednesday 23:50
        thursday_0010 = datetime(2026, 10, 15, 0, 10)
        self.assertEqual(len(sc.due_rules([r], thursday_0010, 30)), 1)
        self.assertEqual(sc.due_rules([r], datetime(2026, 10, 15, 0, 25), 30), [])   # window over (35 min)

    def test_yesterday_candidate_respects_days(self):
        r = rule(time="23:50", days=[0])   # Monday only: Wednesday night does not count
        self.assertEqual(sc.due_rules([r], datetime(2026, 10, 15, 0, 10), 30), [])

    def test_no_double_run_after_last_run(self):
        r = rule(lastRun=at(WED, 8, 0, 5).isoformat())
        self.assertEqual(sc.due_rules([r], at(WED, 8, 5), 30), [])
        # but tomorrow's run is still due
        self.assertEqual(len(sc.due_rules([r], at(datetime(2026, 10, 15), 8, 0, 5), 30)), 1)

    def test_disabled_rule_never_runs(self):
        self.assertEqual(sc.due_rules([rule(enabled=False)], at(WED, 8, 0, 5), 30), [])

    def test_spring_forward_runs_once(self):
        # 02:45 does not exist on a spring-forward night; the wall clock jumps 01:59 -> 03:00.
        r = rule(time="02:45", days=list(range(7)))
        self.assertEqual(sc.due_rules([r], at(WED, 1, 59, 50), 30), [])
        self.assertEqual(len(sc.due_rules([r], at(WED, 3, 0, 5), 30)), 1)
        r.lastRun = at(WED, 3, 0, 5).isoformat()
        self.assertEqual(sc.due_rules([r], at(WED, 3, 0, 40), 30), [])

    def test_fall_back_does_not_double_run(self):
        r = rule(time="01:30", days=list(range(7)))
        self.assertEqual(len(sc.due_rules([r], at(WED, 1, 30, 5), 30), ), 1)
        r.lastRun = at(WED, 1, 30, 5).isoformat()
        # the clock falls back and 01:30 happens again
        self.assertEqual(sc.due_rules([r], at(WED, 1, 30, 20), 30), [])
        self.assertEqual(sc.due_rules([r], at(WED, 1, 45), 30), [])


class SchedulerCase(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.accounts = make_accounts(3)
        for a in self.accounts[:2]:
            a.group = "farm"
        self.mgr, _ = make_manager(self.world, self.accounts)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = sc.RuleStore(Path(self.tmp.name) / "schedules.json")
        self.logs: list[str] = []
        self.now = at(WED, 8, 0, 10)
        self.multi = True
        self.batches: list[tuple] = []

    def launch_batch(self, accounts, place_id, job_id):
        self.batches.append(([a.username for a in accounts], place_id, job_id))
        for a in accounts:
            self.mgr.launch(a, place_id, job_id)

    def make(self, *rules, **kw) -> sc.Scheduler:
        return sc.Scheduler(list(rules), self.store, self.mgr, self.launch_batch, lambda: self.now,
                            self.logs.append, lambda: 30, multi_instance_fn=lambda: self.multi,
                            run_fn=lambda fn: fn(), **kw)


class RunTests(SchedulerCase):
    def test_group_target_resolves_and_starts_members(self):
        s = self.make(rule())
        self.assertEqual(len(s.tick()), 1)
        self.assertEqual([x[:2] for x in self.world.launches], [("acc1", 555), ("acc2", 555)])
        self.assertEqual(self.batches[0][0], ["user1", "user2"])   # user3 is not in the group

    def test_tick_sets_and_saves_last_run_before_running(self):
        r = rule()
        s = self.make(r)
        seen = {}
        original = self.launch_batch
        s.launch_batch_fn = lambda *a: (seen.setdefault("saved", json.loads(self.store.path.read_text())), original(*a))
        s.tick()
        self.assertEqual(r.lastRun, self.now.isoformat(timespec="seconds"))
        self.assertEqual(seen["saved"]["rules"][0]["lastRun"], r.lastRun)   # already on disk when the run began
        self.assertEqual(s.tick(), [])                                       # and it does not repeat

    def test_stop_does_not_trigger_rejoin(self):
        for a in self.accounts[:2]:
            self.mgr.launch(a, 555, None)
        self.assertEqual(len(self.world.launches), 2)
        s = self.make(rule(action="stop", time="23:00"))
        self.now = at(WED, 23, 0, 10)
        s.tick()
        self.assertEqual(len(self.world.kills), 2)
        tick_for(self.mgr, self.world, 120)
        self.assertEqual(len(self.world.launches), 2)   # nothing came back

    def test_already_running_skipped(self):
        self.mgr.launch(self.accounts[0], 555, None)
        s = self.make(rule())
        s.tick()
        self.assertEqual(self.batches[0][0], ["user2"])
        self.assertTrue(any("already running" in m for m in self.logs))

    def test_multi_instance_off_skips_multi_launch(self):
        self.multi = False
        s = self.make(rule())
        s.tick()
        self.assertEqual(self.world.launches, [])
        self.assertTrue(any("multi-instance is off" in m for m in self.logs))

    def test_multi_instance_off_still_allows_a_single_client(self):
        self.multi = False
        s = self.make(rule(targetType="account", target="user3", placeId=None), group_games_fn=lambda: {})
        self.accounts[2].savedPlaceId = 777
        s.tick()
        self.assertEqual([x[:2] for x in self.world.launches], [("acc3", 777)])

    def test_game_order_rule_then_group_default_then_saved(self):
        self.accounts[0].savedPlaceId = 3
        s = self.make(rule(placeId=None), group_games_fn=lambda: {"farm": {"placeId": 2, "jobId": None}})
        s.tick()
        self.assertEqual(self.batches[0][1], 2)                 # group default beats the saved game
        self.world.launches.clear()
        for a in self.accounts:
            self.mgr.stop_account(a)
        s2 = self.make(rule(placeId=None))
        s2.tick()
        self.assertEqual(self.batches[1][1], 3)                 # saved game when there is no default
        self.assertEqual(sc.resolve_game(rule(placeId=9), self.accounts, {"farm": {"placeId": 2}}), (9, None))

    def test_no_game_is_skipped_and_logged(self):
        s = self.make(rule(placeId=None))
        s.tick()
        self.assertEqual(self.world.launches, [])
        self.assertTrue(any("no game" in m for m in self.logs))

    def test_unknown_target_logged_not_raised(self):
        s = self.make(rule(target="ghosts"))
        s.tick()
        self.assertTrue(any("does not exist" in m for m in self.logs))
        self.assertEqual(self.world.launches, [])

    def test_launch_error_is_contained(self):
        def boom(*a):
            raise RuntimeError("boom")
        s = self.make(rule())
        s.launch_batch_fn = boom
        s.tick()
        self.assertTrue(any("failed" in m for m in self.logs))

    def test_run_now_ignores_the_clock_and_keeps_bookkeeping(self):
        r = rule(time="03:00")
        s = self.make(r)
        s.run_now(r)
        self.assertEqual(len(self.world.launches), 2)
        self.assertIsNone(r.lastRun)

    def test_disabled_scheduler_does_nothing(self):
        s = self.make(rule(), enabled_fn=lambda: False)
        self.assertEqual(s.tick(), [])
        self.assertEqual(self.world.launches, [])

    def test_missed_run_fires_once_after_reopen(self):
        r = rule()
        self.now = at(WED, 8, 12)        # app opened 12 minutes late
        self.assertEqual(len(self.make(r).tick()), 1)
        again = sc.RuleStore(self.store.path).load()[0]   # app restarted: lastRun came from disk
        self.assertEqual(sc.due_rules([again], at(WED, 8, 20), 30), [])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "schedules.json"

    def test_rules_roundtrip(self):
        store = sc.RuleStore(self.path)
        rules = [rule(lastRun="2026-10-14T08:00:12", jobId="abcdefghij"), rule(action="stop", time="23:00", placeId=None)]
        store.save(rules)
        self.assertEqual([r.to_dict() for r in sc.RuleStore(self.path).load()], [r.to_dict() for r in rules])

    def test_corrupt_schedule_file_harmless(self):
        self.path.write_text("{ not json", encoding="utf-8")
        store = sc.RuleStore(self.path)
        self.assertEqual(store.load(), [])
        self.assertTrue(store.problems)
        self.assertTrue(self.path.with_name("schedules.json.bad").exists())   # a copy was kept
        self.path.write_text("[1, 2]", encoding="utf-8")                       # valid JSON, wrong shape
        self.assertEqual(store.load(), [])

    def test_missing_file_is_empty_and_quiet(self):
        store = sc.RuleStore(self.path)
        self.assertEqual(store.load(), [])
        self.assertEqual(store.problems, [])

    def test_bad_entries_are_skipped_good_ones_kept(self):
        good = rule().to_dict()
        self.path.write_text(json.dumps({"version": 1, "rules": [
            good, "junk", {"target": "x"}, dict(good, time="25:99"), dict(good, action="explode"),
            dict(good, days=[9, "x"], placeId="nope", unknownKey=1)]}), encoding="utf-8")
        store = sc.RuleStore(self.path)
        rules = store.load()
        self.assertEqual(len(rules), 2)
        self.assertEqual(len(store.problems), 4)
        self.assertEqual((rules[1].days, rules[1].placeId), ([], None))   # sanitised, and harmless: no days = never due

    def test_parse_time(self):
        self.assertEqual(sc.parse_time("8:05"), (8, 5))
        self.assertIsNone(sc.parse_time("24:00"))
        self.assertIsNone(sc.parse_time("noon"))


class ValidateAndDiagnosticsTests(SchedulerCase):
    def test_validate_catches_each_problem(self):
        self.assertEqual(sc.validate_rule(rule(), self.accounts), [])
        self.assertTrue(sc.validate_rule(rule(time="9am"), self.accounts))
        self.assertTrue(sc.validate_rule(rule(days=[]), self.accounts))
        self.assertTrue(sc.validate_rule(rule(target="ghosts"), self.accounts))
        self.assertTrue(sc.validate_rule(rule(placeId=None), self.accounts))                  # no game anywhere
        self.assertEqual(sc.validate_rule(rule(placeId=None, action="stop"), self.accounts), [])   # stop needs none
        self.assertEqual(sc.validate_rule(rule(placeId=None), self.accounts, {"farm": {"placeId": 4}}), [])

    def test_account_target_is_case_insensitive(self):
        self.assertEqual(len(sc.accounts_for(rule(targetType="account", target="USER2"), self.accounts)), 1)

    def test_format_days(self):
        self.assertEqual(sc.format_days(list(range(7))), "Every day")
        self.assertEqual(sc.format_days([0, 1, 2, 3, 4]), "Weekdays")
        self.assertEqual(sc.format_days([5, 6]), "Weekends")
        self.assertEqual(sc.format_days([0, 2]), "Mon, Wed")

    def test_diagnostics_reports_rules_and_problems(self):
        s = self.make(rule(), rule(target="ghosts"), rule(placeId=None))
        s.tick()
        out = diagnostics.check_scheduler({"schedulerEnabled": True, "scheduleMissedWindowMinutes": 30,
                                           "groupGames": {}}, s)
        levels = [r.level for r in out]
        self.assertEqual(levels.count("WARN"), 2)   # unknown target, and the rule with no game
        self.assertTrue(any(r.name == "Scheduler last tick" and "2026-10-14" in r.detail for r in out))

    def test_diagnostics_flags_bad_window_and_file_problems(self):
        self.store.problems = ["schedules.json is unreadable"]
        out = diagnostics.check_scheduler({"scheduleMissedWindowMinutes": 5000}, self.make())
        self.assertGreaterEqual(sum(1 for r in out if r.level == "WARN"), 2)

    def test_diagnostics_skips_without_scheduler(self):
        self.assertEqual(diagnostics.check_scheduler({}, None).level, "SKIP")

    def test_settings_defaults(self):
        self.assertIs(DEFAULTS["schedulerEnabled"], True)
        self.assertEqual(DEFAULTS["scheduleMissedWindowMinutes"], 30)


if __name__ == "__main__":
    unittest.main()