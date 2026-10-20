"""
Recorded macro tests (phase 2): the event data, the recording filters, replay order and timing on
FakeWinApi, and recording control in the engine on FakeRecorder. No Windows, no Tk.
Run:  python -m unittest test_macro_recording -v
"""

from __future__ import annotations

import itertools
import json
import tempfile
import time
import unittest
from pathlib import Path

import macro_engine as me
import macros as mc
import recorder as rec
from hotkeys import FakeHotkeys
from winapi import FakeWinApi

VK_A, VK_F6, VK_F12 = 0x41, 0x75, 0x7B
VK_LCTRL, VK_LSHIFT, VK_LALT, VK_RCTRL = 0xA2, 0xA0, 0xA4, 0xA3


def wait_for(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return bool(cond())


def mv(t, x, y):
    return {"t": t, "kind": "move", "x": x, "y": y}


def btn(t, kind, x, y, button="left"):
    return {"t": t, "kind": kind, "button": button, "x": x, "y": y}


def key(t, kind, vk):
    return {"t": t, "kind": kind, "vk": vk}


SAMPLE = [mv(0, 10, 10), btn(50, "down", 10, 10), btn(120, "up", 10, 10), key(200, "key_down", VK_A),
          key(260, "key_up", VK_A)]


def recorded(**kw) -> mc.Macro:
    base = dict(name="Rec", hotkey="ctrl+alt+f6", type="recorded", repeat=1, intervalMs=20,
                events=[dict(e) for e in SAMPLE])
    base.update(kw)
    return mc.Macro(**base)


class Clock:
    """A clock that only moves when the engine waits, so timing is exact and the test is instant."""

    def __init__(self):
        self.t = 0.0
        self.waits: list[float] = []

    def clock(self):
        return self.t

    def wait(self, stop, seconds):
        self.waits.append(round(seconds, 6))
        self.t += seconds
        return stop.is_set()


# ---------------------------------------------------------------- data
class EventValidationTests(unittest.TestCase):
    def test_good_recording(self):
        self.assertEqual(mc.validate_events(SAMPLE), [])
        self.assertEqual(mc.validate_macro(recorded()), [])

    def test_empty_or_wrong_shape(self):
        for bad in ([], None, "x", {"t": 0}):
            self.assertTrue(mc.validate_events(bad), repr(bad))
        self.assertTrue(mc.validate_macro(recorded(events=[])))

    def test_bad_events(self):
        bad_events = [
            [{"t": 0, "kind": "scroll"}],
            [mv(0, 1, 2), mv(-1, 1, 2)],                       # negative time
            [mv(5, 1, 2), mv(4, 1, 2)],                        # time goes backwards
            [mv(mc.MAX_EVENT_TIME_MS + 1, 1, 2)],
            [mv(0, "1", 2)],
            [mv(0, 40000, 2)],
            [btn(0, "down", 1, 2, button="middle")],
            [key(0, "key_down", 0)],
            [key(0, "key_down", 255)],
            [key(0, "key_down", True)],
            ["not a dict"],
            [{"t": 0, "kind": "move", "x": 1}],                # no y
        ]
        for events in bad_events:
            self.assertTrue(mc.validate_events(events), repr(events))

    def test_too_many_events(self):
        many = [mv(0, 1, 1)] * (mc.MAX_EVENTS + 1)
        self.assertTrue(mc.validate_events(many))
        self.assertEqual(mc.validate_events([mv(0, 1, 1)] * mc.MAX_EVENTS), [])

    def test_speed(self):
        for ok in (0.5, 1, 2, 4, 2.0):
            self.assertEqual(mc.validate_macro(recorded(speed=ok)), [], ok)
        for bad in (0, 3, -1, "2", None, True):
            self.assertTrue(mc.validate_macro(recorded(speed=bad)), repr(bad))

    def test_recorded_needs_the_normal_fields_too(self):
        self.assertTrue(mc.validate_macro(recorded(intervalMs=5)))
        self.assertTrue(mc.validate_macro(recorded(repeat=-1)))
        self.assertTrue(mc.validate_macro(recorded(hotkey="a")))

    def test_summary(self):
        self.assertEqual(mc.recording_summary(recorded()), "5 events, 0.3 s")
        self.assertEqual(mc.events_summary([mv(0, 1, 1)]), "1 event, 0.0 s")
        self.assertEqual(mc.events_summary([]), "0 events, 0.0 s")
        self.assertEqual(mc.events_summary(None), "0 events, 0.0 s")
        self.assertEqual((mc.format_speed(0.5), mc.format_speed(2), mc.format_speed(4.0)), ("0.5x", "2x", "4x"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "macros.json"

    def test_round_trip(self):
        store = mc.MacroStore(self.path)
        a = recorded(name="A", speed=2, repeat=0, intervalMs=300)
        b = mc.Macro(name="B", hotkey="f7", type="click_at", x=1, y=2, repeat=3)
        store.save([a, b])
        loaded = store.load()
        self.assertEqual([m.to_dict() for m in loaded], [a.to_dict(), b.to_dict()])
        self.assertEqual(store.problems, [])

    def test_macros_file_from_phase_1_still_loads(self):
        raw = {"version": 1, "macros": [{"id": "old", "name": "Old", "hotkey": "ctrl+alt+f6", "type": "click_here",
                                         "repeat": 0, "intervalMs": 100}]}
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        m = mc.MacroStore(self.path).load()[0]
        self.assertEqual((m.type, m.events, m.speed), ("click_here", [], 1))

    def test_bad_recordings_are_skipped_good_ones_kept(self):
        good = recorded(name="Good").to_dict()
        raw = {"version": 1, "macros": [
            good,
            dict(good, id="e1", hotkey="f8", events=[]),
            dict(good, id="e2", hotkey="f9", events="junk"),
            dict(good, id="e3", hotkey="f10", events=[{"t": 0, "kind": "move"}]),
            dict(good, id="e4", hotkey="f11", speed=3),
            dict(good, id="e5", hotkey="f12"),                       # f12 alone is fine: kept
            {k: v for k, v in dict(good, id="e6", hotkey="ctrl+f1").items() if k != "events"},
        ]}
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        store = mc.MacroStore(self.path)
        loaded = store.load()
        self.assertEqual([m.hotkey for m in loaded], ["ctrl+alt+f6", "f12"])
        self.assertEqual(len(store.problems), 5)

    def test_missing_speed_defaults_to_1(self):
        raw = recorded().to_dict()
        del raw["speed"]
        self.path.write_text(json.dumps({"version": 1, "macros": [raw]}), encoding="utf-8")
        self.assertEqual(mc.MacroStore(self.path).load()[0].speed, 1)

    def test_events_are_dropped_from_click_macros(self):
        raw = mc.Macro(name="C", hotkey="f6").to_dict()
        raw["events"], raw["speed"] = [mv(0, 1, 1)], 4
        self.path.write_text(json.dumps({"version": 1, "macros": [raw]}), encoding="utf-8")
        m = mc.MacroStore(self.path).load()[0]
        self.assertEqual((m.events, m.speed), ([], 1))

    def test_position_is_cleared_for_recorded(self):
        raw = recorded().to_dict()
        raw["x"], raw["y"] = 5, 6
        self.path.write_text(json.dumps({"version": 1, "macros": [raw]}), encoding="utf-8")
        m = mc.MacroStore(self.path).load()[0]
        self.assertEqual((m.x, m.y), (None, None))


# ---------------------------------------------------------------- the recording filters
class BufferTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.skip = [mc.parse_hotkey(mc.PANIC_HOTKEY), mc.parse_hotkey("ctrl+alt+f6")]

    def buf(self, **kw) -> rec.RecordingBuffer:
        return rec.RecordingBuffer(kw.pop("skip", self.skip), lambda: self.now[0], **kw)

    def at(self, seconds):
        self.now[0] = 100.0 + seconds

    def test_times_start_at_zero_at_the_first_event(self):
        b = self.buf()
        self.at(5)                                   # five seconds of nothing first
        b.button("left", True, 10, 20)
        self.at(5.25)
        b.button("left", False, 10, 20)
        events = b.finish()
        self.assertEqual([e["t"] for e in events], [0, 250])
        self.assertEqual(mc.validate_events(events), [])

    def test_button_events_carry_their_position(self):
        b = self.buf()
        b.button("right", True, 7, 8)
        b.button("right", False, 9, 10)
        self.assertEqual(b.finish(), [{"t": 0, "kind": "down", "button": "right", "x": 7, "y": 8},
                                      {"t": 0, "kind": "up", "button": "right", "x": 9, "y": 10}])

    def test_moves_are_thinned(self):
        b = self.buf()
        b.move(1, 1)
        self.at(0.005)
        b.move(2, 2)                                 # too soon after the last one
        self.at(0.020)
        b.move(3, 3)
        self.at(0.040)
        b.move(3, 3)                                 # same place
        self.at(0.060)
        b.move(4, 4)
        self.assertEqual([(e["x"], e["y"]) for e in b.finish()], [(1, 1), (3, 3), (4, 4)])

    def test_key_auto_repeat_is_one_press(self):
        b = self.buf()
        for _ in range(5):
            b.key(VK_A, True)                        # Windows repeats the key-down while held
        self.at(1)
        b.key(VK_A, False)
        self.assertEqual([(e["kind"], e["t"]) for e in b.finish()], [("key_down", 0), ("key_up", 1000)])

    def test_own_hotkey_is_not_recorded_nor_its_modifiers(self):
        b = self.buf()
        b.key(VK_A, True)
        b.key(VK_A, False)
        self.at(1)
        for vk, down in ((VK_LCTRL, True), (VK_LALT, True), (VK_F6, True),
                         (VK_F6, False), (VK_LALT, False), (VK_LCTRL, False)):
            b.key(vk, down)
        self.assertEqual([(e["kind"], e["vk"]) for e in b.finish()], [("key_down", VK_A), ("key_up", VK_A)])

    def test_panic_hotkey_with_right_hand_modifiers(self):
        b = self.buf()
        b.button("left", True, 1, 1)
        b.button("left", False, 1, 1)
        for vk, down in ((VK_RCTRL, True), (VK_LSHIFT, True), (VK_F12, True), (VK_F12, False),
                         (VK_LSHIFT, False), (VK_RCTRL, False)):
            b.key(vk, down)
        self.assertEqual([e["kind"] for e in b.finish()], ["down", "up"])

    def test_a_different_combination_is_recorded(self):
        b = self.buf()
        for vk in (VK_LCTRL, VK_F6):                 # Ctrl+F6 is not the macro's Ctrl+Alt+F6
            b.key(vk, True)
        for vk in (VK_F6, VK_LCTRL):
            b.key(vk, False)
        self.assertEqual([(e["kind"], e["vk"]) for e in b.finish()],
                         [("key_down", VK_LCTRL), ("key_down", VK_F6), ("key_up", VK_F6), ("key_up", VK_LCTRL)])

    def test_a_function_key_hotkey_without_modifiers_is_skipped(self):
        b = self.buf(skip=[mc.parse_hotkey("f8")])
        b.key(0x77, True)
        b.key(0x77, False)
        self.assertEqual(b.finish(), [])

    def test_release_without_a_recorded_press_is_dropped(self):
        b = self.buf()
        b.button("left", False, 1, 1)                # button held from before the recording
        b.key(VK_A, False)
        self.assertEqual(b.finish(), [])

    def test_whatever_is_still_held_is_released_at_the_end(self):
        b = self.buf()
        b.key(VK_A, True)
        b.button("right", True, 30, 40)
        self.at(2)
        events = b.finish()
        self.assertEqual([(e["kind"], e["t"]) for e in events],
                         [("key_down", 0), ("down", 0), ("key_up", 2000), ("up", 2000)])
        self.assertEqual((events[-1]["x"], events[-1]["y"]), (30, 40))
        self.assertEqual(mc.validate_events(events), [])

    def test_event_limit_still_leaves_room_to_release(self):
        b = self.buf(max_events=rec.CLOSE_RESERVE + 4)
        b.key(VK_A, True)
        for i in range(10):
            self.at(0.1 * (i + 1))
            b.move(i, i)
        self.assertTrue(b.full)
        b.key(VK_A, False)                           # a release is always kept
        events = b.finish()
        self.assertEqual(len(events), 5)
        self.assertEqual(events[-1]["kind"], "key_up")

    def test_time_limit(self):
        b = self.buf(max_seconds=10)
        b.move(1, 1)
        self.at(9)
        self.assertFalse(b.full)
        b.move(2, 2)
        self.at(11)
        self.assertTrue(b.full)
        b.move(3, 3)                                 # too late: not recorded
        self.assertEqual(len(b.finish()), 2)
        self.assertTrue(b.info().full)

    def test_info(self):
        b = self.buf()
        b.move(1, 1)
        self.at(2.5)
        info = b.info()
        self.assertEqual((info.count, info.seconds, info.full), (1, 2.5, False))

    def test_finish_twice_and_input_after_finish(self):
        b = self.buf()
        b.move(1, 1)
        self.assertEqual(len(b.finish()), 1)
        b.move(5, 5)
        b.key(VK_A, True)
        self.assertEqual(b.finish(), [])

    def test_nothing_recorded_is_an_empty_list(self):
        self.assertEqual(self.buf().finish(), [])

    # -- stopping by clicking the Stop button
    def test_the_stop_click_and_the_path_to_it_are_removed(self):
        b = self.buf()
        b.button("left", True, 100, 100)
        b.button("left", False, 100, 100)
        self.at(5)
        b.move(500, 500)                             # leaving the game...
        self.at(5.3)
        b.move(900, 900)                             # ...towards the Stop button
        self.at(5.5)
        b.button("left", True, 900, 900)
        self.at(5.6)
        b.button("left", False, 900, 900)
        self.at(5.7)
        events = b.finish(trim_click=True)
        self.assertEqual([(e["kind"], e["x"]) for e in events], [("down", 100), ("up", 100)])

    def test_only_the_last_second_of_the_path_to_stop_is_removed(self):
        b = self.buf()
        b.move(50, 50)                               # well before: part of the real recording
        self.at(3)
        b.move(700, 700)                             # within a second of the Stop click: removed
        self.at(3.5)
        b.button("left", True, 900, 900)
        b.button("left", False, 900, 900)
        self.at(3.6)
        self.assertEqual([(e["kind"], e["x"]) for e in b.finish(trim_click=True)], [("move", 50)])

    def test_a_click_long_ago_is_not_trimmed(self):
        b = self.buf()
        b.button("left", True, 1, 1)
        b.button("left", False, 1, 1)
        self.at(10)
        self.assertEqual(len(b.finish(trim_click=True)), 2)

    def test_trim_does_nothing_without_a_click(self):
        b = self.buf()
        b.move(1, 1)
        self.assertEqual(len(b.finish(trim_click=True)), 1)

    def test_hotkey_stop_does_not_trim(self):
        b = self.buf()
        b.button("left", True, 1, 1)
        b.button("left", False, 1, 1)
        self.assertEqual(len(b.finish()), 2)         # trim_click False: both kept


class TranslatorTests(unittest.TestCase):
    def setUp(self):
        self.buf = rec.RecordingBuffer([], lambda: 0.0)
        self.tr = rec.HookTranslator(self.buf)

    def kinds(self):
        return [(e["kind"], e.get("button", e.get("vk"))) for e in self.buf.finish()]

    def test_mouse_messages(self):
        self.tr.mouse(rec.WM_LBUTTONDOWN, 1, 2, 0)
        self.tr.mouse(rec.WM_LBUTTONUP, 1, 2, 0)
        self.tr.mouse(rec.WM_RBUTTONDOWN, 3, 4, 0)
        self.tr.mouse(rec.WM_RBUTTONUP, 3, 4, 0)
        self.assertEqual(self.kinds(), [("down", "left"), ("up", "left"), ("down", "right"), ("up", "right")])

    def test_moves_wheel_and_middle_button(self):
        self.tr.mouse(rec.WM_MOUSEMOVE, 5, 6, 0)
        self.tr.mouse(0x020A, 5, 6, 0)               # WM_MOUSEWHEEL: not recorded
        self.tr.mouse(0x0207, 5, 6, 0)               # WM_MBUTTONDOWN: not recorded
        self.assertEqual(self.kinds(), [("move", None)])

    def test_key_messages_including_system_keys(self):
        self.tr.key(rec.WM_KEYDOWN, VK_A, 0)
        self.tr.key(rec.WM_KEYUP, VK_A, 0)
        self.tr.key(rec.WM_SYSKEYDOWN, VK_LALT, 0)
        self.tr.key(rec.WM_SYSKEYUP, VK_LALT, 0)
        self.assertEqual(self.kinds(), [("key_down", VK_A), ("key_up", VK_A), ("key_down", VK_LALT),
                                        ("key_up", VK_LALT)])

    def test_injected_input_is_ignored(self):
        self.tr.mouse(rec.WM_LBUTTONDOWN, 1, 2, rec.LLMHF_INJECTED)
        self.tr.mouse(rec.WM_MOUSEMOVE, 1, 2, rec.LLMHF_INJECTED)
        self.tr.key(rec.WM_KEYDOWN, VK_A, rec.LLKHF_INJECTED)
        self.assertEqual(self.kinds(), [])

    def test_odd_key_codes_are_ignored(self):
        self.tr.key(rec.WM_KEYDOWN, 0, 0)
        self.tr.key(rec.WM_KEYDOWN, 255, 0)
        self.tr.key(0x0102, VK_A, 0)                 # WM_CHAR is not a key message
        self.assertEqual(self.kinds(), [])


class FakeRecorderTests(unittest.TestCase):
    def test_records_what_it_is_fed_between_start_and_stop(self):
        r = rec.FakeRecorder()
        r.move(1, 1)                                 # before start: ignored
        r.start([])
        r.button("left", True, 2, 2)
        r.button("left", False, 2, 2)
        events = r.stop()
        r.key(VK_A, True)                            # after stop: ignored
        self.assertEqual([e["kind"] for e in events], ["down", "up"])
        self.assertEqual(r.stop(), [])

    def test_start_can_fail(self):
        with self.assertRaises(OSError):
            rec.FakeRecorder(fail="no hooks").start([])


class EventTable(unittest.TestCase):
    def test_extended_keys(self):
        from winapi import EXTENDED_VKS
        for vk in (0x25, 0x2E, 0xA3, 0xA5):          # left arrow, Delete, right Ctrl, right Alt
            self.assertIn(vk, EXTENDED_VKS)
        for vk in (VK_A, 0xA2, 0xA4, 0x0D):          # A, left Ctrl, left Alt, Enter
            self.assertNotIn(vk, EXTENDED_VKS)


# ---------------------------------------------------------------- replay
class ReplayTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = mc.MacroStore(Path(self._tmp.name) / "macros.json")
        self.logs: list[str] = []
        self.api = FakeWinApi(cursor=(500, 500))
        self.engines: list[me.MacroEngine] = []
        self.addCleanup(lambda: [e.shutdown() for e in self.engines])

    def engine(self, macros, **kw) -> me.MacroEngine:
        kw.setdefault("listener", FakeHotkeys())
        e = me.MacroEngine(macros, self.store, lambda: self.api, self.logs.append, **kw)
        e.start_hotkeys()
        self.engines.append(e)
        return e

    def run_to_end(self, m, **kw):
        e = self.engine([m], **kw)
        self.assertEqual(e.on_hotkey(m.id), "started")
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        return e

    def test_events_are_played_in_order(self):
        m = recorded()
        clock = Clock()
        e = self.run_to_end(m, clock=clock.clock, wait_fn=clock.wait)
        self.assertEqual(self.api.calls, [("move", 10, 10), ("mouse", "left", True, (10, 10)),
                                          ("mouse", "left", False, (10, 10)), ("key", VK_A, True),
                                          ("key", VK_A, False)])
        self.assertEqual(e.status(m), "Done (1 replay)")
        self.assertEqual((self.api.keys_down, self.api.buttons_down), (set(), set()))

    def test_recorded_timing_is_kept(self):
        clock = Clock()
        self.run_to_end(recorded(), clock=clock.clock, wait_fn=clock.wait)
        self.assertEqual(clock.waits, [0.05, 0.07, 0.08, 0.06])      # gaps between 0, 50, 120, 200, 260 ms

    def test_speed_divides_the_waits(self):
        clock = Clock()
        self.run_to_end(recorded(speed=2), clock=clock.clock, wait_fn=clock.wait)
        self.assertEqual(clock.waits, [0.025, 0.035, 0.04, 0.03])
        clock = Clock()
        self.run_to_end(recorded(speed=0.5, id="other", hotkey="f8"), clock=clock.clock, wait_fn=clock.wait)
        self.assertEqual(clock.waits, [0.1, 0.14, 0.16, 0.12])

    def test_repeat_plays_the_recording_again_after_the_pause(self):
        clock = Clock()
        m = recorded(repeat=2, intervalMs=500)
        e = self.run_to_end(m, clock=clock.clock, wait_fn=clock.wait)
        self.assertEqual(len(self.api.calls), 10)
        self.assertEqual(clock.waits, [0.05, 0.07, 0.08, 0.06, 0.5, 0.05, 0.07, 0.08, 0.06])
        self.assertEqual(e.status(m), "Done (2 replays)")

    def test_interval_minimum_is_enforced_between_replays(self):
        clock = Clock()
        self.run_to_end(recorded(repeat=2, intervalMs=1), clock=clock.clock, wait_fn=clock.wait)
        self.assertIn(mc.MIN_INTERVAL_MS / 1000, clock.waits)

    def test_a_press_moves_the_cursor_only_when_it_is_somewhere_else(self):
        events = [btn(0, "down", 5, 5), btn(10, "up", 5, 5), btn(20, "down", 9, 9), btn(30, "up", 9, 9)]
        clock = Clock()
        self.run_to_end(recorded(events=events), clock=clock.clock, wait_fn=clock.wait)
        self.assertEqual([c[0:3] for c in self.api.calls if c[0] == "move"], [("move", 5, 5), ("move", 9, 9)])

    def test_right_button_and_other_keys(self):
        events = [key(0, "key_down", VK_LCTRL), btn(5, "down", 1, 1, "right"), btn(9, "up", 1, 1, "right"),
                  key(12, "key_up", VK_LCTRL)]
        clock = Clock()
        self.run_to_end(recorded(events=events), clock=clock.clock, wait_fn=clock.wait)
        self.assertEqual([c for c in self.api.calls if c[0] != "move"],
                         [("key", VK_LCTRL, True), ("mouse", "right", True, (1, 1)),
                          ("mouse", "right", False, (1, 1)), ("key", VK_LCTRL, False)])

    def test_repeat_zero_runs_until_the_hotkey_is_pressed_again(self):
        m = recorded(repeat=0, events=[mv(0, 1, 1), key(0, "key_down", VK_A), key(1, "key_up", VK_A)])
        e = self.engine([m])
        self.assertEqual(e.on_hotkey(m.id), "started")
        self.assertTrue(wait_for(lambda: len([c for c in self.api.calls if c[0] == "key"]) >= 6))
        self.assertTrue(e.is_running(m.id))
        self.assertEqual(e.on_hotkey(m.id), "stopped")
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertTrue(e.status(m).startswith("Stopped ("))
        self.assertEqual(self.api.keys_down, set())

    def test_stopping_in_the_middle_releases_held_keys_and_buttons(self):
        events = [key(0, "key_down", VK_A), btn(0, "down", 3, 3), key(30_000, "key_up", VK_A),
                  btn(30_000, "up", 3, 3)]
        m = recorded(events=events)
        e = self.engine([m])
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: VK_A in self.api.keys_down and "left" in self.api.buttons_down))
        self.assertEqual(e.status(m), "Running (replay 1)")
        started = time.monotonic()
        e.on_hotkey(m.id)                                    # the toggle
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertLess(time.monotonic() - started, 2)       # did not wait for the 30 s gap
        self.assertEqual((self.api.keys_down, self.api.buttons_down), (set(), set()))
        self.assertIn(("key", VK_A, False), self.api.calls)

    def test_a_recording_that_ends_with_a_key_held_is_still_released(self):
        events = [key(0, "key_down", VK_A), btn(0, "down", 3, 3)]      # hand-made data: no releases
        self.run_to_end(recorded(events=events))
        self.assertEqual((self.api.keys_down, self.api.buttons_down), (set(), set()))

    def test_blocked_key_stops_the_macro(self):
        self.api.fail_keys = True
        m = recorded()
        e = self.run_to_end(m)
        self.assertEqual(e.status(m), "Stopped (the input was blocked)")
        self.assertTrue(any("administrator" in line for line in self.logs))

    def test_blocked_mouse_press_stops_and_releases_the_keys_already_down(self):
        self.api.fail_clicks = True
        events = [key(0, "key_down", VK_A), btn(1, "down", 3, 3), key(2, "key_up", VK_A)]
        m = recorded(events=events)
        e = self.run_to_end(m)
        self.assertEqual(e.status(m), "Stopped (the input was blocked)")
        self.assertEqual(self.api.keys_down, set())

    def test_master_switch_off_stops_a_replay(self):
        m = recorded(repeat=0, events=[key(0, "key_down", VK_A), key(1, "key_up", VK_A)])
        on = [True]
        e = self.engine([m], enabled_fn=lambda: on[0])
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: len(self.api.calls) >= 4))
        on[0] = False
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(e.status(m), "Stopped (macros turned off)")

    def test_panic_hotkey_stops_a_replay(self):
        m = recorded(repeat=0, events=[key(0, "key_down", VK_A), key(1, "key_up", VK_A)])
        e = self.engine([m])
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: len(self.api.calls) >= 4))
        self.assertTrue(e.listener.press(me.PANIC_NAME))
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(self.api.keys_down, set())

    def test_maximum_run_length_applies_to_replays(self):
        m = recorded(repeat=0)
        ticks = itertools.count(0.0, 1.0)
        e = self.engine([m], clock=lambda: next(ticks), max_run_fn=lambda: 20, wait_fn=lambda stop, s: stop.is_set())
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(e.status(m), "Stopped (maximum run time reached)")
        self.assertEqual(self.api.keys_down, set())

    def test_test_run_replays_once_and_is_not_counted(self):
        m = recorded(repeat=0)                               # would run forever as a real run
        e = self.engine([m])
        self.assertTrue(e.test_click(m, delay=0))
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(len([c for c in self.api.calls if c[0] == "key"]), 2)    # one replay
        self.assertEqual((m.runCount, m.lastRun), (0, None))

    def test_run_count_and_save_for_a_recorded_macro(self):
        m = recorded()
        self.run_to_end(m)
        saved = mc.MacroStore(self.store.path).load()[0]
        self.assertEqual((saved.runCount, saved.events, saved.type), (1, SAMPLE, "recorded"))

    def test_editing_the_saved_macro_does_not_change_a_run_in_progress(self):
        events = [key(0, "key_down", VK_A), key(30_000, "key_up", VK_A)]
        m = recorded(events=events)
        e = self.engine([m])
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: VK_A in self.api.keys_down))
        m.events.append(key(30_001, "key_up", 0x42))         # the run works on a copy
        e.stop(m.id)
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertNotIn(("key", 0x42, False), self.api.calls)

    def test_two_macros_of_different_types_at_once(self):
        a = recorded(repeat=0, events=[mv(0, 1, 1), mv(1, 2, 2)])
        b = mc.Macro(name="Clicker", hotkey="f7", type="click_here", repeat=0, intervalMs=20)
        e = self.engine([a, b])
        e.on_hotkey(a.id)
        e.on_hotkey(b.id)
        self.assertTrue(wait_for(lambda: len(self.api.calls) >= 10))
        e.stop_all()
        self.assertTrue(wait_for(lambda: not e.running_ids()))

    def test_no_mouse_available(self):
        m = recorded()
        e = me.MacroEngine([m], self.store, lambda: None, self.logs.append, listener=FakeHotkeys())
        self.engines.append(e)
        e.start_hotkeys()
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))
        self.assertEqual(e.status(m), "Mouse control is unavailable")

    def test_click_macros_still_work_through_the_refactored_worker(self):
        m = mc.Macro(name="C", hotkey="f6", type="click_at", x=4, y=5, repeat=2, intervalMs=20)
        e = self.run_to_end(m)
        self.assertEqual(self.api.calls, [("move", 4, 5), ("click", "left", (4, 5))] * 2)
        self.assertEqual(e.status(m), "Done (2 clicks)")


