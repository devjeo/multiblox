"""
Scheduler: start or stop an account or a group at a time of day, on chosen weekdays.

Rules live in schedules.json (DATA_DIR). Times are naive LOCAL times, days are 0 = Monday.
No tkinter and no Win32 here; everything the scheduler needs from the app is injected, so it
is tested with the fake machine from test_phase6.

How a tick decides (due_rules): for each enabled rule, the scheduled moment on today and on
yesterday is a candidate (so a 23:50 rule is still catchable at 00:10). A candidate is due when
its weekday is in the rule's days, scheduled <= now <= scheduled + window, and the rule has not
run since that moment (lastRun < scheduled). That one rule covers normal ticks, a missed run
after sleep, midnight, and daylight saving: spring-forward still fires once, and fall-back
cannot double-run because of lastRun.

Runs happen on a worker thread (launching blocks for the client and the settle pause), and
lastRun is saved BEFORE the run starts, so a crash mid-run never repeats it.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import groups
from account_store import DATA_DIR
from jsonio import read_json, write_json_atomic
from workers import Worker

SCHEDULE_FILE = DATA_DIR / "schedules.json"
ACTIONS = ("start", "stop")
TARGET_TYPES = ("group", "account")
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
BUSY_STATES = ("starting", "running", "rejoining")


@dataclass
class Rule:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    enabled: bool = True
    targetType: str = "group"        # group | account
    target: str = ""                 # a group name, or an account username
    action: str = "start"            # start | stop
    time: str = "08:00"              # HH:MM, local
    days: list = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])
    placeId: int | None = None
    jobId: str | None = None
    lastRun: str | None = None       # ISO local time of the last run

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def parse_time(text) -> tuple[int, int] | None:
    m = _TIME_RE.match(str(text).strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    return (hour, minute) if hour < 24 and minute < 60 else None


def _parse_dt(text) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if isinstance(text, str) and text else None
    except ValueError:
        return None


def rule_from_dict(raw) -> Rule | None:
    """A Rule from a saved dict, or None if it is unusable. Unknown keys are ignored."""
    if not isinstance(raw, dict):
        return None
    rule = Rule()
    if isinstance(raw.get("id"), str) and raw["id"]:
        rule.id = raw["id"]
    rule.enabled = raw.get("enabled", True) is not False
    rule.targetType = raw.get("targetType") if raw.get("targetType") in TARGET_TYPES else "group"
    rule.target = raw.get("target").strip() if isinstance(raw.get("target"), str) else ""
    rule.action = raw.get("action") if raw.get("action") in ACTIONS else ""
    days = raw.get("days")
    rule.days = sorted({d for d in days if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6}) \
        if isinstance(days, list) else []
    place = raw.get("placeId")
    rule.placeId = place if isinstance(place, int) and not isinstance(place, bool) and place > 0 else None
    job = raw.get("jobId")
    rule.jobId = job if isinstance(job, str) and len(job) >= 8 else None
    rule.lastRun = raw.get("lastRun") if _parse_dt(raw.get("lastRun")) else None
    if not rule.action or not rule.target or parse_time(raw.get("time")) is None:
        return None
    hour, minute = parse_time(raw["time"])
    rule.time = f"{hour:02d}:{minute:02d}"
    return rule


class RuleStore:
    """schedules.json: {'version': 1, 'rules': [...]}. Never raises on a bad file."""

    def __init__(self, path: Path = SCHEDULE_FILE) -> None:
        self.path = Path(path)
        self.problems: list[str] = []

    def load(self) -> list[Rule]:
        self.problems = []
        if not self.path.exists():
            return []
        raw = read_json(self.path, None)
        if not isinstance(raw, dict) or not isinstance(raw.get("rules"), list):
            self.problems.append("schedules.json is unreadable or not in the expected format; no rules loaded "
                                 "(a copy was kept as schedules.json.bad)")
            try:
                shutil.copyfile(self.path, self.path.with_name(self.path.name + ".bad"))
            except OSError:
                pass
            return []
        rules = []
        for i, entry in enumerate(raw["rules"], 1):
            rule = rule_from_dict(entry)
            if rule is None:
                self.problems.append(f"rule {i} is incomplete or invalid and was skipped")
            else:
                rules.append(rule)
        return rules

    def save(self, rules) -> None:
        write_json_atomic(self.path, {"version": 1, "rules": [r.to_dict() for r in rules]})


def due_rules(rules, now: datetime, window_min: int) -> list[Rule]:
    """Rules that should run now. Pure."""
    window = timedelta(minutes=max(1, min(int(window_min), 1440)))
    due = []
    for rule in rules:
        if not rule.enabled:
            continue
        hm = parse_time(rule.time)
        if hm is None:
            continue
        last = _parse_dt(rule.lastRun)
        best = None
        for offset in (0, -1):
            day = now.date() + timedelta(days=offset)
            if day.weekday() not in rule.days:
                continue
            scheduled = datetime(day.year, day.month, day.day, hm[0], hm[1])
            if scheduled <= now <= scheduled + window and (last is None or last < scheduled):
                best = scheduled if best is None or scheduled > best else best
        if best is not None:
            due.append(rule)
    return due


def format_days(days) -> str:
    days = sorted(set(days))
    if days == list(range(7)):
        return "Every day"
    if days == [0, 1, 2, 3, 4]:
        return "Weekdays"
    if days == [5, 6]:
        return "Weekends"
    return ", ".join(DAY_NAMES[d] for d in days if 0 <= d <= 6) or "No days"


def accounts_for(rule: Rule, accounts) -> list:
    """The accounts a rule applies to (empty if the target does not exist)."""
    if rule.targetType == "account":
        return [a for a in accounts if a.username.lower() == rule.target.strip().lower()]
    if not rule.target.strip() or rule.target == groups.ALL:
        return []
    return groups.filter_accounts(accounts, rule.target)


def resolve_game(rule: Rule, accounts, group_games) -> tuple[int, str | None] | None:
    """Rule's own game, then the group default, then a saved game of a member."""
    members = accounts_for(rule, accounts)
    if not members:
        return None
    group = rule.target if rule.targetType == "group" else (members[0].group or "")
    return groups.resolve_target(group, rule.placeId, rule.jobId if rule.placeId else None, group_games, members)


