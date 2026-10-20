"""
Hotkey macro tests: data, hotkey parsing, and the engine on FakeWinApi / FakeHotkeys
(no Windows, no Tk). Run:  python -m unittest test_macros -v
"""

from __future__ import annotations

import itertools
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import macro_engine as me
import macros as mc
from hotkeys import FakeHotkeys
from winapi import FakeWinApi


def wait_for(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return bool(cond())


def macro(**kw) -> mc.Macro:
    base = dict(name="Farm", hotkey="ctrl+alt+f6", type="click_here", repeat=3, intervalMs=20)
    base.update(kw)
    return mc.Macro(**base)


# ---------------------------------------------------------------- hotkeys
class HotkeyParseTests(unittest.TestCase):
    def test_modifiers_and_key(self):
        hk = mc.parse_hotkey("Ctrl+Alt+F6")
        self.assertEqual((hk.mods, hk.vk, hk.text), (mc.MOD_CONTROL | mc.MOD_ALT, 0x75, "ctrl+alt+f6"))

    def test_normalised_order_and_aliases(self):
        self.assertEqual(mc.parse_hotkey("shift + control + a").text, "ctrl+shift+a")
        self.assertEqual(mc.parse_hotkey("alt+Del").text, "alt+delete")
        self.assertEqual(mc.parse_hotkey("win+ctrl+z").mods, mc.MOD_WIN | mc.MOD_CONTROL)

    def test_f_keys_work_alone(self):
        self.assertEqual(mc.parse_hotkey("F9").vk, 0x78)
        self.assertEqual(mc.parse_hotkey("f24").vk, 0x87)

    def test_virtual_key_codes(self):
        self.assertEqual(mc.parse_hotkey("ctrl+a").vk, ord("A"))
        self.assertEqual(mc.parse_hotkey("ctrl+7").vk, ord("7"))
        self.assertEqual(mc.parse_hotkey("ctrl+space").vk, 0x20)

    def test_rejects_bad_input(self):
        for bad in ("", "   ", None, 5, "a", "7", "space", "ctrl+", "+a", "ctrl+alt", "ctrl+ctrl+a", "hyper+a",
                    "ctrl+f25", "ctrl+f0", "ctrl+banana", "ctrl++a"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                mc.parse_hotkey(bad)

    def test_format_hotkey(self):
        self.assertEqual(mc.format_hotkey("ctrl+alt+f6"), "Ctrl+Alt+F6")
        self.assertEqual(mc.format_hotkey("ctrl+a"), "Ctrl+A")
        self.assertEqual(mc.format_hotkey("nonsense"), "nonsense")

    def test_keypress_capture(self):
        self.assertEqual(mc.hotkey_from_keypress("F6", True, True, False), "ctrl+alt+f6")
        self.assertEqual(mc.hotkey_from_keypress("A", True, False, True), "ctrl+shift+a")
        self.assertEqual(mc.hotkey_from_keypress("Prior", False, True, False), "alt+pageup")
        self.assertIsNone(mc.hotkey_from_keypress("Control_L", True, False, False))   # a bare modifier
        self.assertIsNone(mc.hotkey_from_keypress("exclam", False, False, True))


# ---------------------------------------------------------------- validation
class ValidateTests(unittest.TestCase):
    def test_good_macro(self):
        self.assertEqual(mc.validate_macro(macro()), [])
        self.assertEqual(mc.validate_macro(macro(type="click_at", x=-1920, y=40)), [])

    def test_interval_minimum(self):
        self.assertTrue(mc.validate_macro(macro(intervalMs=mc.MIN_INTERVAL_MS - 1)))
        self.assertEqual(mc.validate_macro(macro(intervalMs=mc.MIN_INTERVAL_MS)), [])
        self.assertTrue(mc.validate_macro(macro(intervalMs=mc.MAX_INTERVAL_MS + 1)))
        self.assertTrue(mc.validate_macro(macro(intervalMs="fast")))

    def test_repeat_range(self):
        self.assertEqual(mc.validate_macro(macro(repeat=0)), [])
        self.assertTrue(mc.validate_macro(macro(repeat=-1)))
        self.assertTrue(mc.validate_macro(macro(repeat=mc.MAX_REPEAT + 1)))
        self.assertTrue(mc.validate_macro(macro(repeat=True)))

    def test_position_needed_for_click_at(self):
        self.assertTrue(mc.validate_macro(macro(type="click_at")))
        self.assertTrue(mc.validate_macro(macro(type="click_at", x=10)))
        self.assertTrue(mc.validate_macro(macro(type="click_at", x=40000, y=1)))
        self.assertEqual(mc.validate_macro(macro(type="click_here")), [])   # position ignored

    def test_name_type_button(self):
        self.assertTrue(mc.validate_macro(macro(name="  ")))
        self.assertTrue(mc.validate_macro(macro(name="x" * 61)))
        self.assertTrue(mc.validate_macro(macro(type="record")))
        self.assertTrue(mc.validate_macro(macro(button="middle")))

    def test_hotkey_problems(self):
        self.assertTrue(mc.validate_macro(macro(hotkey="a")))
        self.assertTrue(mc.validate_macro(macro(hotkey=mc.PANIC_HOTKEY)))   # reserved
        first = macro(name="First", hotkey="ctrl+alt+f6")
        second = macro(name="Second", hotkey="Alt+Ctrl+F6")                  # same combo, written differently
        problems = mc.validate_macro(second, [first])
        self.assertTrue(problems and "First" in problems[0])
        self.assertEqual(mc.validate_macro(first, [first]), [])              # not a conflict with itself


# ---------------------------------------------------------------- store
class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "macros.json"

    def test_round_trip(self):
        store = mc.MacroStore(self.path)
        a = macro(name="A", hotkey="ctrl+alt+f6", lastRun="2026-10-20T08:00:00", runCount=4)
        b = macro(name="B", hotkey="f7", type="click_at", x=100, y=200, button="right", repeat=0, enabled=False)
        store.save([a, b])
        loaded = store.load()
        self.assertEqual([m.to_dict() for m in loaded], [a.to_dict(), b.to_dict()])
        self.assertEqual(store.problems, [])

    def test_missing_file_is_empty(self):
        store = mc.MacroStore(self.path)
        self.assertEqual(store.load(), [])
        self.assertEqual(store.problems, [])

    def test_unreadable_file_keeps_a_bad_copy(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        store = mc.MacroStore(self.path)
        self.assertEqual(store.load(), [])
        self.assertTrue(store.problems)
        bad = self.path.with_name("macros.json.bad")
        self.assertEqual(bad.read_text(encoding="utf-8"), "{ this is not json")

    def test_wrong_shape_keeps_a_bad_copy(self):
        self.path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        store = mc.MacroStore(self.path)
        self.assertEqual(store.load(), [])
        self.assertTrue(self.path.with_name("macros.json.bad").exists())

    def test_invalid_entries_are_skipped_good_ones_kept(self):
        good = macro(name="Good").to_dict()
        raw = {"version": 1, "macros": [
            good, "junk", 7, {"name": "No hotkey", "type": "click_here"},
            dict(good, id="x1", hotkey="a"),                     # bare letter
            dict(good, id="x2", intervalMs=5),                   # under the minimum
            dict(good, id="x3", type="click_at", x=None),        # position missing
            dict(good, id="x4", repeat="many"),
            dict(good, id=good["id"]),                           # same id twice
            dict(good, id="ok2", name="Second", hotkey="F8", unknown="ignored"),
        ]}
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        store = mc.MacroStore(self.path)
        loaded = store.load()
        self.assertEqual([m.name for m in loaded], ["Good", "Second"])
        self.assertEqual(len(store.problems), 8)
        self.assertFalse(self.path.with_name("macros.json.bad").exists())   # file itself was fine

    def test_loaded_values_are_normalised(self):
        raw = {"version": 1, "macros": [{"name": " X ", "hotkey": "Control+ALT+F6", "type": "click_here",
                                         "x": 5, "y": 6, "repeat": 2, "intervalMs": 50}]}
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        m = mc.MacroStore(self.path).load()[0]
        self.assertEqual((m.name, m.hotkey, m.x, m.y, m.button, m.enabled), ("X", "ctrl+alt+f6", None, None,
                                                                              "left", True))

    def test_a_directory_in_place_of_the_file_does_not_crash_load(self):
        self.path.mkdir()                      # a directory where the file should be
        self.assertEqual(mc.MacroStore(self.path).load(), [])


# ---------------------------------------------------------------- fake mouse
class FakeMouseTests(unittest.TestCase):
    def test_records_moves_and_clicks(self):
        api = FakeWinApi()
        api.move_cursor(10, 20)
        api.click("right")
        self.assertEqual(api.calls, [("move", 10, 20), ("click", "right", (10, 20))])
        self.assertEqual(api.get_cursor_pos(), (10, 20))

    def test_unknown_button(self):
        with self.assertRaises(ValueError):
            FakeWinApi().click("middle")


# ---------------------------------------------------------------- engine
class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = mc.MacroStore(Path(self._tmp.name) / "macros.json")
        self.logs: list[str] = []
        self.api = FakeWinApi(cursor=(300, 400))
        self.engines: list[me.MacroEngine] = []
        self.addCleanup(lambda: [e.shutdown() for e in self.engines])

    def engine(self, macros, **kw) -> me.MacroEngine:
        kw.setdefault("listener", FakeHotkeys())
        e = me.MacroEngine(macros, self.store, lambda: self.api, self.logs.append, **kw)
        e.start_hotkeys()
        self.engines.append(e)
        return e

    def clicks(self):
        return [c for c in list(self.api.calls) if c[0] == "click"]

    # -- clicking
    def test_fires_n_clicks_then_stops_by_itself(self):
        m = macro(repeat=3)
        e = self.engine([m])
        self.assertEqual(e.on_hotkey(m.id), "started")
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(self.clicks(), [("click", "left", (300, 400))] * 3)   # clicked where the cursor was
        self.assertEqual([c[0] for c in self.api.calls], ["click"] * 3)        # click_here never moves
        self.assertEqual(e.status(m), "Done (3 clicks)")

    def test_click_at_moves_before_every_click(self):
        m = macro(type="click_at", x=50, y=60, button="right", repeat=2)
        e = self.engine([m])
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(self.api.calls, [("move", 50, 60), ("click", "right", (50, 60))] * 2)

    def test_repeat_zero_runs_until_the_hotkey_is_pressed_again(self):
        m = macro(repeat=0)
        e = self.engine([m])
        self.assertEqual(e.on_hotkey(m.id), "started")
        self.assertTrue(wait_for(lambda: len(self.clicks()) >= 4))
        self.assertTrue(e.is_running(m.id))
        self.assertEqual(e.on_hotkey(m.id), "stopped")          # the same hotkey toggles
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        seen = len(self.clicks())
        time.sleep(0.1)
        self.assertEqual(len(self.clicks()), seen)              # really stopped
        self.assertTrue(e.status(m).startswith("Stopped ("))

    def test_can_start_again_after_it_stopped(self):
        m = macro(repeat=1)
        e = self.engine([m])
        for n in (1, 2):
            self.assertEqual(e.on_hotkey(m.id), "started")
            self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
            self.assertEqual(len(self.clicks()), n)
        self.assertEqual(m.runCount, 2)

    def test_hotkey_through_the_listener(self):
        m = macro(repeat=1)
        e = self.engine([m])
        self.assertTrue(e.listener.press(m.id))                 # FakeHotkeys calls engine.on_hotkey
        self.assertTrue(wait_for(lambda: len(self.clicks()) == 1))

    # -- bookkeeping
    def test_run_count_and_last_run_are_saved(self):
        m = macro(repeat=1)
        e = self.engine([m], now_fn=lambda: __import__("datetime").datetime(2026, 10, 20, 9, 30, 5))
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        saved = mc.MacroStore(self.store.path).load()[0]
        self.assertEqual((saved.runCount, saved.lastRun), (1, "2026-10-20T09:30:05"))

    # -- what must not run
    def test_disabled_macro_is_ignored(self):
        m = macro(enabled=False)
        e = self.engine([m])
        self.assertNotIn(m.id, e.listener.registered)           # its hotkey is not even taken
        self.assertEqual(e.on_hotkey(m.id), "ignored")
        time.sleep(0.05)
        self.assertEqual(self.clicks(), [])
        self.assertEqual(e.status(m), "Off")

    def test_unknown_hotkey_name_is_ignored(self):
        e = self.engine([macro()])
        self.assertEqual(e.on_hotkey("no-such-macro"), "ignored")

    def test_master_switch_off_blocks_hotkeys(self):
        m = macro(repeat=0)
        on = [False]
        e = self.engine([m], enabled_fn=lambda: on[0])
        self.assertEqual(e.on_hotkey(m.id), "disabled")
        self.assertEqual(self.clicks(), [])

    def test_master_switch_off_stops_a_running_macro(self):
        m = macro(repeat=0)
        on = [True]
        e = self.engine([m], enabled_fn=lambda: on[0])
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: len(self.clicks()) >= 2))
        on[0] = False
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(e.status(m), "Stopped (macros turned off)")

    def test_interval_minimum_is_enforced_even_for_bad_data(self):
        m = macro(repeat=4, intervalMs=1)                       # bypasses validation on purpose
        waits = []
        e = self.engine([m], wait_fn=lambda stop, s: (waits.append(s), stop.is_set())[1])
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(len(self.clicks()), 4)
        self.assertEqual(waits, [mc.MIN_INTERVAL_MS / 1000] * 3)

    def test_interval_is_used_between_clicks(self):
        m = macro(repeat=3, intervalMs=250)
        waits = []
        e = self.engine([m], wait_fn=lambda stop, s: (waits.append(s), stop.is_set())[1])
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(waits, [0.25, 0.25])

    def test_maximum_run_length(self):
        m = macro(repeat=0)
        ticks = itertools.count(0.0, 1.0)                       # every clock() call is one 'second' later
        e = self.engine([m], clock=lambda: next(ticks), max_run_fn=lambda: 5,
                        wait_fn=lambda stop, s: stop.is_set())
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertTrue(0 < len(self.clicks()) < 10)
        self.assertEqual(e.status(m), "Stopped (maximum run time reached)")

    def test_blocked_click_stops_the_macro(self):
        self.api.fail_clicks = True
        m = macro(repeat=0)
        e = self.engine([m])
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(len(self.clicks()), 1)
        self.assertEqual(e.status(m), "Stopped (the click was blocked)")
        self.assertTrue(any("administrator" in line for line in self.logs))

    def test_missing_mouse_api_does_not_crash(self):
        m = macro()
        e = me.MacroEngine([m], self.store, lambda: None, self.logs.append, listener=FakeHotkeys())
        self.engines.append(e)
        e.start_hotkeys()
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(e.status(m), "Mouse control is unavailable")

    def test_exception_in_the_worker_is_reported_and_cleaned_up(self):
        m = macro()
        self.api.click = lambda button="left": 1 / 0
        e = self.engine([m])
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(e.status(m), "Error")

    # -- several macros, panic, quit
    def test_two_macros_run_independently(self):
        a, b = macro(name="A", hotkey="f6", repeat=0), macro(name="B", hotkey="f7", repeat=0, button="right")
        e = self.engine([a, b])
        e.on_hotkey(a.id)
        e.on_hotkey(b.id)
        self.assertTrue(wait_for(lambda: {c[1] for c in self.clicks()} == {"left", "right"}))
        self.assertEqual(set(e.running_ids()), {a.id, b.id})
        e.on_hotkey(a.id)
        self.assertTrue(wait_for(lambda: not e.is_running(a.id)))
        self.assertTrue(e.is_running(b.id))                     # B keeps going

    def test_panic_hotkey_stops_everything_even_with_the_switch_off(self):
        a, b = macro(name="A", hotkey="f6", repeat=0), macro(name="B", hotkey="f7", repeat=0)
        on = [True]
        e = self.engine([a, b], enabled_fn=lambda: on[0])
        e.on_hotkey(a.id)
        e.on_hotkey(b.id)
        self.assertTrue(wait_for(lambda: len(e.running_ids()) == 2))
        on[0] = False
        self.assertEqual(e.on_hotkey(me.PANIC_NAME), "panic")
        self.assertTrue(wait_for(lambda: e.running_ids() == []))

    def test_panic_hotkey_is_registered_and_macros_cannot_take_it(self):
        e = self.engine([macro()])
        self.assertEqual(e.listener.registered[me.PANIC_NAME], mc.PANIC_HOTKEY)
        self.assertTrue(e.listener.press(me.PANIC_NAME))

    def test_stop_on_quit(self):
        a, b = macro(name="A", hotkey="f6", repeat=0), macro(name="B", hotkey="f7", repeat=0)
        e = self.engine([a, b])
        e.on_hotkey(a.id)
        e.on_hotkey(b.id)
        self.assertTrue(wait_for(lambda: len(e.running_ids()) == 2))
        threads = [t for t in threading.enumerate() if t.name.startswith("macro-")]
        e.shutdown()
        self.assertEqual(e.running_ids(), [])
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertFalse(e.listener.running)                    # hotkeys released
        self.assertEqual(e.on_hotkey(a.id), "disabled")         # nothing starts after quit
        seen = len(self.clicks())
        time.sleep(0.1)
        self.assertEqual(len(self.clicks()), seen)

    # -- hotkey registration
    def test_hotkey_conflict_is_reported(self):
        a, b = macro(name="A", hotkey="ctrl+alt+f6"), macro(name="B", hotkey="f7")
        e = self.engine([a, b], listener=FakeHotkeys(taken={"ctrl+alt+f6"}))
        self.assertIn(a.id, e.errors)
        self.assertNotIn(b.id, e.errors)
        self.assertTrue(e.status(a).startswith("Hotkey problem"))
        self.assertTrue(any("ctrl+alt+f6".replace("ctrl+alt+f6", "Ctrl+Alt+F6") in line for line in self.logs))

    def test_conflict_is_logged_once_not_on_every_sync(self):
        a = macro(hotkey="ctrl+alt+f6")
        e = self.engine([a], listener=FakeHotkeys(taken={"ctrl+alt+f6"}))
        for _ in range(3):
            e.sync_hotkeys()
        self.assertEqual(len([line for line in self.logs if "Ctrl+Alt+F6" in line]), 1)

    def test_conflict_clears_when_the_key_is_freed(self):
        a = macro(hotkey="ctrl+alt+f6")
        hot = FakeHotkeys(taken={"ctrl+alt+f6"})
        e = self.engine([a], listener=hot)
        self.assertIn(a.id, e.errors)
        hot.taken.clear()
        e.sync_hotkeys()
        self.assertEqual(e.errors, {})

    # -- editing through the engine
    def test_add_replace_remove_save_and_resync(self):
        e = self.engine([])
        m = macro(name="One", hotkey="f6")
        e.add(m)
        self.assertEqual(e.listener.registered[m.id], "f6")
        self.assertEqual([x.name for x in mc.MacroStore(self.store.path).load()], ["One"])
        new = macro(id=m.id, name="One", hotkey="f8")
        e.replace(m, new)
        self.assertEqual(e.listener.registered[m.id], "f8")
        e.set_enabled(new, False)
        self.assertNotIn(m.id, e.listener.registered)
        self.assertFalse(mc.MacroStore(self.store.path).load()[0].enabled)
        e.remove(new)
        self.assertEqual(e.macros, [])
        self.assertEqual(mc.MacroStore(self.store.path).load(), [])

    def test_editing_or_turning_off_a_running_macro_stops_it(self):
        m = macro(repeat=0)
        e = self.engine([m])
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: e.is_running(m.id)))
        e.set_enabled(m, False)
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        e.set_enabled(m, True)
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: e.is_running(m.id)))
        e.replace(m, macro(id=m.id, hotkey="ctrl+alt+f6", repeat=0))
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))

    def test_suspend_releases_macro_hotkeys_but_keeps_panic(self):
        m = macro(hotkey="f6")
        e = self.engine([m])
        e.suspend_hotkeys()
        self.assertEqual(e.listener.registered, {me.PANIC_NAME: mc.PANIC_HOTKEY})
        e.sync_hotkeys()
        self.assertEqual(e.listener.registered[m.id], "f6")

    # -- test click
    def test_test_click_clicks_once_and_does_not_count_as_a_run(self):
        m = macro(type="click_at", x=7, y=8, repeat=0)
        e = self.engine([m])
        self.assertTrue(e.test_click(m, delay=0))
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(self.api.calls, [("move", 7, 8), ("click", "left", (7, 8))])
        self.assertEqual((m.runCount, m.lastRun), (0, None))
        self.assertEqual(m.repeat, 0)                           # the saved macro was not changed

    def test_test_click_refused_while_running(self):
        m = macro(repeat=0)
        e = self.engine([m])
        e.on_hotkey(m.id)
        self.assertFalse(e.test_click(m, delay=0))

    def test_test_click_can_be_stopped_during_its_countdown(self):
        m = macro()
        e = self.engine([m])
        e.test_click(m, delay=30)
        self.assertTrue(wait_for(lambda: e.is_running(m.id)))
        self.assertEqual(e.status(m), "Testing...")
        e.stop_all()
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(self.clicks(), [])


if __name__ == "__main__":
    unittest.main()
