"""
Server hopper: moves accounts to a new public server every N minutes (or on demand).

A hop is always "close, then start": the account's current client is closed COMPLETELY
(RejoinManager.hop waits until its process is gone) and only then is a new one launched into
the chosen server, so one account never has two clients running. If the old client refuses to
close, nothing is launched.

The new server is chosen BEFORE anything is closed. If no suitable server exists the account
simply stays where it is and the hop is tried again later.

Server lists come from server_finder's session cache, so a hop picks from the same list the
Server finder just searched, and any number of accounts hopping in the same game cost at most
one list request every few minutes.

History: every server an account hops to (or is sent to from the Server finder) is remembered
per game, newest last, up to HISTORY_LIMIT. A hop never goes back into a server in that history,
so hopping accounts spread out instead of piling into the same "best" server. When every server
in the list has been visited, only the newest few stay blocked and the older ones become
available again; the history shown to the user is never trimmed by that.

Pure logic: no Tk, no Windows. The manager, the server lookup and the clock are parameters, so
tests drive it with fakes. All work happens on this thread, one hop at a time.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import server_finder as sf

MIN_INTERVAL_MINUTES = 2      # hopping faster than this only burns launches and invites rate limits
MAX_INTERVAL_MINUTES = 24 * 60
MIN_FREE_SLOTS = 2            # the list is a few minutes old: do not aim at a server with one seat left
HOP_GAP_SECONDS = 5.0         # pause between two hops, so several accounts never launch back to back
BUSY_RETRY_SECONDS = 20.0     # the account is starting / being rejoined: look again shortly
NO_SERVER_RETRY_SECONDS = 60.0
FAIL_RETRY_SECONDS = 60.0
MAX_FAILURES = 3              # in a row, then hopping is switched off for that account
HISTORY_LIMIT = 100           # servers remembered per game
KEEP_RECENT = 5               # list used up: only the newest this many stay blocked (at most half the list)


@dataclass
class Plan:
    account: object
    mode: str = "best"
    min_players: int = 0
    interval: float = 1800.0      # seconds
    place_id: int | None = None   # used only when the account is not running yet
    next_at: float = 0.0
    hops: int = 0
    failures: int = 0
    seen_running: bool = False    # we have seen a client for this account (so a vanished one = user stopped it)
    repeat: bool = True           # False: one hop, then the plan removes itself
    last: str = "waiting"


class ServerHopper(threading.Thread):
    def __init__(self, manager, log_fn=print, find_fn=None, clock=time.monotonic, sleep=time.sleep,
                 busy_fn=None, wall=time.time) -> None:
        super().__init__(daemon=True)
        self.manager = manager
        self.log = log_fn
        self.find = find_fn or sf.find_servers_cached     # (place_id, force=bool) -> sf.Found
        self.clock, self.sleep, self.wall = clock, sleep, wall
        self.busy_fn = busy_fn or (lambda account: False)  # True while the app itself is launching this account
        self._plans: dict[str, Plan] = {}
        # place -> {job_id: {"at": wall time, "who": username, "seq": n}}, oldest first. Memory only:
        # server IDs are useless after a few minutes, so nothing is written to disk.
        self._hist: dict[int, dict[str, dict]] = {}
        self._floor: dict[int, int] = {}                   # place -> entries older than this seq no longer block
        self._seq = 0
        self._lock = threading.RLock()
        self._hop_lock = threading.Lock()                  # one hop at a time
        self._stop_event = threading.Event()

    # ---- public API -----------------------------------------------------
    def enable(self, account, *, interval_minutes: float, mode: str = "best", min_players: int = 0,
               place_id: int | None = None, first_in: float | None = None, repeat: bool = True) -> Plan:
        """Start (or restart) hopping for an account. `first_in` seconds until the first hop;
        default: one full interval if it is running, right away if it is not."""
        if mode not in sf.MODES:
            raise ValueError(f"unknown mode {mode!r}")
        minutes = min(max(float(interval_minutes), MIN_INTERVAL_MINUTES), MAX_INTERVAL_MINUTES)
        running = self._client(account) is not None
        if first_in is None:
            first_in = minutes * 60 if running else 0.0
        plan = Plan(account, mode, max(0, int(min_players)), minutes * 60, place_id,
                    next_at=self.clock() + max(0.0, first_in), seen_running=running, repeat=repeat)
        with self._lock:
            self._plans[account.id] = plan
        self.log(f"[hop] {account.username}: " + (f"hopping every {minutes:g} min" if repeat else "one hop")
                 + f" ({sf.MODE_LABELS[mode]})")
        return plan

    def disable(self, account, why: str = "") -> bool:
        with self._lock:
            plan = self._plans.pop(account.id, None)
        if plan:
            self.log(f"[hop] {account.username}: hopping off" + (f" ({why})" if why else ""))
        return plan is not None

    def disable_all(self) -> None:
        with self._lock:
            plans = list(self._plans.values())
            self._plans.clear()
        for p in plans:
            self.log(f"[hop] {p.account.username}: hopping off")

    def hop_now(self, account) -> bool:
        """Make the next tick hop this account."""
        with self._lock:
            plan = self._plans.get(account.id)
            if plan:
                plan.next_at = 0.0
        return plan is not None

    def is_enabled(self, account) -> bool:
        with self._lock:
            return account.id in self._plans

    def snapshot(self) -> list[dict]:
        now = self.clock()
        with self._lock:
            return [dict(id=aid, username=p.account.username, mode=p.mode, hops=p.hops,
                         next_in=max(0.0, p.next_at - now), last=p.last, interval=p.interval,
                         repeat=p.repeat)
                    for aid, p in self._plans.items()]

    def stop(self) -> None:
        self._stop_event.set()

    # ---- history --------------------------------------------------------
    def record_visit(self, place_id: int, job_id: str, who: str = "") -> None:
        """Remember that `who` went into this server. The Server finder calls this when a server is
        joined from its list, so a later hop does not go back there."""
        if not place_id or not job_id:
            return
        with self._lock:
            hist = self._hist.setdefault(place_id, {})
            hist.pop(job_id, None)              # a revisit moves to the newest position
            self._seq += 1
            hist[job_id] = {"at": self.wall(), "who": who, "seq": self._seq}
            while len(hist) > HISTORY_LIMIT:
                hist.pop(next(iter(hist)))

    def history(self, place_id: int | None = None) -> list[dict]:
        """Visited servers, newest first (all games, or just one)."""
        with self._lock:
            rows = [dict(place_id=pid, job_id=job, at=e["at"], who=e["who"], seq=e["seq"])
                    for pid, hist in self._hist.items() if place_id is None or pid == place_id
                    for job, e in hist.items()]
        rows.sort(key=lambda r: -r["seq"])
        return rows

    def was_visited(self, place_id: int, job_id: str) -> bool:
        with self._lock:
            return job_id in self._hist.get(place_id, ())

    def forget_used(self, place_id: int | None = None) -> None:
        """Clears the history (of one game, or of all)."""
        with self._lock:
            if place_id is None:
                self._hist.clear()
                self._floor.clear()
            else:
                self._hist.pop(place_id, None)
                self._floor.pop(place_id, None)

    clear_history = forget_used

    def _blocked(self, place_id: int) -> set[str]:
        with self._lock:
            floor = self._floor.get(place_id, 0)
            return {job for job, e in self._hist.get(place_id, {}).items() if e["seq"] >= floor}

    def _release_old(self, place_id: int, list_size: int) -> None:
        """Every listed server is blocked: let the older ones go, keeping only the newest few
        (never more than half the list, so there is always somewhere to go)."""
        keep = min(KEEP_RECENT, list_size // 2)
        with self._lock:
            seqs = sorted(e["seq"] for e in self._hist.get(place_id, {}).values())
            self._floor[place_id] = seqs[-keep] if keep and len(seqs) >= keep else (seqs[-1] + 1 if seqs else 0)

    # ---- thread ---------------------------------------------------------
    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:   # never let the loop die
                self.log(f"[hop] internal error: {exc!r}")
            self._stop_event.wait(2.0)

    def tick(self) -> None:
        """Hops every account that is due, one after the other."""
        now = self.clock()
        with self._lock:
            due = [p for p in self._plans.values() if now >= p.next_at]
        due.sort(key=lambda p: p.next_at)
        for plan in due:
            if self._stop_event.is_set():
                return
            with self._lock:
                if self._plans.get(plan.account.id) is not plan:   # switched off meanwhile
                    continue
            before = plan.hops
            self._run_plan(plan)
            if plan.hops != before:
                self.sleep(HOP_GAP_SECONDS)   # space real hops out; skipped plans cost nothing

    # ---- one hop --------------------------------------------------------
    def _client(self, account):
        with self.manager._lock:
            return self.manager.clients.get(account.id)

    def _run_plan(self, plan: Plan) -> None:
        with self._hop_lock:
            try:
                self._hop(plan)
            except Exception as exc:
                plan.failures += 1
                plan.last = f"failed: {exc}"
                self.log(f"[hop] {plan.account.username}: hop failed - {exc}")
                if plan.failures >= MAX_FAILURES:
                    self.disable(plan.account, f"{MAX_FAILURES} failures in a row")
                else:
                    plan.next_at = self.clock() + FAIL_RETRY_SECONDS

    def _hop(self, plan: Plan) -> None:
        account = plan.account
        client = self._client(account)
        now = self.clock()

        if client is None and plan.seen_running:
            self.disable(account, "its client was stopped")   # Stop was pressed: do not bring it back
            return
        if self.busy_fn(account):
            plan.next_at = now + BUSY_RETRY_SECONDS
            return
        if client is not None:
            plan.seen_running = True
            if client.private:
                self.disable(account, "it is in a private server")
                return
            if client.state != "running":
                # starting / rejoining / waiting for a retry / gave up: auto-rejoin owns it right now
                plan.last = f"waiting ({client.state})"
                plan.next_at = now + BUSY_RETRY_SECONDS
                return

        place_id = client.place_id if client is not None else plan.place_id
        if not place_id:
            self.disable(account, "no game known")
            return
        current = client.job_id if client is not None else None
        if current:
            self.record_visit(place_id, current, account.username)   # where it is now counts as visited

        job = self._pick(plan, place_id, current)
        if job is None:
            plan.last = "no other server available; staying"
            plan.next_at = now + min(plan.interval, NO_SERVER_RETRY_SECONDS)
            self.log(f"[hop] {account.username}: no other server with room right now; staying where it is")
            return

        self.log(f"[hop] {account.username}: moving to server {job[:8]}...")
        if client is not None:
            self.manager.hop(account, place_id, job)
        else:
            self.manager.launch(account, place_id, job)
        plan.hops += 1
        plan.failures = 0
        plan.seen_running = True
        plan.last = f"joined {job[:8]}..."
        plan.next_at = self.clock() + plan.interval
        self.log(f"[hop] {account.username}: hop #{plan.hops} done")
        if not plan.repeat:
            self.disable(account, "single hop done")

    def _pick(self, plan: Plan, place_id: int, current: str | None) -> str | None:
        """A server that is not in the history (this account has not been there, nobody else was sent
        there). Records it in the history."""
        found = self.find(place_id, force=False)
        job = self._choose(plan, place_id, current, found)
        if job is None and found.cached:
            # the cached list may simply be used up; one forced refresh (the cache rate-limits it)
            found = self.find(place_id, force=True)
            job = self._choose(plan, place_id, current, found)
        if job is None:
            # every listed server is in the history: free the older ones, keep the newest few blocked
            self._release_old(place_id, len(found.servers))
            job = self._choose(plan, place_id, current, found)
        return job

    def _choose(self, plan: Plan, place_id: int, current: str | None, found) -> str | None:
        used = self._blocked(place_id)
        if current:
            used.add(current)
        for s in sf.rank(found.servers, plan.mode, min_players=plan.min_players):
            if s.job_id in used or s.max_players - s.playing < MIN_FREE_SLOTS:
                continue
            self.record_visit(place_id, s.job_id, plan.account.username)
            return s.job_id
        return None
