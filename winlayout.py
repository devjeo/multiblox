"""
Window layout: tile the Roblox clients into a grid on one monitor. Pure logic on top of
winapi.WinApi, so it is tested on any OS with FakeWinApi.

  compute_grid   the cell rectangles
  order_hwnds    window handles in account-list order (stable layout)
  tile           moves the windows into the grid
  minimize_all / restore_all
  AutoTiler      after a client is ready: wait for its window, then ONE debounced re-tile
"""

from __future__ import annotations

import math
import threading
import time

from winapi import SW_MINIMIZE, SW_RESTORE, Rect, WinApi


def _split(length: int, parts: int, gap: int) -> list[tuple[int, int]]:
    """(offset, size) for `parts` equal-ish segments with `gap` pixels between them. Integer pixels,
    the remainder spread over the first segments, never below 1 px."""
    avail = max(0, length - gap * (parts - 1))
    base, extra = divmod(avail, parts)
    out, pos = [], 0
    for i in range(parts):
        size = max(1, base + (1 if i < extra else 0))
        out.append((pos, size))
        pos += size + gap
    return out


def compute_grid(total: int, columns: int, work: Rect, *, gap: int = 4) -> list[Rect]:
    """Cell rectangles for `total` windows inside `work`, row by row. Columns are clamped to the
    window count; a last row with fewer windows keeps the same cell size."""
    if total <= 0:
        return []
    columns = max(1, min(int(columns), total))
    gap = max(0, int(gap))
    rows = math.ceil(total / columns)
    xs = _split(work.w, columns, gap)
    ys = _split(work.h, rows, gap)
    cells = []
    for i in range(total):
        r, c = divmod(i, columns)
        cells.append(Rect(work.x + xs[c][0], work.y + ys[r][0], xs[c][1], ys[r][1]))
    return cells


def order_hwnds(accounts, pid_by_account: dict, hwnd_by_pid: dict) -> list[int]:
    """Window handles in the order of the account list. `pid_by_account` is keyed by account id."""
    out, seen = [], set()
    for account in accounts:
        hwnd = hwnd_by_pid.get(pid_by_account.get(account.id))
        if hwnd and hwnd not in seen:
            seen.add(hwnd)
            out.append(hwnd)
    return out


def tile(api: WinApi, hwnds: list[int], monitor_index: int, columns: int, *, gap: int = 4, log_fn=None) -> int:
    """Moves each window into its grid cell on the chosen monitor. Returns how many were moved.
    A monitor index that no longer exists falls back to monitor 0; fullscreen windows are skipped."""
    log = log_fn or (lambda _m: None)
    monitors = api.get_monitors()
    if not monitors or not hwnds:
        return 0
    if not 0 <= monitor_index < len(monitors):
        log(f"[layout] monitor {monitor_index + 1} is not connected; using monitor 1.")
        monitor_index = 0
    cells = compute_grid(len(hwnds), columns, monitors[monitor_index].work, gap=gap)
    moved = 0
    for hwnd, cell in zip(hwnds, cells):
        if api.is_fullscreen(hwnd):
            log("[layout] a client is fullscreen and cannot be tiled; skipped it.")
            continue
        if api.set_window_pos(hwnd, cell):
            moved += 1
    return moved


def minimize_all(api: WinApi, hwnds: list[int]) -> int:
    return sum(1 for h in hwnds if api.show_window(h, SW_MINIMIZE))


def restore_all(api: WinApi, hwnds: list[int]) -> int:
    return sum(1 for h in hwnds if api.show_window(h, SW_RESTORE))


class AutoTiler:
    """Call `on_ready(pid)` (from a worker thread; it blocks) when a client's process is tracked.
    The window does not exist yet at that moment, so this polls for it (up to `timeout`), then waits
    `debounce` seconds and re-tiles once; if more clients become ready meanwhile, only the last
    caller re-tiles, so several simultaneous launches cause a single pass."""

    def __init__(self, api: WinApi, retile_fn, *, enabled_fn=lambda: True, sleep=time.sleep,
                 clock=time.monotonic, log_fn=None, timeout: float = 30.0, poll: float = 1.0,
                 debounce: float = 2.0) -> None:
        self.api, self.retile_fn, self.enabled_fn = api, retile_fn, enabled_fn
        self.sleep, self.clock = sleep, clock
        self.log = log_fn or (lambda _m: None)
        self.timeout, self.poll, self.debounce = timeout, poll, debounce
        self._lock = threading.Lock()
        self._generation = 0

    def on_ready(self, pid: int) -> bool:
        """True if this call performed the re-tile."""
        if not self.enabled_fn():
            return False
        deadline = self.clock() + self.timeout
        while not self.api.enum_windows_for_pids({pid}).get(pid):
            if self.clock() >= deadline:
                self.log(f"[layout] no window appeared for PID {pid}; not tiling.")
                return False
            self.sleep(self.poll)
        with self._lock:
            self._generation += 1
            token = self._generation
        self.sleep(self.debounce)
        with self._lock:
            if token != self._generation:
                return False  # a newer client became ready; it will do the pass
        if not self.enabled_fn():
            return False
        try:
            self.retile_fn()
        except Exception as exc:
            self.log(f"[layout] auto-tile failed: {exc!r}")
            return False
        return True