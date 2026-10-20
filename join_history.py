"""
Join history: the users you looked up or joined before, newest first, so the Join panel can
offer them as one-click targets. Saved to %APPDATA%\\RobloxAccountManager\\join_history.json.

Only public facts are kept (user ID, username, display name, last-used time). No cookies, no
server IDs: where a user is gets asked of Roblox again every time. Pure logic, no Tk.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import jsonio
from account_store import DATA_DIR

HISTORY_FILE = DATA_DIR / "join_history.json"
MAX_ENTRIES = 30


@dataclass(frozen=True)
class Entry:
    user_id: int
    name: str
    display_name: str = ""
    last_used: float = 0.0

    @property
    def title(self) -> str:
        if self.display_name and self.display_name != self.name:
            return f"{self.display_name} (@{self.name})"
        return self.name


def _parse(item) -> Entry | None:
    """One saved record, or None when it is damaged (a bad file never crashes the app)."""
    if not isinstance(item, dict):
        return None
    uid, name = item.get("userId"), item.get("name")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0 or not isinstance(name, str) or not name:
        return None
    display = item.get("displayName")
    used = item.get("lastUsed")
    return Entry(uid, name, display if isinstance(display, str) else "",
                 float(used) if isinstance(used, (int, float)) and not isinstance(used, bool) else 0.0)


class JoinHistory:
    def __init__(self, path: Path = HISTORY_FILE, clock=time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.entries: list[Entry] = []
        self.load()

    def load(self) -> None:
        raw = jsonio.read_json(self.path, default=[])
        items = raw if isinstance(raw, list) else []
        seen: set[int] = set()
        loaded: list[Entry] = []
        for item in items:
            entry = _parse(item)
            if entry is not None and entry.user_id not in seen:
                seen.add(entry.user_id)
                loaded.append(entry)
        loaded.sort(key=lambda e: e.last_used, reverse=True)
        self.entries = loaded[:MAX_ENTRIES]

    def _save(self) -> None:
        payload = [{"userId": e.user_id, "name": e.name, "displayName": e.display_name, "lastUsed": e.last_used}
                   for e in self.entries]
        try:
            jsonio.write_json_atomic(self.path, payload)
        except OSError:
            pass   # history is a convenience; failing to save it must never break a join

    def add(self, user_id: int, name: str, display_name: str = "") -> None:
        """Puts the user at the top (a repeat is moved up, not duplicated) and trims the list."""
        if not isinstance(user_id, int) or user_id <= 0 or not name:
            return
        entry = Entry(user_id, name, display_name or "", self.clock())
        self.entries = [entry] + [e for e in self.entries if e.user_id != user_id]
        del self.entries[MAX_ENTRIES:]
        self._save()

    def remove(self, user_id: int) -> None:
        self.entries = [e for e in self.entries if e.user_id != user_id]
        self._save()

    def clear(self) -> None:
        self.entries = []
        self._save()

    def get(self, user_id: int) -> Entry | None:
        return next((e for e in self.entries if e.user_id == user_id), None)
