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
from dataclasses import dataclass

SW_MINIMIZE = 6
SW_RESTORE = 9


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


class FakeWinApi(WinApi):
    """Scripted API for tests. Everything it is asked to do is recorded in `calls`."""

    def __init__(self, monitors=None, windows=None, fullscreen=()):
        self.monitors = list(monitors) if monitors is not None else [MonitorInfo(0, Rect(0, 0, 1920, 1040))]
        self.windows: dict[int, int] = dict(windows or {})   # pid -> hwnd
        self.fullscreen = set(fullscreen)                     # hwnds
        self.positions: dict[int, Rect] = {}
        self.calls: list[tuple] = []

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