"""
Phase 6: crash / restart recovery.

While the manager runs, the GUI saves "which account is in which game, in which
client process" to sessions.json. After the app (or the PC) restarts:

  * clients that are STILL running are re-attached ("adopted"), so auto-rejoin
    keeps watching them and nothing has to be closed or relaunched;
  * clients that are gone are relaunched ONLY if the last run did not exit
    cleanly (a crash / power loss / killed process) and that account has
    auto-rejoin ON. After a normal quit they are left alone - you quit on
    purpose.

Stored in: %APPDATA%\\RobloxAccountManager\\sessions.json
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from account_store import DATA_DIR

SESSIONS_FILE = DATA_DIR / "sessions.json"


class SessionStore:
    def __init__(self, path: Path = SESSIONS_FILE):
        self.path = Path(path)
        self._last: str | None = None  # serialized form of the last write, to skip no-op writes

    def load(self) -> tuple[list[dict], bool]:
        """Returns (records, exited_cleanly). Missing or unreadable file = ([], True)."""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            records = [r for r in data.get("sessions", []) if isinstance(r, dict) and r.get("accountId")]
            return records, bool(data.get("clean", False))
        except (OSError, ValueError, AttributeError):
            return [], True

    def save(self, records: list[dict], clean: bool = False) -> bool:
        """Writes the file if anything changed. Returns True if it wrote."""
        payload = json.dumps({"version": 1, "clean": clean, "sessions": records}, indent=2, sort_keys=True)
        if payload == self._last:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.path)
        except OSError:
            return False  # best effort: never crash the app over a bookkeeping file
        self._last = payload
        return True


def restore(manager, store, records: list[dict], clean: bool,
            rejoin_after_crash: bool = True, log=print) -> dict:
    """Re-attach to surviving clients and (after an unclean exit) relaunch dead ones.
    Returns {"adopted": n, "recovered": n, "dropped": n}."""
    counts = {"adopted": 0, "recovered": 0, "dropped": 0}
    by_id = {a.id: a for a in store.accounts}
    for rec in records:
        account = by_id.get(rec.get("accountId"))
        place_id = rec.get("placeId")
        if account is None or not place_id:
            counts["dropped"] += 1
            continue
        private = bool(rec.get("private"))
        if manager.adopt(account, int(place_id), rec.get("jobId"), rec.get("pid"), rec.get("createdUtc"),
                         private=private):
            counts["adopted"] += 1
            log(f"[restore] {account.username}: client still running (PID {rec.get('pid')}); watching it again")
        elif private:
            counts["dropped"] += 1
            log(f"[restore] {account.username}: its private-server client is gone; not relaunching it "
                "(private-server codes are never saved, and a relaunch would land in a public server)")
        elif not clean and rejoin_after_crash and account.autoRejoinEnabled:
            counts["recovered"] += 1
            log(f"[restore] {account.username}: client is gone and the last run did not exit cleanly; relaunching")
            manager.recover(account, int(place_id), rec.get("jobId"))
        else:
            counts["dropped"] += 1
    return counts