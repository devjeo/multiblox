"""
Phase 4: auto-rejoin.

Watches every account launched through launcher.py and, if auto-rejoin is ON
for that account, relaunches it when it disconnects.

A disconnect is detected three ways:
  1. PROCESS EXIT   - the client's RobloxPlayerBeta.exe is gone (instant).
  2. LOG MARKERS    - the client's log under %LOCALAPPDATA%\\Roblox\\logs contains a
                      disconnect marker. To avoid killing a healthy client (e.g.
                      during a teleport), a marker is only acted on if Roblox
                      presence shows the account is NOT in a game ~15s later.
  3. PRESENCE       - Roblox presence says the account is not in a game for two
                      checks in a row (about 90s), covering clients that are
                      still open on an error screen.

Safety limits: no action in the first 30s after a client starts, at most 5
rejoin attempts per account per 10 minutes, and the first attempt returns to
the same server (job) while later attempts accept any server.

NOTE: closing a client yourself looks like a disconnect. Use the launcher's
'stop' command (or turn auto-rejoin off) when you want a client to stay closed.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from account_store import DATA_DIR

PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RobloxAccountManager/0.5"

# Lowercase substrings that suggest the client lost its connection.
# Tune these using rejoin_debug.log (see _dump_debug) if they misfire on your PC.
DISCONNECT_MARKERS = (
    "connection lost",
    "time to disconnect replication data",
    "disconnected",
)

GRACE_SECONDS = 30            # ignore disconnect signals right after a client starts
MARKER_DEBOUNCE_SECONDS = 15  # wait this long after a log marker, then check presence
PRESENCE_POLL_SECONDS = 45
# Location checks: they only learn WHERE an account is (server history, the green "Online now" rows) and
# never count as a miss, so they cannot cause a rejoin. The slow PRESENCE_POLL_SECONDS poll above is still
# the only one that detects a disconnect. A client that has not been confirmed in a server yet is asked
# often, so a new join shows up within seconds; later the check slows down. Asked in the background, so a
# slow Roblox answer never delays the other accounts.
LOCATION_START_SECONDS = 15   # not before the client has had time to load (presence still shows the old server)
LOCATION_FAST_SECONDS = 5     # until confirmed (or LOCATION_FAST_WINDOW seconds after the launch)
LOCATION_FAST_WINDOW = 300
LOCATION_LIVE_SECONDS = 15    # once confirmed: notices a teleport or a leave quickly
LOCATION_BACKOFF_SECONDS = 30  # after an answer of "could not tell" (rate limit, network): wait longer
PRESENCE_MISS_LIMIT = 2       # consecutive "not in game" polls before rejoining
REJOIN_COOLDOWN_SECONDS = 8
SETTLE_SECONDS = 6            # pause after a launch so the event-handle closer can finish
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 600
RETRY_DELAY_SECONDS = 30
NEW_CLIENT_TIMEOUT = 90
LOG_MAP_TIMEOUT = 180
# "Must reach this exact server" launches (joining a friend): if the account is not in that server
# within STRICT_JOIN_SECONDS (refused, friends-only, full...), the client is closed, never retried.
STRICT_FIRST_CHECK_SECONDS = 20
STRICT_POLL_SECONDS = 10
STRICT_JOIN_SECONDS = 75
# "Patient" joins (a player's or a history server, e.g. a full server): the client is LEFT OPEN for as
# long as it takes. There is no timeout, nothing is closed or relaunched while it waits, and the
# presence and log checks that would call it "disconnected" are skipped until it is in the server
# (or Stop is pressed). Only if the client process itself exits (Roblox quit, or the window was
# closed) is the SAME server launched again after this many seconds.
PATIENT_RETRY_SECONDS = 20

DEBUG_LOG = DATA_DIR / "rejoin_debug.log"   # under %APPDATA%: a PyInstaller exe has no stable folder next to the script
PRESENCE_NAMES = {0: "offline", 1: "online (not in a game)", 2: "in game", 3: "in Studio"}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def fetch_own_presence(cookie: str, user_id: int) -> dict | None:
    """Presence of the account that owns `cookie`. None means 'could not tell'."""
    headers = {
        "Cookie": f".ROBLOSECURITY={cookie}",
        "Content-Type": "application/json",
        "Referer": "https://www.roblox.com/",
        "User-Agent": USER_AGENT,
    }
    body = json.dumps({"userIds": [user_id]}).encode("utf-8")
    csrf = None
    for _ in range(2):
        if csrf:
            headers["X-CSRF-TOKEN"] = csrf
        req = urllib.request.Request(PRESENCE_URL, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                presences = json.loads(resp.read().decode("utf-8")).get("userPresences") or []
                return presences[0] if presences else None
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and exc.headers.get("x-csrf-token") and not csrf:
                csrf = exc.headers["x-csrf-token"]
                continue
            return None
        except Exception:
            return None
    return None


def process_creation_utc(pid: int) -> datetime | None:
    """When the process started (UTC). Windows only."""
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    fp = ctypes.POINTER(wintypes.FILETIME)
    k.GetProcessTimes.argtypes = [wintypes.HANDLE, fp, fp, fp, fp]
    k.GetProcessTimes.restype = wintypes.BOOL
    handle = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
        if not k.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
            return None
        ticks = (c.dwHighDateTime << 32) | c.dwLowDateTime
        return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks / 10)
    finally:
        k.CloseHandle(handle)


_LOG_TS_RE = re.compile(r"_(\d{8}T\d{6})Z_")


def find_log_for_process(log_dir: Path, created_utc: datetime, claimed: set) -> Path | None:
    """Roblox names each client log ..._<UTC timestamp>Z_Player_..._last.log; pick the
    one created closest to when this process started."""
    best, best_delta = None, None
    try:
        candidates = list(log_dir.glob("*Player*.log"))
    except OSError:
        return None
    for p in candidates:
        if p in claimed:
            continue
        m = _LOG_TS_RE.search(p.name)
        try:
            if m:
                ts = datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            else:
                ts = datetime.fromtimestamp(p.stat().st_ctime, tz=timezone.utc)
        except (ValueError, OSError):
            continue
        delta = (ts - created_utc).total_seconds()
        if -5 <= delta <= 60 and (best is None or abs(delta) < best_delta):
            best, best_delta = p, abs(delta)
    return best


def read_new_lines(path: Path, offset: int) -> tuple[list[str], int]:
    """Returns complete lines written since `offset`, and the new offset."""
    try:
        size = path.stat().st_size
        if size < offset:
            offset = 0
        if size == offset:
            return [], offset
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], offset
    chunk = data[: last_nl + 1]
    return chunk.decode("utf-8", "replace").splitlines(), offset + len(chunk)


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------
@dataclass
class Client:
    account: object
    place_id: int
    job_id: str | None
    pid: int | None = None
    state: str = "starting"  # starting | running | rejoining | waiting_retry | waiting_slot | gave_up | closed
    started: float = 0.0
    log_path: Path | None = None
    log_offset: int = 0
    log_lines: deque = field(default_factory=lambda: deque(maxlen=15))
    marker_at: float | None = None
    marker_name: str = ""
    misses: int = 0
    last_presence: float = 0.0
    retry_at: float = 0.0
    attempts: deque = field(default_factory=deque)
    cancelled: bool = False   # set by stop_account / a newer launch: abandon any rejoin in progress
    # Earlier PIDs of this same client. Roblox starts a NEW RobloxPlayerBeta.exe (child of the old
    # one) for a teleport and then closes the old one, so one account can own several PIDs over time.
    lineage: set = field(default_factory=set, repr=False)
    strict_job: str | None = None   # server this launch MUST reach; None = ordinary launch
    strict_ok: bool = False         # presence has confirmed the account is in strict_job
    strict_checked: float = 0.0
    patient: bool = False           # with strict_job: keep retrying that server instead of giving up
    waits: int = 0                  # how many times a patient join had to start over
    created_utc: datetime | None = None  # process start time, used to re-attach after an app restart
    # Private-server launch data (see private_links). In memory only, hidden from repr. `private`
    # stays True for an adopted client whose code was (deliberately) not saved; such a client is
    # never relaunched into a public server.
    extra: dict | None = field(default=None, repr=False)
    private: bool = False
    reported: tuple | None = None   # (place, job) last passed to on_server_fn, so each is reported once
    in_game: bool | None = None     # latest presence answer: in a game? None = not asked yet / could not tell
    last_location_poll: float = 0.0
    fetching: bool = False          # a presence request is running in the background
    fetched: tuple | None = field(default=None, repr=False)   # its answer, applied on the manager thread


class RejoinManager(threading.Thread):
    def __init__(self, store, launch_fn, list_pids_fn, kill_fn, log_fn=print,
                 log_dir: Path | None = None, creation_fn=process_creation_utc, presence_fn=None,
                 clock=time.monotonic, sleep=time.sleep, spawn=None,
                 notify_fn=None, on_ready_fn=None, family_fn=None, on_server_fn=None):
        super().__init__(daemon=True)
        self.store = store
        self.launch_fn = launch_fn      # (account, place_id, job_id) -> None, raises on failure
                                        # (private servers: also called with extra=<dict>)
        self.list_pids = list_pids_fn   # () -> set[int] of running game-client PIDs
        self.kill_fn = kill_fn          # (pid) -> None
        self.log = log_fn
        self.log_dir = log_dir or Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "logs"
        self.creation_fn = creation_fn
        self.presence_fn = presence_fn or self._presence
        self.clock, self.sleep = clock, sleep
        self.notify_fn = notify_fn      # (event, account_username, detail) -> None; see _emit
        self.on_ready_fn = on_ready_fn  # (client) -> None, once a client's process is being tracked
        self.on_server_fn = on_server_fn  # (account, place_id, job_id, game_name) -> None, once per server
                                          # a client is CONFIRMED in (server history)
        self.family_fn = family_fn      # () -> {pid: parent_pid} of running game clients (optional);
                                        # lets us follow a client that hands over to a new process
        self._spawn = spawn or (lambda fn, *a: threading.Thread(target=fn, args=a, daemon=True).start())
        self.clients: dict[str, Client] = {}
        self._lock = threading.RLock()
        self._launch_lock = threading.Lock()
        self._stop_event = threading.Event()

    # ---- public API ----------------------------------------------------
    def launch(self, account, place_id: int, job_id: str | None = None, extra: dict | None = None,
               strict: bool = False, patient: bool = False) -> Client:
        """Launch an account, wait until its client appears, and start tracking it.
        `extra` is private-server launch data (see private_links); it is kept in memory for
        rejoins and never saved. Raises whatever launch_fn raises (LaunchError)."""
        with self._launch_lock:
            private = bool(extra)
            client = Client(account, place_id, None if private else job_id, extra=extra or None, private=private)
            if strict and job_id and not private:
                client.strict_job = job_id
                client.patient = patient
            before = set(self.list_pids())
            if private:
                self.launch_fn(account, place_id, None, extra=extra)
            else:
                self.launch_fn(account, place_id, job_id)
            with self._lock:
                old = self.clients.get(account.id)
                if old is not None:
                    old.cancelled = True  # a stale tracker must not keep rejoining in parallel
                self.clients[account.id] = client
            self._await_pid(client, before)
            self.sleep(SETTLE_SECONDS)
        return client

    def hop(self, account, place_id: int, job_id: str | None, *, close_timeout: float = 20.0,
            strict: bool = False, patient: bool = False) -> Client:
        """Move an account to another server: close its current client COMPLETELY, then launch a new
        one into `job_id`. The two never run at the same time. Raises RuntimeError (and leaves the
        old client tracked and untouched in the manager) if the old client will not close, so an
        account is never left with two clients. If only the new launch fails, the account is handed
        to auto-rejoin (any server) when it has that switched on, otherwise it is left closed."""
        with self._lock:
            old = self.clients.get(account.id)
        if old is not None:
            old.cancelled = True            # the watcher and any rejoin in progress now ignore it
            pids = self._lineage_pids(old)
            self._kill_lineage(old)
            deadline = self.clock() + close_timeout
            while pids & set(self.list_pids()):
                if self.clock() >= deadline:
                    old.cancelled = False   # still running: keep watching it as before
                    raise RuntimeError(f"the old client (PID {', '.join(map(str, sorted(pids)))}) did not close; "
                                       "if Roblox runs as administrator, run this app as administrator too")
                self.sleep(0.5)
            self.log(f"[hop] {account.username}: old client closed")
        try:
            return self.launch(account, place_id, job_id, strict=strict, patient=patient)
        except Exception:
            if old is not None:
                with self._lock:
                    if self.clients.get(account.id) is old:
                        old.pid, old.job_id, old.cancelled = None, None, False
                        if account.autoRejoinEnabled:
                            old.state, old.retry_at = "waiting_retry", self.clock() + RETRY_DELAY_SECONDS
                        else:
                            old.state = "closed"
            raise

    def set_enabled(self, account, enabled: bool) -> None:
        account.autoRejoinEnabled = enabled
        self.store.save()

    # ---- process lineage (teleports start a new client process) ----------
    def _family(self) -> dict:
        if not self.family_fn:
            return {}
        try:
            return dict(self.family_fn())
        except Exception:
            return {}

    @staticmethod
    def _descendants(roots, family: dict) -> set:
        """PIDs in `family` whose parent chain leads to one of `roots` (roots themselves excluded)."""
        parents, out, grew = set(roots), set(), True
        while grew:
            grew = False
            for pid, ppid in family.items():
                if ppid in parents and pid not in parents:
                    parents.add(pid)
                    out.add(pid)
                    grew = True
        return out

    @staticmethod
    def _lineage_pids(c: Client) -> set:
        return {p for p in ({c.pid} | c.lineage) if p}

    def _follow_successor(self, c: Client) -> bool:
        """If Roblox handed this client over to a new process (teleport), track the new PID.
        Without this the old PID's exit looks like a disconnect (spurious rejoin) and Stop
        would kill only the dead PID, leaving the new client running."""
        family = self._family()
        if not family or not c.pid:
            return False
        mine = self._lineage_pids(c)
        with self._lock:
            claimed = {p for x in self.clients.values() if x is not c for p in self._lineage_pids(x)}
        kids = sorted(p for p, parent in family.items() if parent in mine and p not in mine and p not in claimed)
        if not kids:
            return False
        old, c.pid = c.pid, kids[0]
        c.lineage.add(old)
        c.log_path, c.log_offset = None, 0     # the new process writes a new log file
        c.marker_at, c.misses = None, 0
        c.started = c.last_presence = self.clock()   # fresh grace period while it loads
        c.in_game, c.last_location_poll, c.fetched = None, 0.0, None   # not confirmed in a server yet
        try:
            c.created_utc = self.creation_fn(c.pid)
        except Exception:
            c.created_utc = None
        self.log(f"[rejoin] {c.account.username}: client moved from PID {old} to PID {c.pid} "
                 "(Roblox started a new process, e.g. a teleport)")
        return True

    def _kill_lineage(self, c: Client) -> bool:
        """Close the client's current process AND any processes it handed over to. Returns True
        if something was closed. Logs loudly if a process survives (usually: Roblox runs as
        administrator but this app does not, so taskkill is refused)."""
        roots = self._lineage_pids(c)
        killed = False
        for _ in range(6):
            alive = set(self.list_pids())
            targets = (roots & alive) | (self._descendants(roots, self._family()) & alive)
            if not targets:
                break
            for pid in sorted(targets):
                self.kill_fn(pid)
                killed = True
            roots |= targets
            if not self.family_fn:       # no lineage info: one pass, as before
                break
            self.sleep(0.5)              # a successor may be starting right now; look again
        survivors = roots & set(self.list_pids())
        if survivors and killed:
            self.log(f"[!] {c.account.username}: could not close PID {', '.join(map(str, sorted(survivors)))}. "
                     "If Roblox runs as administrator, run this app as administrator too.")
        return killed

    def stop_account(self, account) -> bool:
        """Close an account's client on purpose (no auto-rejoin)."""
        with self._lock:
            client = self.clients.pop(account.id, None)
        if client:
            client.cancelled = True  # a rejoin already in progress checks this and backs out
        if client and self._kill_lineage(client):
            self.log(f"[rejoin] {account.username}: stopped on request (PID {client.pid})")
            return True
        return False

    def live_servers(self) -> dict:
        """Where accounts are right now: {(place_id, job_id): {"game": str, "names": [username, ...]}}.
        Only clients that are running AND whose server Roblox's presence has confirmed (the same fact
        that feeds the server history) are listed, so a launch that is still loading, waiting for a
        full server, rejoining or closed is not shown as being in a server. Reads memory only: no
        request is made."""
        out: dict = {}
        with self._lock:
            for c in self.clients.values():
                if c.state != "running" or c.cancelled or c.private or not c.job_id:
                    continue
                if c.reported != (c.place_id, c.job_id) or c.in_game is not True:
                    continue
                entry = out.setdefault((c.place_id, c.job_id), {"game": "", "names": []})
                entry["names"].append(c.account.username)
                entry["game"] = entry["game"] or getattr(c.account, "lastGameName", "") or ""
        return out

    def resume(self, account) -> bool:
        """Clear a 'gave up' state and try again now."""
        with self._lock:
            client = self.clients.get(account.id)
        if not client or client.state not in ("gave_up", "closed", "waiting_retry"):
            return False
        client.attempts.clear()
        client.state = "rejoining"
        self._spawn(self._rejoin, client)
        return True

    # ---- restart recovery (see sessions.py) -----------------------------
    def session_records(self) -> list[dict]:
        """What should survive an app restart: one JSON-safe dict per tracked client."""
        out = []
        with self._lock:
            for c in self.clients.values():
                if c.cancelled or c.state not in ("running", "starting", "rejoining", "waiting_retry"):
                    continue
                rec = {
                    "accountId": c.account.id, "placeId": c.place_id, "jobId": c.job_id, "pid": c.pid,
                    "createdUtc": c.created_utc.isoformat() if c.created_utc else None,
                }
                if c.private:
                    rec["private"] = True  # only the fact: the private-server code is never written to disk
                out.append(rec)
        return out

    def adopt(self, account, place_id: int, job_id: str | None, pid: int | None,
              created_iso: str | None, private: bool = False) -> bool:
        """Re-attach to a client that outlived an app restart. Refuses if the PID is gone or
        now belongs to a different process (recycled PID: creation times differ)."""
        if not pid or pid not in set(self.list_pids()):
            return False
        saved = None
        if created_iso:
            try:
                saved = datetime.fromisoformat(created_iso)
            except ValueError:
                saved = None
        try:
            actual = self.creation_fn(pid)
        except Exception:
            actual = None
        if saved and actual and abs((actual - saved).total_seconds()) > 2:
            return False
        with self._lock:
            if any(x.pid == pid for x in self.clients.values()):
                return False
            c = Client(account, place_id, job_id, private=private)
            c.pid, c.state = pid, "running"
            age = (datetime.now(timezone.utc) - actual).total_seconds() if actual else 0.0
            c.started = self.clock() - max(age, 0.0)
            c.created_utc = actual or saved
            c.last_presence = self.clock()
            self.clients[account.id] = c
        return True

    def recover(self, account, place_id: int, job_id: str | None) -> None:
        """Relaunch an account whose client died while the app was not running."""
        c = Client(account, place_id, job_id, state="rejoining")
        with self._lock:
            self.clients[account.id] = c
        self._spawn(self._rejoin, c)

    def status_lines(self) -> list[str]:
        now = self.clock()
        lines = []
        with self._lock:
            for c in self.clients.values():
                up = f"{now - c.started:.0f}s" if c.started else "-"
                on = "ON" if c.account.autoRejoinEnabled else "off"
                lines.append(f"  {c.account.username}: PID {c.pid or '-'} | {c.state} | up {up} | "
                             f"auto-rejoin {on} | attempts {len(c.attempts)}/{MAX_ATTEMPTS}")
        return lines

    def stop(self) -> None:
        self._stop_event.set()

    # ---- thread loop ----------------------------------------------------
    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:  # keep the watcher alive no matter what
                self.log(f"[rejoin] internal error: {exc!r}")
            self._stop_event.wait(1.0)

    def _tick(self) -> None:
        now = self.clock()
        alive = set(self.list_pids())
        with self._lock:
            clients = list(self.clients.values())
        for c in clients:
            try:  # one account's problem must never stop the others from being watched
                if c.cancelled:
                    continue
                if c.state == "running":
                    self._check_running(c, now, alive)
                elif c.state == "waiting_retry" and now >= c.retry_at:
                    c.state = "rejoining"
                    self._spawn(self._rejoin, c)
                elif c.state == "waiting_slot" and now >= c.retry_at:
                    c.retry_at = float("inf")       # the retry below sets the next time itself
                    self._spawn(self._retry_exact, c)
            except Exception as exc:
                self.log(f"[rejoin] {c.account.username}: internal error: {exc!r}")

    # ---- detection ------------------------------------------------------
    def _check_running(self, c: Client, now: float, alive: set) -> None:
        if self._follow_successor(c):
            alive = alive | {c.pid}      # the new process is running (it came from a fresh snapshot)
        if c.pid not in alive:
            self._on_disconnect(c, "client process exited")
            return
        uptime = now - c.started
        if self._check_strict(c, now, uptime):
            return
        if c.patient and c.strict_job and not c.strict_ok:
            return    # waiting to get in: leave the client alone (no log or presence "disconnect" checks)
        self._poll_log(c, now, uptime)

        if c.marker_at is not None and now - c.marker_at >= MARKER_DEBOUNCE_SECONDS:
            marker, c.marker_at = c.marker_name, None
            p = self.presence_fn(c.account)
            ptype = p.get("userPresenceType") if p else None
            if ptype is None:
                self.log(f"[rejoin] {c.account.username}: log marker '{marker}' but presence unavailable; ignoring")
            elif ptype != 2:
                self._on_disconnect(c, f"log marker '{marker}' and presence is {PRESENCE_NAMES.get(ptype, ptype)}")
                return
            else:
                self.log(f"[rejoin] {c.account.username}: log marker '{marker}' but still in game; ignoring")

        self._apply_fetched(c)
        regular = now - c.last_presence >= PRESENCE_POLL_SECONDS
        if not regular and uptime < LOCATION_START_SECONDS:
            return
        if regular or now - c.last_location_poll >= self._location_interval(c, uptime):
            if c.fetching:
                return
            if regular:
                c.last_presence = now
            c.last_location_poll = now
            c.fetching = True
            self._spawn(self._fetch_presence, c, uptime, regular)
            self._apply_fetched(c)       # (a synchronous spawn, as in the tests, has answered already)

    def _location_interval(self, c: Client, uptime: float) -> float:
        confirmed = c.reported == (c.place_id, c.job_id)
        fast = not confirmed and uptime < LOCATION_FAST_WINDOW
        return min(LOCATION_FAST_SECONDS if fast else LOCATION_LIVE_SECONDS, PRESENCE_POLL_SECONDS)

    def _fetch_presence(self, c: Client, uptime: float, regular: bool) -> None:
        """Runs in a background thread: only asks Roblox. Everything that changes state is done by
        _apply_fetched on the manager thread."""
        try:
            p = self.presence_fn(c.account)
        except Exception as exc:
            self.log(f"[rejoin] {c.account.username}: presence check failed: {exc!r}")
            p = None
        c.fetched = (p, uptime, regular)
        c.fetching = False

    def _apply_fetched(self, c: Client) -> None:
        got, c.fetched = c.fetched, None
        if got is None or c.state != "running" or c.cancelled:
            return
        p, uptime, regular = got
        if p is None:
            c.last_location_poll = self.clock() + LOCATION_BACKOFF_SECONDS - LOCATION_LIVE_SECONDS
            return
        self._poll_presence(c, uptime, p, regular)

    def _check_strict(self, c: Client, now: float, uptime: float) -> bool:
        """For a launch that must land in one specific server (friend join). Confirms it through
        presence; if the account is not there in time, closes the client for good. Returns True
        if it closed it."""
        if not c.strict_job or c.strict_ok:
            return False
        if uptime >= STRICT_FIRST_CHECK_SECONDS and now - c.strict_checked >= STRICT_POLL_SECONDS:
            c.strict_checked = now
            p = self.presence_fn(c.account)
            if p and p.get("userPresenceType") == 2 and p.get("gameId") == c.strict_job:
                c.strict_ok = True
                self._report_server(c, p)
                return False
        if uptime < STRICT_JOIN_SECONDS or c.patient:   # a patient join never times out
            return False
        self._abandon_strict(c, f"did not get into the requested server within {STRICT_JOIN_SECONDS}s "
                                "(Roblox refused the join, it is friends-only, or the server is full)")
        return True

    def _wait_for_slot(self, c: Client, reason: str) -> None:
        """Patient join whose client process is GONE before it got in: launch the SAME server again soon.
        Only called when the process has exited, never to close a waiting client. Runs until it gets in;
        Stop (stop_account) ends it. Not limited by MAX_ATTEMPTS."""
        c.waits += 1
        c.state = "waiting_slot"
        c.retry_at = self.clock() + PATIENT_RETRY_SECONDS
        self._kill_lineage(c)
        self.log(f"[join] {c.account.username}: {reason}. Launching that server again in "
                 f"{PATIENT_RETRY_SECONDS}s, and again until it gets in (try {c.waits}). Press Stop to end it.")

    def _retry_exact(self, c: Client) -> None:
        with self._launch_lock:
            if c.cancelled or self._stop_event.is_set() or c.state != "waiting_slot":
                return
            try:
                if self._kill_lineage(c):
                    for _ in range(20):
                        if not (self._lineage_pids(c) & set(self.list_pids())):
                            break
                        self.sleep(0.5)
                before = set(self.list_pids())
                self.launch_fn(c.account, c.place_id, c.strict_job)
            except Exception as exc:
                self.log(f"[join] {c.account.username}: retry failed - {exc}")
                if getattr(exc, "fatal", False):
                    c.state = "gave_up"
                    self._emit("rejoin_gaveup", c, f"cannot keep trying: {exc}")
                else:
                    c.retry_at = self.clock() + PATIENT_RETRY_SECONDS
                return
            c.job_id, c.strict_ok, c.strict_checked, c.misses = c.strict_job, False, 0.0, 0
            self._await_pid(c, before)
            if c.state != "running" and not c.cancelled:   # no client appeared: just try again later
                c.state, c.retry_at = "waiting_slot", self.clock() + PATIENT_RETRY_SECONDS
            self.sleep(SETTLE_SECONDS)

    def _abandon_strict(self, c: Client, reason: str) -> None:
        with self._lock:
            if self.clients.get(c.account.id) is c:
                del self.clients[c.account.id]
        c.cancelled = True            # no rejoin, no disconnect alert
        self._kill_lineage(c)
        self.log(f"[join] {c.account.username}: {reason}; closed it.")
        self._emit("rejoin_gaveup", c, reason)

    def _poll_presence(self, c: Client, uptime: float, p: dict, regular: bool = True) -> None:
        """Applies one presence answer. Only a `regular` poll can count as a miss (see LOCATION_*)."""
        c.in_game = p.get("userPresenceType") == 2
        if p.get("userPresenceType") == 2:
            place, job = p.get("placeId") or p.get("rootPlaceId"), p.get("gameId")
            if not regular and not c.reported and c.job_id and job and job != c.job_id and uptime < 90:
                c.in_game = None     # an early extra check still shows the server it was in BEFORE this join
                return
            c.misses = 0
            changed = False
            if place and int(place) != c.place_id:
                c.place_id, changed = int(place), True
            if job and job != c.job_id:
                c.job_id, changed = job, True
            self._report_server(c, p)
            acct = c.account
            if changed or acct.savedPlaceId != c.place_id or acct.savedJobId != c.job_id:
                acct.savedPlaceId, acct.savedJobId = c.place_id, c.job_id
                if p.get("lastLocation"):
                    acct.lastGameName = p["lastLocation"]
                self.store.save()
        elif regular and uptime >= GRACE_SECONDS:
            c.misses += 1
            if c.misses >= PRESENCE_MISS_LIMIT:
                ptype = p.get("userPresenceType")
                self._on_disconnect(c, f"presence says {PRESENCE_NAMES.get(ptype, ptype)}")

    def _report_server(self, c: Client, p: dict) -> None:
        """Tells on_server_fn which server presence CONFIRMS this client is in (once per server).
        Private servers are skipped: their server ID cannot be joined from the public flow."""
        if self.on_server_fn is None or c.private or p.get("userPresenceType") != 2:
            return
        place, job = p.get("placeId") or p.get("rootPlaceId"), p.get("gameId")
        try:
            place = int(place)
        except (TypeError, ValueError):
            return
        if not job or place <= 0 or c.reported == (place, job):
            return
        c.reported = (place, job)
        try:
            self.on_server_fn(c.account, place, job, str(p.get("lastLocation") or ""))
        except Exception as exc:
            self.log(f"[rejoin] on-server callback failed: {exc!r}")

    def _poll_log(self, c: Client, now: float, uptime: float) -> None:
        if c.log_path is None:
            if uptime > LOG_MAP_TIMEOUT:
                return
            created = self.creation_fn(c.pid)
            if not created:
                return
            with self._lock:
                claimed = {x.log_path for x in self.clients.values() if x.log_path and x is not c}
            path = find_log_for_process(self.log_dir, created, claimed)
            if path:
                c.log_path = path
                try:
                    c.log_offset = path.stat().st_size  # only watch what happens from now on
                except OSError:
                    c.log_offset = 0
                self.log(f"[rejoin] {c.account.username}: watching {path.name}")
            return
        lines, c.log_offset = read_new_lines(c.log_path, c.log_offset)
        for line in lines:
            c.log_lines.append(line)
            low = line.lower()
            if c.marker_at is None and uptime >= GRACE_SECONDS:
                for marker in DISCONNECT_MARKERS:
                    if marker in low:
                        c.marker_at, c.marker_name = now, marker
                        self._dump_debug(c, marker)
                        break

    def _dump_debug(self, c: Client, marker: str) -> None:
        """Save the log lines around a marker (long tokens redacted) so markers can be tuned."""
        try:
            DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(DEBUG_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} {c.account.username} "
                        f"marker '{marker}' ===\n")
                for line in c.log_lines:
                    f.write(re.sub(r"[A-Za-z0-9_.\-]{50,}", "<redacted>", line) + "\n")
        except OSError:
            pass

    # ---- reacting -------------------------------------------------------
    def _emit(self, event: str, c: Client, detail: str = "") -> None:
        """Tell the optional notify callback. A broken callback must never break rejoining."""
        if self.notify_fn is None:
            return
        try:
            self.notify_fn(event, c.account.username, detail)
        except Exception as exc:
            self.log(f"[rejoin] notify callback failed: {exc!r}")

    def _ready(self, c: Client) -> None:
        if self.on_ready_fn is None:
            return
        try:
            self.on_ready_fn(c)
        except Exception as exc:
            self.log(f"[rejoin] on-ready callback failed: {exc!r}")

    def _on_disconnect(self, c: Client, reason: str) -> None:
        if c.cancelled:  # stopped on purpose a moment ago: that is not a disconnect
            return
        if c.strict_job and not c.strict_ok:   # never reached the requested server: do not retry elsewhere
            if c.patient:
                self._wait_for_slot(c, f"closed before reaching the requested server ({reason})")
            else:
                self._abandon_strict(c, f"closed before reaching the requested server ({reason})")
            return
        self.log(f"[rejoin] {c.account.username}: disconnect detected - {reason}")
        self._emit("disconnect", c, reason + ("" if c.account.autoRejoinEnabled else " (auto-rejoin is off)"))
        if not c.account.autoRejoinEnabled:
            c.state = "closed"
            self.log(f"[rejoin] {c.account.username}: auto-rejoin is OFF for this account; leaving it closed")
            return
        c.state = "rejoining"
        self._spawn(self._rejoin, c)

    def _rejoin(self, c: Client) -> None:
        with self._launch_lock:
            if c.cancelled or self._stop_event.is_set():
                return
            if c.private and not c.extra:
                # Re-attached after an app restart: the private-server code was not kept, and
                # "any server" would drop this account into a public server.
                c.state = "gave_up"
                self.log(f"[rejoin] {c.account.username}: was in a private server and its code is never saved, "
                         "so it cannot rejoin it after an app restart. Launch it again from the Private tab "
                         "of the join tools.")
                self._emit("rejoin_gaveup", c, "private server cannot be rejoined after an app restart")
                return
            now = self.clock()
            while c.attempts and now - c.attempts[0] > ATTEMPT_WINDOW_SECONDS:
                c.attempts.popleft()
            if len(c.attempts) >= MAX_ATTEMPTS:
                c.state = "gave_up"
                self.log(f"[rejoin] {c.account.username}: gave up after {MAX_ATTEMPTS} attempts in "
                         f"{ATTEMPT_WINDOW_SECONDS // 60} min. Use 'resume' to try again.")
                self._emit("rejoin_gaveup", c, f"gave up after {MAX_ATTEMPTS} attempts in "
                                               f"{ATTEMPT_WINDOW_SECONDS // 60} min")
                return
            c.attempts.append(now)
            n = len(c.attempts)

            if self._kill_lineage(c):
                for _ in range(20):
                    if not (self._lineage_pids(c) & set(self.list_pids())):
                        break
                    self.sleep(0.5)

            self.sleep(REJOIN_COOLDOWN_SECONDS)
            if c.cancelled or self._stop_event.is_set():  # Stop / quit pressed during the cooldown
                self.log(f"[rejoin] {c.account.username}: rejoin cancelled")
                return
            private = bool(c.extra)
            # public: first try same server, later any server. private: always the same private server.
            job = None if private else (c.job_id if n == 1 else None)
            before = set(self.list_pids())
            try:
                if private:
                    self.launch_fn(c.account, c.place_id, None, extra=c.extra)
                else:
                    self.launch_fn(c.account, c.place_id, job)
            except Exception as exc:
                self.log(f"[rejoin] {c.account.username}: relaunch failed - {exc}")
                if getattr(exc, "fatal", False):
                    c.state = "gave_up"
                    self.log(f"[rejoin] {c.account.username}: giving up (this will not fix itself)")
                    self._emit("rejoin_gaveup", c, f"relaunch failed and will not fix itself: {exc}")
                    if getattr(exc, "reason", "") == "cookie_rejected":
                        self._emit("cookie_invalid", c, "Roblox rejected this account's cookie; re-add the account")
                else:
                    c.state, c.retry_at = "waiting_retry", self.clock() + RETRY_DELAY_SECONDS
                return
            c.job_id = job
            self.log(f"[rejoin] {c.account.username}: relaunched (attempt {n}/{MAX_ATTEMPTS}, "
                     f"{'same private server' if private else 'same server' if job else 'any server'})")
            self._await_pid(c, before)
            self.sleep(SETTLE_SECONDS)

    def _await_pid(self, c: Client, before: set) -> None:
        deadline = self.clock() + NEW_CLIENT_TIMEOUT
        while self.clock() < deadline and not self._stop_event.is_set():
            with self._lock:
                claimed = {p for x in self.clients.values() if x is not c for p in self._lineage_pids(x)}
            handed_over = self._descendants(claimed, self._family())   # another account's teleport, not our launch
            new = sorted(set(self.list_pids()) - before - claimed - handed_over)
            if new:
                if c.cancelled:  # Stop was pressed while this client was starting
                    self.kill_fn(new[0])
                    self.log(f"[rejoin] {c.account.username}: stopped while starting; closed PID {new[0]}")
                    return
                with self._lock:
                    c.pid, c.state, c.started = new[0], "running", self.clock()
                    c.log_path, c.log_offset = None, 0
                    c.marker_at, c.misses, c.last_presence = None, 0, self.clock()
                    c.in_game, c.last_location_poll, c.fetched = None, 0.0, None
                try:
                    c.created_utc = self.creation_fn(c.pid)
                except Exception:
                    c.created_utc = None
                self.log(f"[rejoin] {c.account.username}: tracking client PID {c.pid}")
                self._emit("launch", c, f"client started (PID {c.pid})")
                self._ready(c)
                return
            self.sleep(0.5)
        self.log(f"[rejoin] {c.account.username}: no new Roblox client appeared after launching")
        if c.account.autoRejoinEnabled:
            c.state, c.retry_at = "waiting_retry", self.clock() + RETRY_DELAY_SECONDS
        else:
            c.state = "closed"

    # ---- default presence lookup ---------------------------------------
    def _presence(self, account) -> dict | None:
        try:
            cookie = self.store.get_cookie(account)
        except OSError:
            return None
        try:
            return fetch_own_presence(cookie, account.userId)
        finally:
            del cookie