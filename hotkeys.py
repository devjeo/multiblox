"""
Global hotkeys (RegisterHotKey), so a macro hotkey works while Roblox has the focus.

`HotkeyListener` is the interface the macro engine uses. `FakeHotkeys` is a scripted version for
tests. `real_listener()` builds the Win32 one and only works on Windows; nothing here touches
ctypes at import time, so this module imports anywhere.

How the real one works: a hotkey belongs to the thread that registered it, and that thread must
run a message loop to be told about key presses. So one background thread owns every hotkey and
sits in GetMessage. Other threads never call RegisterHotKey themselves: sync() posts a wake-up
message (WM_APP) to that thread with the wanted set of hotkeys and waits for the answer, so the
dashboard can add, change or remove macros at any time.

sync() takes {name: "ctrl+alt+f6"} and returns {name: "why it failed"} for the hotkeys Windows
refused (usually: another program already has that key combination). Failed ones are tried again
on the next sync, so freeing the key elsewhere and saving any macro picks it up.
"""

from __future__ import annotations

import itertools
import queue
import sys
import threading

from macros import parse_hotkey

MOD_NOREPEAT = 0x4000          # holding the keys down does not fire again and again
WM_HOTKEY = 0x0312
WM_APP = 0x8000
WM_QUIT = 0x0012
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
SYNC_TIMEOUT_SECONDS = 3.0


class HotkeyListener:
    """Interface. `on_press(name)` is called from the listener thread; keep it quick."""

    def __init__(self, on_press=None, log_fn=print) -> None:
        self.on_press = on_press
        self.log = log_fn

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def sync(self, wanted: dict[str, str]) -> dict[str, str]:
        """Makes exactly `wanted` ({name: hotkey text}) the registered set. Returns {name: error}."""
        raise NotImplementedError


class FakeHotkeys(HotkeyListener):
    """Scripted listener for tests: `taken` holds hotkey texts that 'another program' owns."""

    def __init__(self, on_press=None, log_fn=print, taken=()) -> None:
        super().__init__(on_press, log_fn)
        self.taken = set(taken)
        self.registered: dict[str, str] = {}     # name -> normalised hotkey text
        self.running = False
        self.sync_calls = 0

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False
        self.registered = {}

    def sync(self, wanted):
        self.sync_calls += 1
        errors, seen = {}, set()
        self.registered = {}
        for name, text in wanted.items():
            try:
                norm = parse_hotkey(text).text
            except ValueError as exc:
                errors[name] = str(exc)
                continue
            if norm in self.taken or norm in seen:
                errors[name] = "Already used by another program or macro."
                continue
            seen.add(norm)
            self.registered[name] = norm
        return errors

    def press(self, name: str) -> bool:
        """Pretends the user pressed the hotkey registered under `name`. False if it is not registered."""
        if not self.running or name not in self.registered or self.on_press is None:
            return False
        self.on_press(name)
        return True


def real_listener(on_press=None, log_fn=print) -> HotkeyListener:
    """The Win32 implementation. Raises OSError off Windows."""
    if sys.platform != "win32":
        raise OSError("global hotkeys need Windows")
    return _Win32Hotkeys(on_press, log_fn)


class _Command:
    def __init__(self, wanted: dict[str, str]) -> None:
        self.wanted = wanted
        self.errors: dict[str, str] = {}
        self.done = threading.Event()


