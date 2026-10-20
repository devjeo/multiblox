"""
Server history: every server any account actually got into, all games together, newest first.
Saved to %APPDATA%\\RobloxAccountManager\\server_history.json.

A row is one (place, server/job ID). Several accounts in the same server share one row. Rows are
recorded when Roblox's presence confirms where a client is (see RejoinManager.on_server_fn), so a
launch that never got in is not listed, and random joins and teleports are listed too.

Only public facts are kept: game name, place ID, server ID, usernames of YOUR accounts, times.
No cookies. Pure logic, no Tk. Thread-safe (the manager thread writes, the UI thread reads).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import jsonio
from account_store import DATA_DIR

HISTORY_FILE = DATA_DIR / "server_history.json"
MAX_ROWS = 200
MAX_ACCOUNTS_PER_ROW = 12


@dataclass
class Row:
    place_id: int
    job_id: str
    game: str = ""
    accounts: list = field(default_factory=list)
    first_at: float = 0.0
    last_at: float = 0.0

    @property
    def key(self) -> tuple:
        return (self.place_id, self.job_id)


def _parse(item) -> Row | None:
    if not isinstance(item, dict):
        return None
    place, job = item.get("placeId"), item.get("jobId")
    if isinstance(place, bool) or not isinstance(place, int) or place <= 0 \
            or not isinstance(job, str) or len(job) < 8:
        return None
    accounts = [a for a in (item.get("accounts") or []) if isinstance(a, str) and a] \
        if isinstance(item.get("accounts"), list) else []

    def num(v) -> float:
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
    game = item.get("game")
    return Row(place, job, game if isinstance(game, str) else "", accounts[:MAX_ACCOUNTS_PER_ROW],
               num(item.get("firstAt")), num(item.get("lastAt")))


class ServerHistory:
    def __init__(self, path: Path = HISTORY_FILE, clock=time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self.rows: list[Row] = []
        self.load()

    def load(self) -> None:
        raw = jsonio.read_json(self.path, default=[])
        seen: set = set()
        rows: list[Row] = []
        for item in raw if isinstance(raw, list) else []:
            row = _parse(item)
            if row is not None and row.key not in seen:
                seen.add(row.key)
                rows.append(row)
        rows.sort(key=lambda r: r.last_at, reverse=True)
        with self._lock:
            self.rows = rows[:MAX_ROWS]

    def _save(self) -> None:
        payload = [{"placeId": r.place_id, "jobId": r.job_id, "game": r.game, "accounts": r.accounts,
                    "firstAt": r.first_at, "lastAt": r.last_at} for r in self.rows]
        try:
            jsonio.write_json_atomic(self.path, payload)
        except OSError:
            pass   # a convenience list: never let a full disk break a launch

    def add(self, place_id: int, job_id: str, game: str, username: str) -> bool:
        """Records that `username` is in this server. Returns True when the list changed."""
        if not isinstance(place_id, int) or place_id <= 0 or not isinstance(job_id, str) or len(job_id) < 8:
            return False
        now = self.clock()
        with self._lock:
            row = next((r for r in self.rows if r.key == (place_id, job_id)), None)
            if row is None:
                row = Row(place_id, job_id, game or "", [], now, now)
            else:
                self.rows.remove(row)
                if game:
                    row.game = game
            if username and username not in row.accounts:
                row.accounts.append(username)
                del row.accounts[:-MAX_ACCOUNTS_PER_ROW]
            row.last_at = now
            self.rows.insert(0, row)
            del self.rows[MAX_ROWS:]
            self._save()
        return True

    def list(self) -> list[Row]:
        with self._lock:
            return [Row(r.place_id, r.job_id, r.game, list(r.accounts), r.first_at, r.last_at) for r in self.rows]

    def get(self, place_id: int, job_id: str) -> Row | None:
        return next((r for r in self.list() if r.key == (place_id, job_id)), None)

    def remove(self, place_id: int, job_id: str, keep=()) -> bool:
        """Forgets one server. A server in `keep` (where an account is right now) is skipped.
        Returns True when the row was removed."""
        key = (place_id, job_id)
        if key in keep:
            return False
        with self._lock:
            before = len(self.rows)
            self.rows = [r for r in self.rows if r.key != key]
            self._save()
            return len(self.rows) != before

    def clear(self, keep=()) -> int:
        """Forgets every saved server except those in `keep` (a set of (place_id, job_id): the servers
        where your accounts are right now, which can not be cleared). Returns how many were removed."""
        with self._lock:
            kept = [r for r in self.rows if r.key in keep]
            removed = len(self.rows) - len(kept)
            self.rows = kept
            self._save()
        return removed
