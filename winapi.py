"""
Window-system access for the layout feature (see winlayout.py).

`WinApi` is the small interface the layout code needs. `FakeWinApi` is a scripted version
for tests. `real_api()` builds the real Win32 implementation and only works on Windows;
nothing here touches ctypes/Win32 at import time, so this module imports anywhere.

DPI: the app is system-DPI aware, so on a mixed-DPI setup window sizes would be mis-scaled.
The real implementation therefore runs its calls inside a per-monitor-aware THREAD DPI
context (and restores the previous one), so rectangles are physical pixels. The process
setting is never changed.
"""

from __future__ import annotations

import contextlib
import sys
import time
from dataclasses import dataclass

SW_MINIMIZE = 6
SW_RESTORE = 9

MOUSE_BUTTONS = ("left", "right")   # click() takes one of these

# Keys SendInput must flag as "extended" (the grey arrow/edit block, right Ctrl/Alt, Win keys, ...), or the
# game sees the numpad version of the key instead.
EXTENDED_VKS = frozenset({0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,    # PageUp .. Down
                          0x2C, 0x2D, 0x2E,                                  # PrintScreen, Insert, Delete
                          0x5B, 0x5C, 0x5D,                                  # left/right Win, Apps
                          0x6F, 0x90,                                        # numpad divide, NumLock
                          0xA3, 0xA5})                                       # right Ctrl, right Alt


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class MonitorInfo:
    index: int
    work: Rect   # the usable area (excludes the taskbar), in physical pixels
    dpi: int = 96


class WinApi:
    """Interface. Implementations must not raise for a window that has gone away."""

    def enum_windows_for_pids(self, pids) -> dict[int, int]:
        """{pid: hwnd} for the main (largest visible top-level) window of each pid that has one."""
        raise NotImplementedError

    def get_monitors(self) -> list[MonitorInfo]:
        raise NotImplementedError

    def set_window_pos(self, hwnd: int, rect: Rect) -> bool:
        """Moves/resizes so the VISIBLE window occupies `rect`. False if it could not."""
        raise NotImplementedError

    def show_window(self, hwnd: int, cmd: int) -> bool:
        raise NotImplementedError

    def set_foreground(self, hwnd: int) -> bool:
        raise NotImplementedError

    def is_fullscreen(self, hwnd: int) -> bool:
        return False

    # ---- mouse (hotkey macros, see macros.py / macro_engine.py). Screen pixels, physical.
    def get_cursor_pos(self) -> tuple[int, int]:
        raise NotImplementedError

    def move_cursor(self, x: int, y: int) -> bool:
        raise NotImplementedError

    def click(self, button: str = "left") -> bool:
        """Presses and releases the mouse button at the current cursor position. False if it failed."""
        raise NotImplementedError

    # ---- single presses (recorded macros replay these). False if Windows refused the input.
    def mouse_button(self, button: str, down: bool) -> bool:
        raise NotImplementedError

    def key(self, vk: int, down: bool) -> bool:
        """Presses (down=True) or releases a key by virtual-key code."""
        raise NotImplementedError