class _Win32Hotkeys(HotkeyListener):
    def __init__(self, on_press=None, log_fn=print) -> None:
        super().__init__(on_press, log_fn)
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32")

        def sig(fn, args, res):
            fn.argtypes, fn.restype = args, res

        H = wintypes.HWND
        sig(u.RegisterHotKey, [H, ctypes.c_int, wintypes.UINT, wintypes.UINT], wintypes.BOOL)
        sig(u.UnregisterHotKey, [H, ctypes.c_int], wintypes.BOOL)
        sig(u.GetMessageW, [ctypes.POINTER(wintypes.MSG), H, wintypes.UINT, wintypes.UINT], ctypes.c_int)
        sig(u.PeekMessageW, [ctypes.POINTER(wintypes.MSG), H, wintypes.UINT, wintypes.UINT, wintypes.UINT],
            wintypes.BOOL)
        sig(u.PostThreadMessageW, [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL)
        sig(self.kernel32.GetCurrentThreadId, [], wintypes.DWORD)

        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._cmds: queue.Queue = queue.Queue()
        self._regs: dict[str, tuple[int, str]] = {}     # name -> (registration id, hotkey text); thread only
        self._by_id: dict[int, str] = {}                # registration id -> name; thread only
        self._ids = itertools.count(1)

    # ---- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        if not self._ready.wait(3.0):
            raise OSError("the hotkey thread did not start")

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        if self._thread_id:
            self.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        thread.join(2.0)
        self._thread_id = 0

    def sync(self, wanted):
        if self._thread is None or not self._thread_id or not thread_alive(self._thread):
            return {name: "Hotkeys are not running." for name in wanted}
        cmd = _Command(dict(wanted))
        self._cmds.put(cmd)
        if not self.user32.PostThreadMessageW(self._thread_id, WM_APP, 0, 0):
            return {name: "Hotkeys are not running." for name in wanted}
        if not cmd.done.wait(SYNC_TIMEOUT_SECONDS):
            return {name: "The hotkey thread did not answer." for name in wanted}
        return cmd.errors

    # ---- the hotkey thread
    def _run(self) -> None:
        msg = self.wt.MSG()
        self.user32.PeekMessageW(self.ct.byref(msg), None, 0, 0, 0)   # PM_NOREMOVE: creates this thread's queue
        self._thread_id = self.kernel32.GetCurrentThreadId()
        self._ready.set()
        try:
            while self.user32.GetMessageW(self.ct.byref(msg), None, 0, 0) > 0:   # 0 = WM_QUIT, -1 = error
                if msg.message == WM_HOTKEY:
                    name = self._by_id.get(int(msg.wParam))
                    if name is not None and self.on_press is not None:
                        try:
                            self.on_press(name)
                        except Exception as exc:       # one bad callback must not kill the listener
                            self.log(f"[!] Macros: hotkey handler failed: {exc!r}")
                elif msg.message == WM_APP:
                    self._drain()
        finally:
            for reg_id, _name in list(self._by_id.items()):
                self.user32.UnregisterHotKey(None, reg_id)
            self._regs.clear()
            self._by_id.clear()
            self._thread_id = 0
            while True:      # anyone still waiting on sync() gets an answer rather than the timeout
                try:
                    self._cmds.get_nowait().done.set()
                except queue.Empty:
                    break

    def _drain(self) -> None:
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                return
            try:
                cmd.errors = self._apply(cmd.wanted)
            except Exception as exc:
                cmd.errors = {name: f"Hotkey error: {exc!r}" for name in cmd.wanted}
            cmd.done.set()

    def _apply(self, wanted: dict[str, str]) -> dict[str, str]:
        errors: dict[str, str] = {}
        parsed = {}
        for name, text in wanted.items():
            try:
                parsed[name] = parse_hotkey(text)
            except ValueError as exc:
                errors[name] = str(exc)
        for name, (reg_id, text) in list(self._regs.items()):      # drop what is gone or changed
            if name not in parsed or parsed[name].text != text:
                self.user32.UnregisterHotKey(None, reg_id)
                del self._regs[name]
                del self._by_id[reg_id]
        for name, hk in parsed.items():
            if name in self._regs:
                continue
            reg_id = next(self._ids)
            if self.user32.RegisterHotKey(None, reg_id, hk.mods | MOD_NOREPEAT, hk.vk):
                self._regs[name] = (reg_id, hk.text)
                self._by_id[reg_id] = name
            else:
                code = self.ct.get_last_error()
                errors[name] = ("Already used by another program (or another macro)."
                                if code == ERROR_HOTKEY_ALREADY_REGISTERED
                                else f"Windows refused this hotkey (error {code}).")
        return errors


def thread_alive(thread: threading.Thread | None) -> bool:
    return thread is not None and thread.is_alive()
