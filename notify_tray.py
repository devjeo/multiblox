"""
Tray channel: a Windows balloon/toast through the pystray icon.

No tray icon (pystray not installed, or the icon failed) means send() returns
False. There is deliberately no MessageBox fallback: a modal box would block
the worker thread.
"""

from __future__ import annotations

APP_TITLE = "Roblox Account Manager"


class TrayChannel:
    name = "tray"

    def __init__(self, icon_fn) -> None:
        self._icon_fn = icon_fn  # lazy: the tray is built after the notifier

    def send(self, n) -> bool:
        try:
            icon = self._icon_fn()
            if icon is None or not getattr(icon, "HAS_NOTIFICATION", True):
                return False
            icon.notify(f"{n.account}: {n.detail}".strip(": "), f"{APP_TITLE} - {n.event}")  # pystray: (message, title)
            return True
        except Exception:
            return False