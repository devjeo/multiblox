"""
Notifier: decides WHICH events are worth telling you about and hands them to the
channels (Discord, tray). Pure logic: no Tk, no Windows, no network of its own.

  * notify() never blocks: it queues the event for a Worker thread.
  * An event is dropped if it is not enabled, or if the same (event, account)
    was already sent within the cooldown. force=True skips both checks (the
    Test button).
  * Each channel call is wrapped: one failing channel never stops the others or
    the worker. Everything logged passes through safelog.redact().

A channel is any object with a `send(notification) -> bool` method that never
raises. `clock` is a monotonic float callable (same one the RejoinManager uses).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from safelog import redact
from workers import Worker

EVENT_LABELS = {
    "disconnect": "A client disconnects",
    "rejoin_gaveup": "Auto-rejoin gives up",
    "cookie_invalid": "A cookie is rejected (account must be re-added)",
    "launch": "A client starts (can be noisy)",
}


@dataclass(frozen=True)
class Notification:
    event: str
    account: str
    detail: str
    at: datetime


class Notifier:
    def __init__(self, clock, channels, events, cooldown_sec, log_fn=print, now_fn=None) -> None:
        self.clock = clock
        self._now = now_fn or datetime.now
        self._log_fn = log_fn
        self._lock = threading.RLock()
        self._channels: dict = dict(channels or {})
        self._events: set[str] = {e for e in (events or ()) if isinstance(e, str)}
        self._cooldown = float(cooldown_sec)
        self._last: dict[tuple[str, str], float] = {}
        self.last_status: dict[str, bool] = {}   # per channel name, for diagnostics
        self._worker = Worker("notifier", self._handle_item, self._log)

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        self._worker.start()

    def stop(self) -> None:
        self._worker.stop()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        return self._worker.wait_idle(timeout)

    def reconfigure(self, channels, events, cooldown_sec) -> None:
        """Thread-safe; call after the settings are saved."""
        with self._lock:
            self._channels = dict(channels or {})
            self._events = {e for e in (events or ()) if isinstance(e, str)}
            self._cooldown = float(cooldown_sec)

    # ---- API ------------------------------------------------------------
    def notify(self, event: str, account: str, detail: str = "", *, force: bool = False) -> None:
        """Non-blocking: queues the event and returns immediately."""
        n = Notification(event, account, detail, self._now())
        self._worker.submit((n, force))

    # ---- worker side ----------------------------------------------------
    def _log(self, message: str) -> None:
        try:
            self._log_fn(redact(message))
        except Exception:
            pass

    def _handle_item(self, item) -> None:
        self._handle(*item)

    def _handle(self, n: Notification, force: bool = False) -> None:
        with self._lock:
            channels = list(self._channels.items())
            if not force:
                if n.event not in self._events:
                    return
                key, now = (n.event, n.account), self.clock()
                last = self._last.get(key)
                if last is not None and now - last < self._cooldown:
                    return
                self._last[key] = now
                if len(self._last) > 500:  # forget entries whose cooldown is long over
                    self._last = {k: t for k, t in self._last.items() if now - t < self._cooldown}
        sent = []
        for name, channel in channels:  # outside the lock: sending can be slow
            reason = ""
            try:
                ok = bool(channel.send(n))
            except Exception as exc:
                ok, reason = False, repr(exc)
            previous = self.last_status.get(name)
            self.last_status[name] = ok
            if ok:
                sent.append(name)
            elif previous is not False:  # say so once, not on every event while it stays broken
                self._log(f"[notify] {name}: could not send '{n.event}'" + (f" ({reason})" if reason else ""))
        if sent:
            self._log(f"[notify] sent '{n.event}' for {n.account} via {', '.join(sent)}")