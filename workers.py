"""
Worker: one daemon thread that runs queued items one at a time.

Used so slow work (a Discord POST) never blocks the thread that asked for it.
An exception inside `on_item` is logged and the loop carries on.
"""

from __future__ import annotations

import queue
import threading

_STOP = object()


class Worker:
    def __init__(self, name: str, on_item, log_fn=print) -> None:
        self.name = name
        self._on_item = on_item
        self._log = log_fn
        self._q: queue.Queue = queue.Queue()
        self._cv = threading.Condition()
        self._pending = 0
        self._thread: threading.Thread | None = None
        self._stopping = False

    def start(self) -> None:
        with self._cv:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
            self._thread.start()

    def submit(self, item) -> bool:
        """Queues `item`. Returns False (and drops it) once stop() has been called."""
        with self._cv:
            if self._stopping:
                return False
            self._pending += 1
        self._q.put(item)
        return True

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Blocks until every submitted item has been handled (mainly for tests)."""
        with self._cv:
            return self._cv.wait_for(lambda: self._pending == 0, timeout)

    def stop(self, timeout: float = 2.0) -> None:
        """Handles what is already queued, then ends the thread (waits up to `timeout`)."""
        with self._cv:
            self._stopping = True
            thread = self._thread
        self._q.put(_STOP)
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        while True:
            item = self._q.get()
            if item is _STOP:
                return
            try:
                self._on_item(item)
            except Exception as exc:  # one bad item must never kill the worker
                try:
                    self._log(f"[worker {self.name}] error: {exc!r}")
                except Exception:
                    pass
            finally:
                with self._cv:
                    self._pending -= 1
                    self._cv.notify_all()