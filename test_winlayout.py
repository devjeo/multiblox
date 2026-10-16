"""
Window layout tests. Pure logic plus FakeWinApi: they run on Linux, macOS and Windows.
Run:  python -m unittest test_winlayout -v
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

import diagnostics
import winlayout
from settings import DEFAULTS
from winapi import SW_MINIMIZE, SW_RESTORE, FakeWinApi, MonitorInfo, Rect

WORK = Rect(0, 0, 1920, 1040)


def overlaps(a: Rect, b: Rect) -> bool:
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.h and b.y < a.y + a.h


class GridTests(unittest.TestCase):
    def test_single_window_fills_work_area(self):
        self.assertEqual(winlayout.compute_grid(1, 3, WORK, gap=4), [WORK])

    def test_three_one_row(self):
        cells = winlayout.compute_grid(3, 3, WORK, gap=0)
        self.assertEqual({c.y for c in cells}, {0})
        self.assertEqual([c.x for c in cells], [0, 640, 1280])
        self.assertEqual({c.h for c in cells}, {1040})

    def test_five_two_rows(self):
        cells = winlayout.compute_grid(5, 3, WORK, gap=4)
        self.assertEqual(len(cells), 5)
        self.assertEqual(len({c.y for c in cells}), 2)
        self.assertEqual(len({c.x for c in cells}), 3)
        for i, a in enumerate(cells):
            for b in cells[i + 1:]:
                self.assertFalse(overlaps(a, b))

    def test_gap_reduces_cells(self):
        no_gap = winlayout.compute_grid(2, 2, WORK, gap=0)
        gapped = winlayout.compute_grid(2, 2, WORK, gap=20)
        self.assertLess(gapped[0].w, no_gap[0].w)
        self.assertEqual(gapped[1].x - (gapped[0].x + gapped[0].w), 20)

    def test_remainder_pixels_distributed(self):
        cells = winlayout.compute_grid(3, 3, Rect(0, 0, 1000, 500), gap=0)  # 1000 / 3 = 333 r 1
        self.assertEqual([c.w for c in cells], [334, 333, 333])
        self.assertEqual(cells[-1].x + cells[-1].w, 1000)  # the row spans the whole width exactly

    def test_columns_clamped_to_total(self):
        cells = winlayout.compute_grid(2, 6, WORK, gap=0)
        self.assertEqual([c.w for c in cells], [960, 960])
        self.assertEqual(len(winlayout.compute_grid(4, 0, WORK)), 4)  # 0 columns behaves as 1

    def test_zero_windows_empty(self):
        self.assertEqual(winlayout.compute_grid(0, 3, WORK), [])

    def test_tiny_work_area_never_negative(self):
        for cell in winlayout.compute_grid(6, 3, Rect(0, 0, 5, 3), gap=32):
            self.assertGreaterEqual(cell.w, 1)
            self.assertGreaterEqual(cell.h, 1)

    def test_work_area_offset_is_respected(self):
        cells = winlayout.compute_grid(2, 2, Rect(-1920, 100, 1920, 1000), gap=0)
        self.assertEqual((cells[0].x, cells[0].y), (-1920, 100))


class OrderTests(unittest.TestCase):
    def test_order_follows_account_list(self):
        accounts = [SimpleNamespace(id="a"), SimpleNamespace(id="b"), SimpleNamespace(id="c")]
        pid_by_account = {"c": 30, "a": 10}  # b has no client
        hwnd_by_pid = {10: 111, 30: 333}
        self.assertEqual(winlayout.order_hwnds(accounts, pid_by_account, hwnd_by_pid), [111, 333])

    def test_client_without_a_window_is_left_out(self):
        accounts = [SimpleNamespace(id="a"), SimpleNamespace(id="b")]
        self.assertEqual(winlayout.order_hwnds(accounts, {"a": 1, "b": 2}, {2: 222}), [222])


class TileTests(unittest.TestCase):
    def test_tile_moves_each_hwnd(self):
        api = FakeWinApi()
        self.assertEqual(winlayout.tile(api, [11, 22, 33], 0, 3, gap=0), 3)
        self.assertEqual([api.positions[h].x for h in (11, 22, 33)], [0, 640, 1280])

    def test_chosen_monitor_is_used(self):
        second = MonitorInfo(1, Rect(1920, 0, 1280, 720))
        api = FakeWinApi(monitors=[MonitorInfo(0, WORK), second])
        winlayout.tile(api, [11], 1, 3)
        self.assertEqual(api.positions[11], second.work)

    def test_missing_monitor_falls_back(self):
        api, logs = FakeWinApi(), []
        self.assertEqual(winlayout.tile(api, [11], 5, 3, log_fn=logs.append), 1)
        self.assertEqual(api.positions[11], WORK)
        self.assertTrue(logs)

    def test_fullscreen_client_is_skipped(self):
        api = FakeWinApi(fullscreen=[22])
        self.assertEqual(winlayout.tile(api, [11, 22, 33], 0, 3), 2)
        self.assertNotIn(22, api.positions)

    def test_nothing_to_tile(self):
        self.assertEqual(winlayout.tile(FakeWinApi(), [], 0, 3), 0)
        self.assertEqual(winlayout.tile(FakeWinApi(monitors=[]), [1], 0, 3), 0)

    def test_minimize_restore_commands(self):
        api = FakeWinApi()
        self.assertEqual(winlayout.minimize_all(api, [1, 2]), 2)
        self.assertEqual(winlayout.restore_all(api, [1]), 1)
        self.assertEqual(api.calls, [("show", 1, SW_MINIMIZE), ("show", 2, SW_MINIMIZE), ("show", 1, SW_RESTORE)])


class AutoTilerTests(unittest.TestCase):
    def make(self, windows=None, enabled=True):
        self.now = 0.0
        self.api = FakeWinApi(windows=windows or {})
        self.tiles = 0
        self.logs: list[str] = []
        self.on_sleep = None

        def sleep(seconds):
            self.now += seconds
            if self.on_sleep:
                self.on_sleep(seconds)

        def retile():
            self.tiles += 1

        return winlayout.AutoTiler(self.api, retile, enabled_fn=lambda: enabled, sleep=sleep,
                                   clock=lambda: self.now, log_fn=self.logs.append)

    def test_waits_for_the_window_then_tiles_once(self):
        tiler = self.make()

        def appear(_s):
            if self.now >= 3:
                self.api.windows[7] = 700
        self.on_sleep = appear
        self.assertTrue(tiler.on_ready(7))
        self.assertEqual(self.tiles, 1)

    def test_gives_up_when_no_window_appears(self):
        tiler = self.make()
        self.assertFalse(tiler.on_ready(7))
        self.assertEqual(self.tiles, 0)
        self.assertGreaterEqual(self.now, 30)
        self.assertTrue(any("no window appeared" in line for line in self.logs))

    def test_simultaneous_launches_cause_one_pass(self):
        tiler = self.make(windows={1: 100, 2: 200})
        results = []

        def second_client_becomes_ready_during_the_debounce(seconds):
            if seconds == tiler.debounce and not results:
                results.append("second")
                results.append(tiler.on_ready(2))  # nested: the newest caller wins
        self.on_sleep = second_client_becomes_ready_during_the_debounce
        first = tiler.on_ready(1)
        self.assertFalse(first)            # superseded
        self.assertTrue(results[1])        # the later one did the pass
        self.assertEqual(self.tiles, 1)

    def test_disabled_does_nothing(self):
        tiler = self.make(windows={1: 100}, enabled=False)
        self.assertFalse(tiler.on_ready(1))
        self.assertEqual(self.tiles, 0)

    def test_retile_error_is_contained(self):
        tiler = self.make(windows={1: 100})
        tiler.retile_fn = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertFalse(tiler.on_ready(1))
        self.assertTrue(any("auto-tile failed" in line for line in self.logs))


class DiagnosticsAndSettingsTests(unittest.TestCase):
    def test_layout_defaults(self):
        self.assertEqual((DEFAULTS["layoutMonitor"], DEFAULTS["layoutColumns"], DEFAULTS["layoutGap"],
                          DEFAULTS["layoutAutoTile"]), (0, 3, 4, False))

    def test_check_reports_monitors_and_last_tile(self):
        out = diagnostics.check_layout({"layoutMonitor": 0}, FakeWinApi(),
                                       {"clients": 3, "matched": 3, "moved": 3})
        self.assertEqual([r.level for r in out], ["OK", "OK"])

    def test_check_warns_when_saved_monitor_is_gone(self):
        out = diagnostics.check_layout({"layoutMonitor": 2}, FakeWinApi(), None)
        self.assertTrue(any(r.level == "WARN" and "not connected" in r.detail for r in out))

    def test_check_warns_when_some_windows_were_not_moved(self):
        out = diagnostics.check_layout({"layoutMonitor": 0}, FakeWinApi(),
                                       {"clients": 3, "matched": 2, "moved": 2})
        self.assertTrue(any(r.level == "WARN" for r in out))

    def test_check_skips_without_api(self):
        self.assertEqual(diagnostics.check_layout({}, None, None).level, "SKIP")


if __name__ == "__main__":
    unittest.main()