class FakeWinApi(WinApi):
    """Scripted API for tests. Everything it is asked to do is recorded in `calls`."""

    def __init__(self, monitors=None, windows=None, fullscreen=(), cursor=(0, 0)):
        self.monitors = list(monitors) if monitors is not None else [MonitorInfo(0, Rect(0, 0, 1920, 1040))]
        self.windows: dict[int, int] = dict(windows or {})   # pid -> hwnd
        self.fullscreen = set(fullscreen)                     # hwnds
        self.positions: dict[int, Rect] = {}
        self.calls: list[tuple] = []
        self.cursor: tuple[int, int] = tuple(cursor)         # where the pretend mouse is
        self.fail_clicks = False                             # tests: make click() / mouse_button() report failure
        self.fail_keys = False                               # tests: make key() report failure
        self.keys_down: set[int] = set()                     # keys / buttons pressed and not yet released,
        self.buttons_down: set[str] = set()                  # so tests can check nothing is left stuck

    def enum_windows_for_pids(self, pids):
        return {pid: self.windows[pid] for pid in pids if pid in self.windows}

    def get_monitors(self):
        return list(self.monitors)

    def set_window_pos(self, hwnd, rect):
        self.calls.append(("pos", hwnd, rect))
        self.positions[hwnd] = rect
        return True

    def show_window(self, hwnd, cmd):
        self.calls.append(("show", hwnd, cmd))
        return True

    def set_foreground(self, hwnd):
        self.calls.append(("foreground", hwnd))
        return True

    def is_fullscreen(self, hwnd):
        return hwnd in self.fullscreen

    def get_cursor_pos(self):
        return self.cursor

    def move_cursor(self, x, y):
        self.calls.append(("move", x, y))
        self.cursor = (x, y)
        return True

    def click(self, button="left"):
        if button not in MOUSE_BUTTONS:
            raise ValueError(f"unknown mouse button {button!r}")
        self.calls.append(("click", button, self.cursor))
        return not self.fail_clicks

    def mouse_button(self, button, down):
        if button not in MOUSE_BUTTONS:
            raise ValueError(f"unknown mouse button {button!r}")
        self.calls.append(("mouse", button, down, self.cursor))
        if self.fail_clicks:
            return False
        (self.buttons_down.add if down else self.buttons_down.discard)(button)
        return True

    def key(self, vk, down):
        self.calls.append(("key", vk, down))
        if self.fail_keys:
            return False
        (self.keys_down.add if down else self.keys_down.discard)(vk)
        return True


def real_api() -> WinApi:
    """The Win32 implementation. Raises OSError off Windows."""
    if sys.platform != "win32":
        raise OSError("window layout needs Windows")
    return _RealWinApi()