def validate_rule(rule: Rule, accounts, group_games=None) -> list[str]:
    """Human-readable problems; empty means the rule can be saved."""
    problems = []
    if rule.action not in ACTIONS:
        problems.append("Choose an action (start or stop).")
    if parse_time(rule.time) is None:
        problems.append("Time must look like 08:00 (24-hour).")
    if not rule.days:
        problems.append("Pick at least one day.")
    if not accounts_for(rule, accounts):
        what = "account" if rule.targetType == "account" else "group (with at least one account)"
        problems.append(f"The target is not an existing {what}.")
    elif rule.action == "start" and resolve_game(rule, accounts, group_games or {}) is None:
        problems.append("No game: enter a place ID, pick a saved game, or set a default game for the group.")
    return problems


class Scheduler:
    """`launch_batch_fn(accounts, place_id, job_id)` launches the accounts one after another and
    blocks until done (the app passes its _launch_worker). `manager` is the RejoinManager."""

    def __init__(self, rules, store, manager, launch_batch_fn, now_fn, log_fn, window_min_fn, *,
                 group_games_fn=lambda: {}, multi_instance_fn=lambda: True, enabled_fn=lambda: True,
                 run_fn=None) -> None:
        self.rules: list[Rule] = rules
        self.store, self.manager = store, manager
        self.launch_batch_fn, self.now_fn, self.log = launch_batch_fn, now_fn, log_fn
        self.window_min_fn = window_min_fn
        self.group_games_fn, self.multi_instance_fn, self.enabled_fn = group_games_fn, multi_instance_fn, enabled_fn
        self.last_tick: datetime | None = None
        self.last_results: dict[str, str] = {}   # rule id -> short outcome of its last run
        self._lock = threading.Lock()
        self._worker: Worker | None = None
        self._run_fn = run_fn   # tests pass a synchronous runner; the app uses the worker thread

    # ---- ticking ----------------------------------------------------------
    def tick(self) -> list[Rule]:
        """Starts every due rule. Cheap; call every 30 s and once at startup."""
        now = self.now_fn()
        self.last_tick = now
        if not self.enabled_fn():
            return []
        with self._lock:
            due = due_rules(self.rules, now, self.window_min_fn())
            for rule in due:
                rule.lastRun = now.isoformat(timespec="seconds")   # before the run: a crash never repeats it
            if due:
                self._save()
        for rule in due:
            self.log(f"[schedule] running: {self.describe(rule)}")
            self._submit(dataclasses.replace(rule, days=list(rule.days)))
        return due

    def run_now(self, rule: Rule) -> None:
        """Runs a rule immediately (the Run now button). Does not change its schedule bookkeeping."""
        self.log(f"[schedule] running now: {self.describe(rule)}")
        self._submit(dataclasses.replace(rule, days=list(rule.days)))

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop(timeout=1.0)

    def save(self) -> None:
        with self._lock:
            self._save()

    def _save(self) -> None:
        try:
            self.store.save(self.rules)
        except OSError as exc:
            self.log(f"[!] Schedule: could not save schedules.json ({exc}).")

    def _submit(self, rule: Rule) -> None:
        if self._run_fn is not None:
            self._run_fn(lambda: self._execute_safely(rule))
            return
        if self._worker is None:
            self._worker = Worker("scheduler", lambda fn: fn(), self.log)
            self._worker.start()
        self._worker.submit(lambda: self._execute_safely(rule))

    # ---- running a rule ---------------------------------------------------
    @staticmethod
    def describe(rule: Rule) -> str:
        return f"{rule.action} {rule.targetType} '{rule.target}' at {rule.time} ({format_days(rule.days)})"

    def _execute_safely(self, rule: Rule) -> None:
        try:
            self.last_results[rule.id] = self._execute(rule)
        except Exception as exc:
            self.last_results[rule.id] = f"error: {exc!r}"
            self.log(f"[!] Schedule: {self.describe(rule)} failed: {exc!r}")

    def _execute(self, rule: Rule) -> str:
        accounts = self.manager.store.accounts
        members = accounts_for(rule, accounts)
        if not members:
            self.log(f"[!] Schedule: '{rule.target}' does not exist any more; skipped.")
            return "target not found"
        if rule.action == "stop":
            stopped = 0
            for account in members:
                if self.manager.stop_account(account):   # marks the stop intentional: no auto-rejoin
                    stopped += 1
                    self.log(f"[schedule] stopped {account.username}")
            return f"stopped {stopped} of {len(members)}"
        return self._start(rule, members, accounts)

    def _start(self, rule: Rule, members, accounts) -> str:
        with self.manager._lock:
            busy = {aid for aid, c in self.manager.clients.items() if c.state in BUSY_STATES}
        todo = []
        for account in members:
            if account.id in busy:
                self.log(f"[schedule] {account.username} is already running; skipped.")
            else:
                todo.append(account)
        if not todo:
            return "nothing to start"
        game = resolve_game(rule, accounts, self.group_games_fn())
        if game is None:
            self.log(f"[!] Schedule: no game for '{rule.target}'; skipped.")
            return "no game"
        if not self.multi_instance_fn() and len(todo) + len(self.manager.list_pids()) > 1:
            self.log("[!] Schedule: multi-instance is off and more than one client would run; skipped.")
            return "multi-instance off"
        place_id, job_id = game
        self.launch_batch_fn(todo, place_id, job_id)
        return f"started {len(todo)}"