# ---------------------------------------------------------------- recording control in the engine
class RecordingControlTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = mc.MacroStore(Path(self._tmp.name) / "macros.json")
        self.logs: list[str] = []
        self.api = FakeWinApi()
        self.recorders: list[rec.FakeRecorder] = []
        self.engines: list[me.MacroEngine] = []
        self.addCleanup(lambda: [e.shutdown() for e in self.engines])

    def make_recorder(self):
        r = rec.FakeRecorder()
        self.recorders.append(r)
        return r

    def engine(self, macros=(), **kw) -> me.MacroEngine:
        kw.setdefault("recorder_fn", self.make_recorder)
        e = me.MacroEngine(list(macros), self.store, lambda: self.api, self.logs.append, listener=FakeHotkeys(), **kw)
        e.start_hotkeys()
        self.engines.append(e)
        return e

    def test_start_record_stop(self):
        e = self.engine()
        self.assertIsNone(e.start_recording("ctrl+alt+f6"))
        self.assertIsNotNone(e.recording_info())
        r = self.recorders[0]
        r.button("left", True, 5, 6)
        r.button("left", False, 5, 6)
        self.assertEqual(e.recording_info().count, 2)
        events = e.finish_recording()
        self.assertEqual([ev["kind"] for ev in events], ["down", "up"])
        self.assertIsNone(e.recording_info())
        self.assertIsNone(e.finish_recording())              # nothing left to finish

    def test_the_macros_own_and_the_panic_hotkey_are_not_recorded(self):
        e = self.engine()
        e.start_recording("ctrl+alt+f6")
        self.assertEqual({hk.text for hk in self.recorders[0].skip}, {mc.PANIC_HOTKEY, "ctrl+alt+f6"})
        e.finish_recording()
        e.start_recording("")                                # no hotkey chosen yet
        self.assertEqual({hk.text for hk in self.recorders[1].skip}, {mc.PANIC_HOTKEY})
        e.finish_recording()
        e.start_recording("nonsense")
        self.assertEqual({hk.text for hk in self.recorders[2].skip}, {mc.PANIC_HOTKEY})

    def test_macro_hotkeys_are_released_while_recording_and_come_back(self):
        m = mc.Macro(name="C", hotkey="ctrl+alt+f6")
        e = self.engine([m])
        self.assertIn(m.id, e.listener.registered)
        e.start_recording()
        self.assertEqual(set(e.listener.registered), {me.PANIC_NAME})
        e.sync_hotkeys()                                     # e.g. the editor's hotkey box lost focus
        self.assertEqual(set(e.listener.registered), {me.PANIC_NAME})
        e.finish_recording()
        self.assertEqual(set(e.listener.registered), {me.PANIC_NAME, m.id})

    def test_running_macros_stop_when_recording_starts(self):
        m = mc.Macro(name="C", hotkey="f6", repeat=0, intervalMs=20)
        e = self.engine([m])
        e.on_hotkey(m.id)
        self.assertTrue(wait_for(lambda: e.is_running(m.id)))
        e.start_recording()
        self.assertTrue(wait_for(lambda: not e.is_running(m.id)))

    def test_panic_hotkey_ends_a_recording_and_keeps_the_events(self):
        m = mc.Macro(name="C", hotkey="ctrl+alt+f6")
        e = self.engine([m])
        e.start_recording()
        self.recorders[0].key(VK_A, True)
        self.recorders[0].key(VK_A, False)
        self.assertTrue(e.listener.press(me.PANIC_NAME))
        self.assertIsNone(e.recording_info())                # the recording is over...
        self.assertFalse(self.recorders[0].active)
        self.assertEqual(set(e.listener.registered), {me.PANIC_NAME})   # ...hotkeys wait for finish_recording
        events = e.finish_recording()
        self.assertEqual([ev["kind"] for ev in events], ["key_down", "key_up"])
        self.assertEqual(set(e.listener.registered), {me.PANIC_NAME, m.id})
        self.assertIsNone(e.finish_recording())

    def test_panic_does_not_resync_on_the_hotkey_thread(self):
        e = self.engine()
        e.start_recording()
        calls = e.listener.sync_calls
        e.listener.press(me.PANIC_NAME)                      # sync() would deadlock the real listener thread
        self.assertEqual(e.listener.sync_calls, calls)

    def test_trim_click_is_passed_on(self):
        now = [0.0]
        e = self.engine(recorder_fn=lambda: self.recorders.append(rec.FakeRecorder(clock=lambda: now[0]))
                        or self.recorders[-1])
        e.start_recording()
        r = self.recorders[0]
        r.button("left", True, 1, 1)
        r.button("left", False, 1, 1)
        r.button("left", True, 9, 9)                         # the click on Stop
        r.button("left", False, 9, 9)
        self.assertEqual(len(e.finish_recording(trim_click=True)), 2)

    def test_not_available(self):
        e = self.engine(recorder_fn=lambda: None)
        self.assertIn("not available", e.start_recording())
        self.assertIsNone(e.recording_info())

    def test_recorder_that_raises(self):
        def boom():
            raise RuntimeError("no hooks")
        e = self.engine(recorder_fn=boom)
        self.assertIn("not available", e.start_recording())
        self.assertTrue(any("unavailable" in line for line in self.logs))

    def test_start_failure_is_reported_and_leaves_hotkeys_alone(self):
        m = mc.Macro(name="C", hotkey="f6")
        e = self.engine([m], recorder_fn=lambda: rec.FakeRecorder(fail="Windows refused the input hook"))
        message = e.start_recording()
        self.assertIn("Windows refused", message)
        self.assertIn(m.id, e.listener.registered)
        self.assertIsNone(e.recording_info())

    def test_a_second_recording_is_refused(self):
        e = self.engine()
        e.start_recording()
        self.assertIn("already", e.start_recording())

    def test_recording_works_with_the_master_switch_off(self):
        e = self.engine(enabled_fn=lambda: False)
        self.assertIsNone(e.start_recording())

    def test_shutdown_ends_the_recording(self):
        e = self.engine()
        e.start_recording()
        e.shutdown()
        self.assertFalse(self.recorders[0].active)
        self.assertIsNone(e.recording_info())
        self.assertIn("closing", e.start_recording())

    def test_recording_then_saving_a_recorded_macro_round_trip(self):
        e = self.engine()
        e.start_recording()
        r = self.recorders[0]
        r.button("left", True, 5, 6)
        r.button("left", False, 5, 6)
        r.key(VK_A, True)
        r.key(VK_A, False)
        macro = mc.Macro(name="Made", hotkey="ctrl+alt+f6", type="recorded", events=e.finish_recording())
        self.assertEqual(mc.validate_macro(macro), [])
        e.add(macro)
        self.assertEqual(mc.MacroStore(self.store.path).load()[0].events, macro.events)


if __name__ == "__main__":
    unittest.main()
