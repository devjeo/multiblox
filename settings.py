"""
App settings, saved to %APPDATA%\\RobloxAccountManager\\settings.json.

Moved out of gui.py so that it (and anything that reads settings) can be
imported and tested without Tk or Windows. Behaviour is unchanged:

  * a flat dict of camelCase keys;
  * a saved value is only used if its type matches the default's type, so an
    old or hand-edited file can never break the app;
  * unknown keys in the file are ignored (and dropped on the next save);
  * apply() pushes the tunables into autorejoin and multi_instance, which read
    their module globals live.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import autorejoin
import server_finder
from account_store import DATA_DIR

SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULTS = {
    "autoEnableMultiInstance": True,
    "closeToTray": True,
    "confirmActions": True,      # ask before stopping clients, bulk launches and similar
    "theme": "dark",             # dark | light (see theme.py)
    "rejoinCooldown": 8,         # autorejoin.REJOIN_COOLDOWN_SECONDS
    "maxAttempts": 5,            # autorejoin.MAX_ATTEMPTS
    "attemptWindowMinutes": 10,  # autorejoin.ATTEMPT_WINDOW_SECONDS / 60
    "presencePollSeconds": 45,   # autorejoin.PRESENCE_POLL_SECONDS
    "launchPauseSeconds": 6,     # autorejoin.SETTLE_SECONDS (pause after each launch)
    "restoreSessions": True,     # re-attach to surviving clients on startup
    "rejoinAfterCrash": True,    # relaunch dead ones after an unclean exit (auto-rejoin accounts only)
    "mutexNames": "ROBLOX_singletonMutex",  # comma separated; change if a Roblox update renames them
    "eventNames": "ROBLOX_singletonEvent",
    # Notifications (see notifier.py). The Discord webhook URL itself is NOT here: it is a
    # secret and lives encrypted in secrets.json (secrets_store).
    "notifyEvents": ["disconnect", "rejoin_gaveup", "cookie_invalid"],
    "notifyDiscord": False,
    "notifyTray": True,
    "notifyCooldownSeconds": 120,  # same event for the same account is sent at most this often
    # Default game per account group: {"farm": {"placeId": 12345, "jobId": None}}
    "groupGames": {},
    # Resource saver (see resourcesaver.py): client flags written before each launch.
    "performanceProfile": "Normal",       # Normal | Low | Minimum
    "applyProfileBeforeLaunch": False,
    # Window layout (see winlayout.py)
    "layoutMonitor": 0,        # 0 = first monitor (the primary one)
    "layoutColumns": 3,
    "layoutGap": 4,
    "layoutAutoTile": False,
    # Scheduler (see scheduler.py)
    "schedulerEnabled": True,
    "scheduleMissedWindowMinutes": 30,   # a run missed while asleep/closed still fires within this long
    # Server finder cache + hopper (see server_finder.py, server_hopper.py)
    "serverCacheSeconds": 120,   # a game's server list is reused this long before Roblox is asked again
    "hopIntervalMinutes": 30,    # default "hop every" in the Server hopper tab
    "hopMode": "best",           # which server the hopper picks (a server_finder mode)
    "hopMinPlayers": 0,
    # Hotkey macros (see macros.py, macro_engine.py)
    "macrosEnabled": True,       # master switch: off = macro hotkeys do nothing (the stop-all hotkey still works)
    "macroMaxRunMinutes": 60,    # a macro that is still running after this long stops by itself
}


class Settings(dict):
    def __init__(self, path: Path | None = None, apply_now: bool = True) -> None:
        # deepcopy: DEFAULTS holds a dict, and every Settings must get its own copy of it
        super().__init__(copy.deepcopy(DEFAULTS))
        self.path = Path(path) if path else SETTINGS_FILE
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for key, default in DEFAULTS.items():
                if key in raw and type(raw[key]) is type(default):
                    self[key] = raw[key]
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        if apply_now:
            self.apply()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(self), f, indent=2)
        os.replace(tmp, self.path)

    def apply(self) -> None:
        """Push the tunables into autorejoin (it reads these module globals live)."""
        autorejoin.REJOIN_COOLDOWN_SECONDS = self["rejoinCooldown"]
        autorejoin.MAX_ATTEMPTS = self["maxAttempts"]
        autorejoin.ATTEMPT_WINDOW_SECONDS = self["attemptWindowMinutes"] * 60
        autorejoin.PRESENCE_POLL_SECONDS = self["presencePollSeconds"]
        autorejoin.SETTLE_SECONDS = self["launchPauseSeconds"]
        server_finder.cache.ttl = max(0, self["serverCacheSeconds"])
        if sys.platform != "win32":
            return  # multi_instance needs Windows; nothing to push elsewhere
        import multi_instance
        split = lambda text: [n.strip() for n in text.split(",") if n.strip()]
        multi_instance.set_singleton_names(split(self["mutexNames"]), split(self["eventNames"]))