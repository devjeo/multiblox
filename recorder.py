"""
Recording mouse and keyboard input for recorded macros (phase 2), see macros.py for the event format.

`RecordingBuffer` is the pure part (no Win32): it turns raw mouse/key notifications into macro events
and applies every filter, so it is tested anywhere:
  * times are ms from the first event (a recording never starts with a dead wait);
  * mouse moves are thinned (at most one per MOVE_MIN_MS, none for an unchanged position); presses
    carry their own x, y, so nothing is lost;
  * key auto-repeat is dropped (one down per physical press);
  * the macro's own hotkey and the stop-all hotkey are not recorded, and neither are the Ctrl/Alt/Shift
    presses that belong to them;
  * a release whose press was not recorded is dropped, and a key or button still held when the
    recording ends gets a release, so a replay can never leave something stuck down;
  * limits: MAX_RECORD_SECONDS after the start, and MAX_EVENTS events.

`HookTranslator` maps raw Windows hook messages to buffer calls (also pure, so it is tested too).
`FakeRecorder` is the scripted version for tests. `real_recorder()` builds the Win32 one
(WH_MOUSE_LL / WH_KEYBOARD_LL hooks on their own thread with a message loop) and only works on
Windows; nothing here touches ctypes at import time, so this module imports anywhere.

Input that other programs inject (including this app's own clicks and replays) is ignored.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass

import macros as mc

MOVE_MIN_MS = 15                 # thinning of mouse moves (about 66 per second at most)
CLOSE_RESERVE = 256              # events kept free so keys still held at the end can be released
APPROACH_SECONDS = 1.0           # moves this soon before a trimmed Stop click are dropped with it
TRIM_WINDOW_SECONDS = 0.5        # a Stop click is only trimmed if it just happened

# modifier virtual-key codes -> RegisterHotKey flag (the hook reports left/right variants)
_MOD_FLAGS = {0x10: mc.MOD_SHIFT, 0xA0: mc.MOD_SHIFT, 0xA1: mc.MOD_SHIFT,
              0x11: mc.MOD_CONTROL, 0xA2: mc.MOD_CONTROL, 0xA3: mc.MOD_CONTROL,
              0x12: mc.MOD_ALT, 0xA4: mc.MOD_ALT, 0xA5: mc.MOD_ALT,
              0x5B: mc.MOD_WIN, 0x5C: mc.MOD_WIN}


@dataclass(frozen=True)
class RecordingInfo:
    count: int          # events so far
    seconds: float      # time since the recording started
    full: bool          # a limit was reached: the recording should be stopped


class RecordingBuffer:
    def __init__(self, skip=(), clock=time.monotonic, max_seconds: float = mc.MAX_RECORD_SECONDS,
                 max_events: int = mc.MAX_EVENTS, move_min_ms: float = MOVE_MIN_MS) -> None:
        """`skip`: Hotkey objects (macros.parse_hotkey) that must not appear in the recording."""
        self.skip = list(skip)
        self.clock = clock
        self.max_seconds = max_seconds
        self.max_events = max(1, max_events - CLOSE_RESERVE)
        self.move_min = move_min_ms / 1000.0
        self.started = clock()
        self._lock = threading.Lock()
        self._events: list[tuple[float, dict]] = []     # (when, event); t is filled in by finish()
        self._first: float | None = None
        self._last_move: tuple[float, int, int] | None = None   # (when, x, y) of the last recorded move
        self._pressed: set[int] = set()                  # every key physically down (for modifiers, auto-repeat)
        self._held_keys: dict[int, dict] = {}            # recorded key_down not yet released
        self._held_buttons: dict[str, dict] = {}         # recorded button down not yet released
        self._last_down: dict[str, tuple[float, dict]] = {}
        self._last_up: tuple[float, dict] | None = None
        self._done = False

    # ---- input (called from the hook thread)
    def move(self, x: int, y: int) -> None:
        with self._lock:
            now = self.clock()
            if not self._accepting(now):
                return
            if self._last_move is not None:
                when, lx, ly = self._last_move
                if (lx, ly) == (x, y) or now - when < self.move_min:
                    return
            self._last_move = (now, x, y)
            self._add(now, {"kind": "move", "x": int(x), "y": int(y)})

    def button(self, button: str, down: bool, x: int, y: int) -> None:
        with self._lock:
            now = self.clock()
            if down:
                if not self._accepting(now):
                    return
                ev = {"kind": "down", "button": button, "x": int(x), "y": int(y)}
                self._add(now, ev)
                self._held_buttons[button] = ev
                self._last_down[button] = (now, ev)
            else:
                if button not in self._held_buttons or self._done:
                    return                       # its press was not recorded: nothing to release
                ev = {"kind": "up", "button": button, "x": int(x), "y": int(y)}
                del self._held_buttons[button]
                self._events.append((now, ev))   # a release is always kept, even past a limit
                self._last_up = (now, ev)

    def key(self, vk: int, down: bool) -> None:
        with self._lock:
            now = self.clock()
            if self._done:
                return
            if not down:
                self._pressed.discard(vk)
                if self._held_keys.pop(vk, None) is not None:
                    self._events.append((now, {"kind": "key_up", "vk": int(vk)}))
                return
            if vk in self._pressed:
                return                           # auto-repeat of a key that is already down
            self._pressed.add(vk)
            mods = self._mods()
            for hk in self.skip:
                if vk == hk.vk and mods == hk.mods:
                    self._strip_modifiers(hk.mods)
                    return
            if not self._accepting(now):
                return
            ev = {"kind": "key_down", "vk": int(vk)}
            self._add(now, ev)
            self._held_keys[vk] = ev

    # ---- state
    @property
    def full(self) -> bool:
        with self._lock:
            return self._is_full(self.clock())

    def info(self) -> RecordingInfo:
        with self._lock:
            now = self.clock()
            return RecordingInfo(len(self._events), max(0.0, now - self.started), self._is_full(now))

    def finish(self, trim_click: bool = False) -> list[dict]:
        """Ends the recording and returns its events (times filled in). `trim_click`: the recording was
        stopped by clicking a Stop button, so that last click (and the mouse path leading to it) is removed."""
        with self._lock:
            if self._done:
                return []
            self._done = True
            now = self.clock()
            if trim_click:
                self._trim_stop_click(now)
            if self._events:                     # release whatever is still held
                for vk in list(self._held_keys):
                    self._events.append((now, {"kind": "key_up", "vk": vk}))
                for button in list(self._held_buttons):
                    ev = self._held_buttons[button]
                    self._events.append((now, {"kind": "up", "button": button, "x": ev["x"], "y": ev["y"]}))
            self._held_keys.clear()
            self._held_buttons.clear()
            if not self._events:
                return []
            first = self._events[0][0]
            limit_ms = int(self.max_seconds * 1000)
            out, last_t = [], 0
            for when, ev in self._events:
                t = max(last_t, min(limit_ms + 1000, round((when - first) * 1000)))
                last_t = t
                out.append({"t": t, **ev})
            return out

    # ---- internals (lock held)
    def _is_full(self, now: float) -> bool:
        return len(self._events) >= self.max_events or now - self.started > self.max_seconds

    def _accepting(self, now: float) -> bool:
        return not self._done and not self._is_full(now)

    def _add(self, now: float, ev: dict) -> None:
        self._events.append((now, ev))
        if self._first is None:
            self._first = now

    def _mods(self) -> int:
        flags = 0
        for vk in self._pressed:
            flags |= _MOD_FLAGS.get(vk, 0)
        return flags

    def _strip_modifiers(self, mods: int) -> None:
        """A skipped hotkey was pressed: forget the recorded Ctrl/Alt/Shift/Win presses that belong to it
        (their releases are then dropped too, because the press is no longer 'held')."""
        for vk in [v for v in self._held_keys if _MOD_FLAGS.get(v, 0) & mods]:
            target = self._held_keys.pop(vk)
            self._events = [(w, e) for w, e in self._events if e is not target]

    def _trim_stop_click(self, now: float) -> None:
        if self._last_up is None or now - self._last_up[0] > TRIM_WINDOW_SECONDS:
            return
        up_when, up = self._last_up
        down = self._last_down.get(up["button"])
        if down is None:
            return
        down_when, down_ev = down
        self._events = [(w, e) for w, e in self._events if e is not up and e is not down_ev]
        while self._events and self._events[-1][1]["kind"] == "move" \
                and self._events[-1][0] >= down_when - APPROACH_SECONDS:
            self._events.pop()                   # the mouse path towards the Stop button


# ---------------------------------------------------------------- raw hook messages -> buffer calls
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
LLMHF_INJECTED = 0x01
LLKHF_INJECTED = 0x10
_BUTTON_MESSAGES = {WM_LBUTTONDOWN: ("left", True), WM_LBUTTONUP: ("left", False),
                    WM_RBUTTONDOWN: ("right", True), WM_RBUTTONUP: ("right", False)}
_KEY_MESSAGES = {WM_KEYDOWN: True, WM_SYSKEYDOWN: True, WM_KEYUP: False, WM_SYSKEYUP: False}


class HookTranslator:
    """What the two hook callbacks do with a raw message. Middle/extra mouse buttons and the wheel are
    not recorded (replay can only press the left and right buttons). Injected input is ignored."""

    def __init__(self, buffer: RecordingBuffer) -> None:
        self.buffer = buffer

    def mouse(self, message: int, x: int, y: int, flags: int) -> None:
        if flags & LLMHF_INJECTED:
            return
        if message == WM_MOUSEMOVE:
            self.buffer.move(x, y)
        elif message in _BUTTON_MESSAGES:
            button, down = _BUTTON_MESSAGES[message]
            self.buffer.button(button, down, x, y)

    def key(self, message: int, vk: int, flags: int) -> None:
        if flags & LLKHF_INJECTED or message not in _KEY_MESSAGES:
            return
        if 1 <= vk <= 254:
            self.buffer.key(vk, _KEY_MESSAGES[message])


# ---------------------------------------------------------------- recorders
class InputRecorder:
    """Interface. One recorder records once: start(), then stop()."""

    def __init__(self, log_fn=print) -> None:
        self.log = log_fn
        self.buffer: RecordingBuffer | None = None

    def start(self, skip_hotkeys=()) -> None:
        """Begins recording. `skip_hotkeys`: Hotkey objects that must not be recorded. Raises OSError."""
        raise NotImplementedError

    def stop(self, trim_click: bool = False) -> list[dict]:
        """Ends the recording (safe to call twice) and returns the events."""
        raise NotImplementedError

    def info(self) -> RecordingInfo:
        return self.buffer.info() if self.buffer is not None else RecordingInfo(0, 0.0, False)


class FakeRecorder(InputRecorder):
    """Scripted recorder for tests: feed it input with move() / button() / key(). `fail` makes start() raise."""

    def __init__(self, log_fn=print, clock=time.monotonic, fail: str | None = None) -> None:
        super().__init__(log_fn)
        self.clock, self.fail = clock, fail
        self.active = False
        self.skip: list = []

    def start(self, skip_hotkeys=()) -> None:
        if self.fail:
            raise OSError(self.fail)
        self.skip = list(skip_hotkeys)
        self.buffer = RecordingBuffer(self.skip, self.clock)
        self.active = True

    def stop(self, trim_click=False):
        if not self.active or self.buffer is None:
            return []
        self.active = False
        return self.buffer.finish(trim_click)

    def move(self, x, y):
        if self.active:
            self.buffer.move(x, y)

    def button(self, button, down, x, y):
        if self.active:
            self.buffer.button(button, down, x, y)

    def key(self, vk, down):
        if self.active:
            self.buffer.key(vk, down)


def real_recorder(log_fn=print) -> InputRecorder:
    """The Win32 implementation. Raises OSError off Windows."""
    if sys.platform != "win32":
        raise OSError("recording needs Windows")
    return _Win32Recorder(log_fn)


WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
WM_QUIT = 0x0012


class _Win32Recorder(InputRecorder):
    def __init__(self, log_fn=print) -> None:
        super().__init__(log_fn)
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        self.MSLL, self.KBLL = MSLLHOOKSTRUCT, KBDLLHOOKSTRUCT
        self.HOOKPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        def sig(fn, args, res):
            fn.argtypes, fn.restype = args, res

        sig(u.SetWindowsHookExW, [ctypes.c_int, self.HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD], ctypes.c_void_p)
        sig(u.UnhookWindowsHookEx, [ctypes.c_void_p], wintypes.BOOL)
        sig(u.CallNextHookEx, [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM], wintypes.LPARAM)
        sig(u.GetMessageW, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], ctypes.c_int)
        sig(u.PeekMessageW, [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                             wintypes.UINT], wintypes.BOOL)
        sig(u.PostThreadMessageW, [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL)
        sig(self.kernel32.GetCurrentThreadId, [], wintypes.DWORD)
        sig(self.kernel32.GetModuleHandleW, [wintypes.LPCWSTR], wintypes.HMODULE)
        self._set_ctx = None
        try:
            fn = u.SetThreadDpiAwarenessContext
            sig(fn, [ctypes.c_void_p], ctypes.c_void_p)
            self._set_ctx = fn
        except AttributeError:
            pass

        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._error: str | None = None
        self._translator: HookTranslator | None = None
        self._procs = ()                      # the callbacks must stay referenced while the hooks exist

    def start(self, skip_hotkeys=()) -> None:
        if self._thread is not None:
            raise OSError("already recording")
        self.buffer = RecordingBuffer(skip_hotkeys)
        self._translator = HookTranslator(self.buffer)
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()
        if not self._ready.wait(3.0) or self._error:
            error = self._error or "the recording thread did not start"
            self._shutdown_thread()
            raise OSError(error)

    def stop(self, trim_click=False):
        self._shutdown_thread()
        return self.buffer.finish(trim_click) if self.buffer is not None else []

    def _shutdown_thread(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        if self._thread_id:
            self.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(2.0)

    # ---- the hook thread
    def _mouse_proc(self, code, wparam, lparam):
        try:
            if code >= 0 and self._translator is not None:
                s = self.ct.cast(lparam, self.ct.POINTER(self.MSLL)).contents
                self._translator.mouse(int(wparam), int(s.x), int(s.y), int(s.flags))
        except Exception:
            pass                              # a hook must never raise into Windows
        return self.user32.CallNextHookEx(None, code, wparam, lparam)

    def _key_proc(self, code, wparam, lparam):
        try:
            if code >= 0 and self._translator is not None:
                s = self.ct.cast(lparam, self.ct.POINTER(self.KBLL)).contents
                self._translator.key(int(wparam), int(s.vkCode), int(s.flags))
        except Exception:
            pass
        return self.user32.CallNextHookEx(None, code, wparam, lparam)

    def _run(self) -> None:
        hooks = []
        try:
            if self._set_ctx is not None:     # hook coordinates are physical pixels, like SetCursorPos in winapi.py
                try:
                    self._set_ctx(self.ct.c_void_p(-4))
                except OSError:
                    pass
            msg = self.wt.MSG()
            self.user32.PeekMessageW(self.ct.byref(msg), None, 0, 0, 0)    # creates this thread's message queue
            self._thread_id = self.kernel32.GetCurrentThreadId()
            self._procs = (self.HOOKPROC(self._mouse_proc), self.HOOKPROC(self._key_proc))
            module = self.kernel32.GetModuleHandleW(None)
            for hook_id, proc in ((WH_MOUSE_LL, self._procs[0]), (WH_KEYBOARD_LL, self._procs[1])):
                handle = self.user32.SetWindowsHookExW(hook_id, proc, module, 0)
                if not handle:
                    self._error = f"Windows refused the input hook (error {self.ct.get_last_error()})"
                    return
                hooks.append(handle)
            self._ready.set()
            while self.user32.GetMessageW(self.ct.byref(msg), None, 0, 0) > 0:
                pass                          # the hooks are called from inside GetMessage
        except Exception as exc:
            self._error = f"recording failed: {exc!r}"
        finally:
            for handle in hooks:
                self.user32.UnhookWindowsHookEx(handle)
            self._thread_id = 0
            self._ready.set()                 # never leave start() waiting
