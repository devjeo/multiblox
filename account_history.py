"""
Per-account server history: for EACH of your accounts, the servers it got into and when,
newest first. Saved to %APPDATA%\\RobloxAccountManager\\account_history.json, keyed by user ID.

Recorded from the same presence confirmation as server_history.py, so a visit exists only after
Roblox confirmed the account is in that server. Re-entering the same server later adds a new visit
(so you see every time), but a repeated report of the same server within a session is ignored.

Only public facts are kept: game name, place ID, server ID, time. No cookies. Pure logic, no Tk.
Thread-safe (the manager thread writes, the UI thread reads).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import jsonio
from account_store import DATA_DIR

HISTORY_FILE = DATA_DIR / "account_history.json"
MAX_VISITS_PER_ACCOUNT = 50


@dataclass(frozen=True)
class Visit:
    place_id: int
    job_id: str
    game: str = ""
    at: float = 0.0


def _parse(item) -> Visit | None:
    if not isinstance(item, dict):
        return None
    place, job = item.get("placeId"), item.get("jobId")
    if isinstance(place, bool) or not isinstance(place, int) or place <= 0 \
            or not isinstance(job, str) or len(job) < 8:
        return None
    at = item.get("at")
    game = item.get("game")
    return Visit(place, job, game if isinstance(game, str) else "",
                 float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0.0)


class AccountHistory:
    def __init__(self, path: Path = HISTORY_FILE, clock=time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self.visits: dict[int, list[Visit]] = {}
        self.load()

    def load(self) -> None:
        raw = jsonio.read_json(self.path, default={})
        loaded: dict[int, list[Visit]] = {}
        for key, items in (raw if isinstance(raw, dict) else {}).items():
            try:
                uid = int(key)
            except (TypeError, ValueError):
                continue
            vs = [v for v in (_parse(i) for i in (items if isinstance(items, list) else [])) if v is not None]
            vs.sort(key=lambda v: v.at, reverse=True)
            loaded[uid] = vs[:MAX_VISITS_PER_ACCOUNT]
        with self._lock:
            self.visits = loaded

    def _save(self) -> None:
        payload = {str(uid): [{"placeId": v.place_id, "jobId": v.job_id, "game": v.game, "at": v.at} for v in vs]
                   for uid, vs in self.visits.items()}
        try:
            jsonio.write_json_atomic(self.path, payload)
        except OSError:
            pass   # a convenience list: never let a full disk break a launch

    def add(self, user_id: int, place_id: int, job_id: str, game: str = "") -> bool:
        """Records a visit. Returns False when ignored (bad input, or same server as the latest visit)."""
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0 \
                or not isinstance(place_id, int) or place_id <= 0 \
                or not isinstance(job_id, str) or len(job_id) < 8:
            return False
        with self._lock:
            vs = self.visits.setdefault(user_id, [])
            if vs and (vs[0].place_id, vs[0].job_id) == (place_id, job_id):
                return False
            vs.insert(0, Visit(place_id, job_id, game or "", self.clock()))
            del vs[MAX_VISITS_PER_ACCOUNT:]
            self._save()
        return True

    def list(self, user_id: int) -> list[Visit]:
        with self._lock:
            return list(self.visits.get(user_id, []))

    def latest(self, user_id: int) -> Visit | None:
        vs = self.list(user_id)
        return vs[0] if vs else None

    def clear(self, user_id: int | None = None, keep=()) -> None:
        """Forgets one account's history, or everyone's with no argument. Visits to a server in `keep`
        (a set of (place_id, job_id): where the account is right now) are skipped and stay."""
        def survivors(vs: list[Visit]) -> list[Visit]:
            return [v for v in vs if (v.place_id, v.job_id) in keep]
        with self._lock:
            targets = list(self.visits) if user_id is None else [user_id]
            for uid in targets:
                left = survivors(self.visits.get(uid, []))
                if left:
                    self.visits[uid] = left
                else:
                    self.visits.pop(uid, None)
            self._save()