class _RealWinApi(WinApi):
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    GW_OWNER = 4
    GWL_STYLE = -16
    WS_CAPTION = 0x00C00000
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    MONITORINFOF_PRIMARY = 1
    MONITOR_DEFAULTTONEAREST = 2
    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    MAPVK_VK_TO_VSC = 0
    MOUSE_FLAGS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010)}   # (down, up)
    CLICK_HOLD_SECONDS = 0.01    # between press and release, so games see a real click

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                        ("dwFlags", wintypes.DWORD)]

        self.RECT, self.MONITORINFO = RECT, MONITORINFO
        self.WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.MONITORENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM)

        def sig(fn, args, res):
            fn.argtypes, fn.restype = args, res

        H = wintypes.HWND
        sig(u.EnumWindows, [self.WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL)
        sig(u.IsWindowVisible, [H], wintypes.BOOL)
        sig(u.IsIconic, [H], wintypes.BOOL)
        sig(u.IsZoomed, [H], wintypes.BOOL)
        sig(u.GetWindow, [H, wintypes.UINT], H)
        sig(u.GetWindowThreadProcessId, [H, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
        sig(u.GetWindowRect, [H, ctypes.POINTER(RECT)], wintypes.BOOL)
        sig(u.ShowWindow, [H, ctypes.c_int], wintypes.BOOL)
        sig(u.SetWindowPos, [H, H, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT],
            wintypes.BOOL)
        sig(u.SetForegroundWindow, [H], wintypes.BOOL)
        sig(u.GetWindowLongW, [H, ctypes.c_int], ctypes.c_long)
        sig(u.MonitorFromWindow, [H, wintypes.DWORD], wintypes.HANDLE)
        sig(u.GetMonitorInfoW, [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)], wintypes.BOOL)
        sig(u.EnumDisplayMonitors, [wintypes.HDC, ctypes.POINTER(RECT), self.MONITORENUMPROC, wintypes.LPARAM],
            wintypes.BOOL)
        # mouse input (SendInput). The INPUT union must hold the keyboard and hardware members too,
        # or sizeof(INPUT) is wrong on 64-bit Windows and SendInput rejects the call.
        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class _INPUT_UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

        self.POINT, self.MOUSEINPUT, self.KEYBDINPUT, self.INPUT = POINT, MOUSEINPUT, KEYBDINPUT, INPUT
        sig(u.GetCursorPos, [ctypes.POINTER(POINT)], wintypes.BOOL)
        sig(u.SetCursorPos, [ctypes.c_int, ctypes.c_int], wintypes.BOOL)
        sig(u.SendInput, [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int], wintypes.UINT)
        sig(u.MapVirtualKeyW, [wintypes.UINT, wintypes.UINT], wintypes.UINT)
        self._set_ctx = None
        try:  # Windows 10 1607+; older systems simply keep the process DPI behaviour
            fn = u.SetThreadDpiAwarenessContext
            sig(fn, [ctypes.c_void_p], ctypes.c_void_p)
            self._set_ctx = fn
        except AttributeError:
            pass
        self.dwm = None
        try:
            self.dwm = ctypes.WinDLL("dwmapi")
            sig(self.dwm.DwmGetWindowAttribute, [H, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD], ctypes.c_long)
        except (OSError, AttributeError):
            self.dwm = None
        self.shcore = None
        try:
            self.shcore = ctypes.WinDLL("shcore")
            sig(self.shcore.GetDpiForMonitor,
                [wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)],
                ctypes.c_long)
        except (OSError, AttributeError):
            self.shcore = None

    @contextlib.contextmanager
    def _dpi(self):
        prev = None
        if self._set_ctx is not None:
            try:
                prev = self._set_ctx(self.ct.c_void_p(-4))  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            except OSError:
                prev = None
        try:
            yield
        finally:
            if prev and self._set_ctx is not None:
                try:
                    self._set_ctx(self.ct.c_void_p(prev))
                except OSError:
                    pass

    def _rect(self, hwnd):
        r = self.RECT()
        return r if self.user32.GetWindowRect(hwnd, self.ct.byref(r)) else None

    def enum_windows_for_pids(self, pids):
        wanted = set(pids)
        best: dict[int, tuple[int, int]] = {}   # pid -> (area, hwnd)
        ct, wt = self.ct, self.wt

        def cb(hwnd, _lparam):
            try:
                if not self.user32.IsWindowVisible(hwnd) or self.user32.GetWindow(hwnd, self.GW_OWNER):
                    return True
                pid = wt.DWORD()
                self.user32.GetWindowThreadProcessId(hwnd, ct.byref(pid))
                if pid.value not in wanted:
                    return True
                r = self._rect(hwnd)
                if r is None:
                    return True
                area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                if area > best.get(pid.value, (-1, 0))[0]:
                    best[pid.value] = (area, hwnd)
            except Exception:
                pass  # never let an exception cross the C callback
            return True

        proc = self.WNDENUMPROC(cb)
        with self._dpi():
            self.user32.EnumWindows(proc, 0)
        return {pid: hwnd for pid, (_area, hwnd) in best.items()}

    def get_monitors(self):
        found: list[tuple[bool, int, int, Rect, int]] = []
        ct = self.ct

        def cb(hmon, _hdc, _rect, _lparam):
            try:
                info = self.MONITORINFO()
                info.cbSize = ct.sizeof(self.MONITORINFO)
                if self.user32.GetMonitorInfoW(hmon, ct.byref(info)):
                    w = info.rcWork
                    dpi = 96
                    if self.shcore is not None:
                        dx, dy = self.wt.UINT(), self.wt.UINT()
                        if self.shcore.GetDpiForMonitor(hmon, 0, ct.byref(dx), ct.byref(dy)) == 0 and dx.value:
                            dpi = int(dx.value)
                    found.append((not (info.dwFlags & self.MONITORINFOF_PRIMARY), w.left, w.top,
                                  Rect(w.left, w.top, w.right - w.left, w.bottom - w.top), dpi))
            except Exception:
                pass
            return True

        proc = self.MONITORENUMPROC(cb)
        with self._dpi():
            self.user32.EnumDisplayMonitors(None, None, proc, 0)
        found.sort(key=lambda t: (t[0], t[1], t[2]))  # primary first, then left to right
        return [MonitorInfo(i, rect, dpi) for i, (_p, _l, _t, rect, dpi) in enumerate(found)]

    def _frame_insets(self, hwnd, outer):
        """(left, top, right, bottom) of the invisible resize border, from the DWM frame bounds."""
        if self.dwm is None:
            return 0, 0, 0, 0
        frame = self.RECT()
        hr = self.dwm.DwmGetWindowAttribute(hwnd, self.DWMWA_EXTENDED_FRAME_BOUNDS,
                                            self.ct.byref(frame), self.ct.sizeof(frame))
        if hr != 0:
            return 0, 0, 0, 0
        return (max(0, frame.left - outer.left), max(0, frame.top - outer.top),
                max(0, outer.right - frame.right), max(0, outer.bottom - frame.bottom))

    def set_window_pos(self, hwnd, rect):
        try:
            with self._dpi():
                if self.user32.IsIconic(hwnd) or self.user32.IsZoomed(hwnd):
                    self.user32.ShowWindow(hwnd, SW_RESTORE)
                outer = self._rect(hwnd)
                if outer is None:
                    return False
                l, t, r, b = self._frame_insets(hwnd, outer)
                return bool(self.user32.SetWindowPos(
                    hwnd, None, rect.x - l, rect.y - t, rect.w + l + r, rect.h + t + b,
                    self.SWP_NOZORDER | self.SWP_NOACTIVATE))
        except OSError:
            return False

    def show_window(self, hwnd, cmd):
        return bool(self.user32.ShowWindow(hwnd, cmd))

    def set_foreground(self, hwnd):
        return bool(self.user32.SetForegroundWindow(hwnd))

    def is_fullscreen(self, hwnd):
        """Covers its whole monitor and has no title bar (Roblox's fullscreen). Cannot be tiled."""
        try:
            with self._dpi():
                r = self._rect(hwnd)
                if r is None or self.user32.GetWindowLongW(hwnd, self.GWL_STYLE) & self.WS_CAPTION:
                    return False
                info = self.MONITORINFO()
                info.cbSize = self.ct.sizeof(self.MONITORINFO)
                hmon = self.user32.MonitorFromWindow(hwnd, self.MONITOR_DEFAULTTONEAREST)
                if not self.user32.GetMonitorInfoW(hmon, self.ct.byref(info)):
                    return False
                m = info.rcMonitor
                return (r.left, r.top, r.right, r.bottom) == (m.left, m.top, m.right, m.bottom)
        except OSError:
            return False

    # ---- mouse
    def get_cursor_pos(self):
        try:
            with self._dpi():
                pt = self.POINT()
                if self.user32.GetCursorPos(self.ct.byref(pt)):
                    return int(pt.x), int(pt.y)
        except OSError:
            pass
        raise OSError("could not read the cursor position")

    def move_cursor(self, x, y):
        try:
            with self._dpi():
                return bool(self.user32.SetCursorPos(int(x), int(y)))
        except OSError:
            return False

    def _send_mouse(self, flags) -> bool:
        inp = self.INPUT(type=self.INPUT_MOUSE)
        inp.mi = self.MOUSEINPUT(0, 0, 0, flags, 0, 0)
        return self.user32.SendInput(1, self.ct.byref(inp), self.ct.sizeof(self.INPUT)) == 1

    def mouse_button(self, button, down):
        if button not in self.MOUSE_FLAGS:
            raise ValueError(f"unknown mouse button {button!r}")
        try:
            return self._send_mouse(self.MOUSE_FLAGS[button][0 if down else 1])
        except OSError:
            return False

    def key(self, vk, down):
        try:
            flags = (0 if down else self.KEYEVENTF_KEYUP) | (self.KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0)
            scan = self.user32.MapVirtualKeyW(int(vk), self.MAPVK_VK_TO_VSC)
            inp = self.INPUT(type=self.INPUT_KEYBOARD)
            inp.ki = self.KEYBDINPUT(int(vk), scan, flags, 0, 0)
            return self.user32.SendInput(1, self.ct.byref(inp), self.ct.sizeof(self.INPUT)) == 1
        except OSError:
            return False

    def click(self, button="left"):
        if button not in self.MOUSE_FLAGS:
            raise ValueError(f"unknown mouse button {button!r}")
        down, up = self.MOUSE_FLAGS[button]
        try:
            if not self._send_mouse(down):
                return False
            time.sleep(self.CLICK_HOLD_SECONDS)
            return self._send_mouse(up)     # always tried after a press, so a button is never left held
        except OSError:
            return False
