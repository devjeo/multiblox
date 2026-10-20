"""
Macro engine: runs macros when their hotkey is pressed.

No tkinter and no Win32 here. The mouse comes from a WinApi (winapi.py), the hotkeys from a
HotkeyListener (hotkeys.py); both are injected, so tests use FakeWinApi and FakeHotkeys.

Flow:  hotkey pressed -> on_hotkey(name) -> macro idle?  -> start it on its own worker thread
                                         -> macro running? -> stop it (toggle)

Each running macro has one worker thread and one stop Event. Safeguards:
  * master switch (`enabled_fn`): off means hotkeys do nothing and running macros stop;
  * the interval never goes below MIN_INTERVAL_MS, whatever the data says;
  * a run ends by itself after `max_run_fn()` seconds (a macro left running by accident);
  * the panic hotkey (macros.PANIC_HOTKEY) stops everything and works even when the switch is off;
  * shutdown() stops every worker and waits for it (the app calls it on quit).

lastRun and runCount are saved when a run STARTS (like the scheduler does), so a crash never loses them.

Recorded macros (phase 2) use the same start/stop toggle and safeguards. One run replays the recording
with its original timing (divided by the speed) and `repeat` counts replays, `intervalMs` is the pause
between them. Stopping is checked between events (and during every wait); keys and mouse buttons the
replay pressed are always released when it ends, so nothing is left held down.

Recording: start_recording() / finish_recording() drive a recorder.InputRecorder. While it records, the
macro hotkeys are released (the stop-all hotkey stays and also ends the recording) and running macros stop.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import macros as mc

PANIC_NAME = "__panic__"
_USER_STOP = "stopped"      # internal: a pass was cut short by the stop flag
DEFAULT_MAX_RUN_SECONDS = 3600
TEST_DELAY_SECONDS = 3.0     # "Test click": time to move the mouse to where the click should happen


@dataclass
class _Run:
    macro: mc.Macro                  # a copy, so editing the saved macro does not change a run in progress
    stop: threading.Event
    thread: threading.Thread | None = None
    clicks: int = 0                  # clicks done (recorded macros: replays done)
    test: bool = False
    held_keys: set = dataclasses.field(default_factory=set)       # pressed by a replay, not yet released
    held_buttons: set = dataclasses.field(default_factory=set)


def _clicks(n: int, unit: str = "click") -> str:
    return f"{n} {unit}" + ("" if n == 1 else "s")


def _default_wait(stop: threading.Event, seconds: float) -> bool:
    """Sleeps up to `seconds`; True if stop was set meanwhile."""
    return stop.wait(seconds)


class MacroEngine:
    def __init__(self, macros, store, api_fn, log_fn=print, *, listener=None, enabled_fn=lambda: True,
                 max_run_fn=lambda: DEFAULT_MAX_RUN_SECONDS, now_fn=datetime.now, clock=time.monotonic,
                 wait_fn=_default_wait, on_change=None, recorder_fn=lambda: None) -> None:
        self.macros: list[mc.Macro] = macros
        self.store, self.api_fn, self.log = store, api_fn, log_fn
        self.listener = listener
        if listener is not None:
            listener.on_press = self.on_hotkey
        self.enabled_fn, self.max_run_fn = enabled_fn, max_run_fn
        self.now_fn, self.clock, self.wait_fn = now_fn, clock, wait_fn
        self.on_change = on_change                      # called (any thread) when a run starts or ends
        self.recorder_fn = recorder_fn                  # returns a new recorder.InputRecorder, or None
        self._recorder = None                           # the recorder that is recording now
        self._recorded: list[dict] | None = None        # events of a recording the stop-all hotkey ended
        self._resync_pending = False                    # macro hotkeys still to be put back after that
        self.errors: dict[str, str] = {}                # macro id -> hotkey problem (shown in the dashboard)
        self.last_results: dict[str, str] = {}          # macro id -> short outcome of its last run
        self._runs: dict[str, _Run] = {}
        self._lock = threading.RLock()
        self._closed = False
        self._panic_failed = False

    # ---- hotkeys --------------------------------------------------------
    def start_hotkeys(self) -> None:
        """Starts the listener and registers every enabled macro's hotkey plus the panic hotkey."""
        if self.listener is None:
            return
        self.listener.start()
        self.sync_hotkeys()

    def sync_hotkeys(self) -> dict[str, str]:
        """Registers what should be registered now. Returns (and remembers) {macro id: error}."""
        if self.listener is None:
            return {}
        with self._lock:
            wanted = {PANIC_NAME: mc.PANIC_HOTKEY}
            if self._recorder is None:       # while recording, only the stop-all hotkey stays registered
                wanted.update({m.id: m.hotkey for m in self.macros if m.enabled})
        failed = self.listener.sync(wanted)
        with self._lock:
            before, self.errors = self.errors, {k: v for k, v in failed.items() if k != PANIC_NAME}
        if PANIC_NAME in failed and not self._panic_failed:
            self.log(f"[!] Macros: the stop-all hotkey {mc.format_hotkey(mc.PANIC_HOTKEY)} could not be "
                     f"registered ({failed[PANIC_NAME]})")
        self._panic_failed = PANIC_NAME in failed
        for macro in list(self.macros):       # log a problem once, not on every re-sync
            if macro.id in self.errors and before.get(macro.id) != self.errors[macro.id]:
                self.log(f"[!] Macros: hotkey {mc.format_hotkey(macro.hotkey)} for '{macro.name}': "
                         f"{self.errors[macro.id]}")
        self._changed()
        return dict(self.errors)

    def suspend_hotkeys(self) -> None:
        """Releases every macro hotkey (the stop-all hotkey stays). The dashboard calls this while the user
        is pressing keys to choose a hotkey, so pressing an existing combination reaches the form instead
        of toggling that macro. sync_hotkeys() puts them back."""
        if self.listener is not None:
            self.listener.sync({PANIC_NAME: mc.PANIC_HOTKEY})

    def on_hotkey(self, name: str) -> str:
        """A registered hotkey was pressed. Returns what happened (for tests and logs):
        'panic', 'started', 'stopped', 'disabled' (master switch off), 'ignored'."""
        if name == PANIC_NAME:
            n = self.stop_all()
            if self._recorder is not None:           # the stop-all hotkey also ends a recording
                # This runs on the hotkey thread, which sync_hotkeys() has to talk to, so the macro hotkeys are
                # put back later, by finish_recording().
                self._recorded = self._end_recording(resync=False)
                self.log("[macros] stop-all hotkey pressed (recording ended)")
            else:
                self.log(f"[macros] stop-all hotkey pressed ({n} stopped)")
            return "panic"
        if self._closed or not self.enabled_fn():
            return "disabled"
        macro = self._find(name)
        if macro is None or not macro.enabled:
            return "ignored"
        with self._lock:
            running = name in self._runs
        if running:
            self.stop(name)
            return "stopped"
        return "started" if self._start(macro) else "ignored"

    # ---- editing (the dashboard calls these; each saves and re-registers hotkeys) ----
    def add(self, macro: mc.Macro) -> None:
        with self._lock:
            self.macros.append(macro)
        self._saved()

    def replace(self, old: mc.Macro, new: mc.Macro) -> None:
        self.stop(old.id)                      # an edit never changes a run in progress: it ends it
        with self._lock:
            self.macros[self.macros.index(old)] = new
        self._saved()

    def remove(self, macro: mc.Macro) -> None:
        self.stop(macro.id)
        with self._lock:
            if macro in self.macros:
                self.macros.remove(macro)
            self.last_results.pop(macro.id, None)
        self._saved()

    def set_enabled(self, macro: mc.Macro, enabled: bool) -> None:
        if not enabled:
            self.stop(macro.id)
        macro.enabled = enabled
        self._saved()

    def _saved(self) -> None:
        self.save()
        self.sync_hotkeys()

    def save(self) -> None:
        with self._lock:
            try:
                self.store.save(self.macros)
            except OSError as exc:
                self.log(f"[!] Macros: could not save macros.json ({exc}).")

    # ---- running --------------------------------------------------------
    def is_running(self, macro_id: str) -> bool:
        with self._lock:
            return macro_id in self._runs

    def running_ids(self) -> list[str]:
        with self._lock:
            return list(self._runs)

    def status(self, macro: mc.Macro) -> str:
        """Short text for the dashboard's Status column."""
        with self._lock:
            run = self._runs.get(macro.id)
        if run is not None:
            if run.test:
                return "Testing..."
            return f"Running (replay {run.clicks + 1})" if run.macro.type == "recorded" else f"Running ({run.clicks})"
        if macro.id in self.errors:
            return "Hotkey problem: " + self.errors[macro.id]
        if not macro.enabled:
            return "Off"
        return self.last_results.get(macro.id, "Ready")

    def test_click(self, macro: mc.Macro, delay: float = TEST_DELAY_SECONDS) -> bool:
        """One click as the macro would do it (moves first for 'click at position'), or one replay of a
        recorded macro, after `delay` seconds so the mouse can be put where it should start. Does not
        touch lastRun/runCount. False if the macro is already running."""
        return self._start(macro, once=True, delay=delay)

    def _start(self, macro: mc.Macro, once: bool = False, delay: float = 0.0) -> bool:
        with self._lock:
            if self._closed or macro.id in self._runs:
                return False
            snapshot = dataclasses.replace(macro, events=list(macro.events))
            if once:
                snapshot.repeat = 1
            run = _Run(snapshot, threading.Event(), test=once)
            self._runs[macro.id] = run
            if not once:
                macro.lastRun = self.now_fn().isoformat(timespec="seconds")
                macro.runCount += 1
        if not once:
            self.save()                         # before the run, outside the lock: file I/O
            self.log(f"[macros] started: {mc.describe(macro)}")
        run.thread = threading.Thread(target=self._work, args=(run, delay), name=f"macro-{macro.name[:20]}",
                                      daemon=True)
        run.thread.start()
        self._changed()
        return True

    def stop(self, macro_id: str) -> bool:
        """Tells a running macro to stop (it ends within one interval). False if it was not running."""
        with self._lock:
            run = self._runs.get(macro_id)
        if run is None:
            return False
        run.stop.set()
        return True

    def stop_all(self) -> int:
        with self._lock:
            runs = list(self._runs.values())
        for run in runs:
            run.stop.set()
        return len(runs)

    def shutdown(self, timeout: float = 2.0) -> None:
        """App quit: nothing starts any more, every worker is stopped and waited for, hotkeys released."""
        with self._lock:
            self._closed = True
            runs = list(self._runs.values())
        for run in runs:
            run.stop.set()
        if self._recorder is not None:
            self._end_recording(resync=False)
        end = self.clock() + timeout
        for run in runs:
            if run.thread is not None and run.thread is not threading.current_thread():
                run.thread.join(max(0.0, end - self.clock()))
        if self.listener is not None:
            self.listener.stop()

    # ---- recording ------------------------------------------------------
    def start_recording(self, own_hotkey: str | None = None) -> str | None:
        """Starts recording mouse and keyboard input. `own_hotkey`: the hotkey of the macro being recorded
        (not recorded). Returns None when recording started, otherwise a message for the user."""
        with self._lock:
            if self._closed:
                return "The app is closing."
            if self._recorder is not None:
                return "A recording is already running."
        skip = [mc.parse_hotkey(mc.PANIC_HOTKEY)]
        if own_hotkey:
            try:
                skip.append(mc.parse_hotkey(own_hotkey))
            except ValueError:
                pass
        try:
            recorder = self.recorder_fn()
        except Exception as exc:
            recorder = None
            self.log(f"[!] Macros: recording is unavailable: {exc!r}")
        if recorder is None:
            return "Recording is not available on this system."
        self.stop_all()                      # their clicks would only be noise
        try:
            recorder.start(skip)
        except OSError as exc:
            self.log(f"[!] Macros: could not start recording: {exc}")
            return f"Could not start recording: {exc}"
        with self._lock:
            self._recorder = recorder
            self._recorded = None
        self.suspend_hotkeys()               # pressing a macro hotkey must not start it mid-recording
        self.log("[macros] recording started")
        return None

    def recording_info(self):
        """recorder.RecordingInfo of the recording in progress, or None when none is running."""
        recorder = self._recorder
        return recorder.info() if recorder is not None else None

    def finish_recording(self, trim_click: bool = False) -> list[dict] | None:
        """Ends the recording and returns its events (maybe an empty list). If the stop-all hotkey already
        ended it, returns those events. None if there was no recording at all. `trim_click`: the user
        stopped it by clicking a Stop button, so that click is removed from the end."""
        if self._recorder is not None:
            events = self._end_recording(trim_click)
        else:
            events, self._recorded = self._recorded, None
            if self._resync_pending and not self._closed:
                self._resync_pending = False
                self.sync_hotkeys()
        return events

    def _end_recording(self, trim_click: bool = False, resync: bool = True) -> list[dict]:
        with self._lock:
            recorder, self._recorder = self._recorder, None
        if recorder is None:
            return []
        try:
            events = recorder.stop(trim_click)
        except Exception as exc:
            self.log(f"[!] Macros: stopping the recording failed: {exc!r}")
            events = []
        if resync and not self._closed:
            self.sync_hotkeys()              # the macro hotkeys come back
        else:
            self._resync_pending = True
        self.log(f"[macros] recording ended ({len(events)} events)")
        return events

    def _find(self, macro_id: str) -> mc.Macro | None:
        with self._lock:
            return next((m for m in self.macros if m.id == macro_id), None)

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                pass

    # ---- the worker -----------------------------------------------------
    def _work(self, run: _Run, delay: float) -> None:
        macro = run.macro
        recorded = macro.type == "recorded"
        result = "Ready"
        try:
            api = self.api_fn()
            if api is None:
                result = "Mouse control is unavailable"
                self.log(f"[!] Macros: '{macro.name}' cannot run: mouse control is unavailable.")
                return
            if delay and self.wait_fn(run.stop, delay):
                return
            deadline = self.clock() + max(1, self.max_run_fn())
            result = self._replay_loop(run, api, deadline) if recorded else self._click_loop(run, api, deadline)
            if run.stop.is_set() and result == "Ready":
                result = f"Stopped ({_clicks(run.clicks, 'replay' if recorded else 'click')})"
        except Exception as exc:                 # a worker must never die silently or leave a macro 'running'
            result = "Error"
            self.log(f"[!] Macros: '{macro.name}' failed: {exc!r}")
        finally:
            with self._lock:
                if self._runs.get(macro.id) is run:
                    del self._runs[macro.id]
                if not run.test or result.startswith(("Stopped (", "Error", "Mouse")):
                    self.last_results[macro.id] = result
            if not run.test:
                self.log(f"[macros] ended: {macro.name}: {result}")
            self._changed()

    def _click_loop(self, run: _Run, api, deadline: float) -> str:
        """Click macros. Returns the outcome text ('Ready' = ended by the stop flag, the caller words that)."""
        macro = run.macro
        interval = max(mc.MIN_INTERVAL_MS, macro.intervalMs) / 1000.0
        while not run.stop.is_set():
            if not self.enabled_fn():
                return "Stopped (macros turned off)"
            if macro.type == "click_at" and not api.move_cursor(macro.x, macro.y):
                self.log(f"[!] Macros: '{macro.name}' could not move the cursor; stopped.")
                return "Stopped (could not move the mouse)"
            if not api.click(macro.button):
                self.log(f"[!] Macros: '{macro.name}': Windows blocked the click. If Roblox runs as "
                         f"administrator, run this app as administrator too. Stopped.")
                return "Stopped (the click was blocked)"
            run.clicks += 1
            if macro.repeat and run.clicks >= macro.repeat:
                return f"Done ({_clicks(run.clicks)})"
            if self.clock() >= deadline:
                self.log(f"[macros] '{macro.name}' stopped: maximum run time reached.")
                return "Stopped (maximum run time reached)"
            if self.wait_fn(run.stop, interval):
                break
        return "Ready"

    def _replay_loop(self, run: _Run, api, deadline: float) -> str:
        """Recorded macros: replay the recording `repeat` times (0 = until stopped), `intervalMs` apart."""
        macro = run.macro
        pause = max(mc.MIN_INTERVAL_MS, macro.intervalMs) / 1000.0
        while not run.stop.is_set():
            if not self.enabled_fn():
                return "Stopped (macros turned off)"
            outcome = self._play_pass(run, api, deadline)
            if outcome == _USER_STOP:
                break
            if outcome is not None:
                return outcome
            run.clicks += 1
            if macro.repeat and run.clicks >= macro.repeat:
                return f"Done ({_clicks(run.clicks, 'replay')})"
            if self.clock() >= deadline:
                self.log(f"[macros] '{macro.name}' stopped: maximum run time reached.")
                return "Stopped (maximum run time reached)"
            if self.wait_fn(run.stop, pause):
                break
        return "Ready"

    def _play_pass(self, run: _Run, api, deadline: float) -> str | None:
        """One replay of the recording. None = played to the end, _USER_STOP = cut short by the stop flag,
        anything else = the outcome text of a failure. Always releases what it pressed."""
        macro = run.macro
        speed = macro.speed if isinstance(macro.speed, (int, float)) and macro.speed > 0 else 1
        started = self.clock()
        pos = None                                # where the replay last put the cursor
        try:
            for ev in macro.events:
                if run.stop.is_set():
                    return _USER_STOP
                if self.clock() >= deadline:
                    self.log(f"[macros] '{macro.name}' stopped: maximum run time reached.")
                    return "Stopped (maximum run time reached)"
                wait = started + ev["t"] / 1000.0 / speed - self.clock()
                if wait > 0.0005 and self.wait_fn(run.stop, wait):
                    return _USER_STOP
                kind = ev["kind"]
                if kind == "move":
                    ok = api.move_cursor(ev["x"], ev["y"])
                    pos = (ev["x"], ev["y"])
                    if not ok:
                        self.log(f"[!] Macros: '{macro.name}' could not move the cursor; stopped.")
                        return "Stopped (could not move the mouse)"
                    continue
                if kind in ("down", "up"):
                    if pos != (ev["x"], ev["y"]):         # a press happens where it was recorded
                        if not api.move_cursor(ev["x"], ev["y"]):
                            self.log(f"[!] Macros: '{macro.name}' could not move the cursor; stopped.")
                            return "Stopped (could not move the mouse)"
                        pos = (ev["x"], ev["y"])
                    down = kind == "down"
                    ok = api.mouse_button(ev["button"], down)
                    if ok:
                        (run.held_buttons.add if down else run.held_buttons.discard)(ev["button"])
                else:
                    down = kind == "key_down"
                    ok = api.key(ev["vk"], down)
                    if ok:
                        (run.held_keys.add if down else run.held_keys.discard)(ev["vk"])
                if not ok:
                    self.log(f"[!] Macros: '{macro.name}': Windows blocked the input. If Roblox runs as "
                             f"administrator, run this app as administrator too. Stopped.")
                    return "Stopped (the input was blocked)"
            return None
        finally:
            self._release_held(run, api)

    def _release_held(self, run: _Run, api) -> None:
        """Lets go of every key and mouse button this replay still holds (best effort, never raises)."""
        for vk in list(run.held_keys):
            try:
                api.key(vk, False)
            except Exception:
                pass
        for button in list(run.held_buttons):
            try:
                api.mouse_button(button, False)
            except Exception:
                pass
        run.held_keys.clear()
        run.held_buttons.clear()
