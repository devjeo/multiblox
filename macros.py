"""
Hotkey macros, data side: the Macro record, macros.json, hotkey parsing and validation.

No tkinter and no Win32 here (so it is tested anywhere). Running a macro is macro_engine.py,
listening for the hotkey is hotkeys.py, the forms are macros_window.py.

A hotkey is text like "ctrl+alt+f6". parse_hotkey() turns it into the modifier flags and the
virtual-key code that RegisterHotKey wants, and rejects anything that would take a normal typing
key away from every other program (a plain letter, digit or arrow with no modifier).
"""

from __future__ import annotations

import dataclasses
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from account_store import DATA_DIR
from jsonio import read_json, write_json_atomic

MACRO_FILE = DATA_DIR / "macros.json"
TYPES = ("click_here", "click_at", "recorded")
TYPE_LABELS = {"click_here": "Click here", "click_at": "Click at position", "recorded": "Recorded"}
BUTTONS = ("left", "right")

# Recorded macros (phase 2). A recording is a list of events with a time offset in ms from the first one:
#   {"t": 0,   "kind": "move", "x": 10, "y": 20}
#   {"t": 120, "kind": "down" | "up", "button": "left" | "right", "x": 10, "y": 20}
#   {"t": 300, "kind": "key_down" | "key_up", "vk": 65}
EVENT_KINDS = ("move", "down", "up", "key_down", "key_up")
SPEEDS = (0.5, 1, 2, 4)                # replay speed multipliers
MAX_EVENTS = 10_000                    # per recording (a macro with more is refused / skipped when loaded)
MAX_EVENT_TIME_MS = 3_600_000          # no event later than one hour into a recording
MAX_RECORD_SECONDS = 60                # a recording stops by itself after this long

MIN_INTERVAL_MS = 20
MAX_INTERVAL_MS = 3_600_000     # one hour between clicks
MAX_REPEAT = 1_000_000
COORD_MIN, COORD_MAX = -32768, 32767   # screen coordinates; negative on a monitor left of / above the main one
MAX_NAME = 60

PANIC_HOTKEY = "ctrl+shift+f12"   # always stops every running macro; macros may not use it

# RegisterHotKey modifier flags
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
_MODIFIERS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_MOD_ORDER = ("ctrl", "alt", "shift", "win")
_MOD_ALIASES = {"control": "ctrl", "windows": "win", "super": "win", "cmd": "win"}

# Keys other than letters, digits and F1-F24 (virtual-key codes).
_NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "backspace": 0x08, "pause": 0x13,
    "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "insert": 0x2D, "delete": 0x2E, "escape": 0x1B,
}
_KEY_ALIASES = {"return": "enter", "esc": "escape", "pgup": "pageup", "pgdn": "pagedown", "del": "delete",
                "ins": "insert", "prior": "pageup", "next": "pagedown"}
# Keys that are fine alone (they are not typing keys). Everything else needs a modifier.
_BARE_OK = {f"f{i}" for i in range(1, 25)}


@dataclass(frozen=True)
class Hotkey:
    mods: int       # MOD_* flags
    vk: int         # virtual-key code
    text: str       # normalised text, e.g. "ctrl+alt+f6"


def _key_vk(key: str) -> int | None:
    if len(key) == 1 and "a" <= key <= "z":
        return ord(key.upper())
    if len(key) == 1 and "0" <= key <= "9":
        return ord(key)
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x70 + int(key[1:]) - 1      # VK_F1 = 0x70
    return _NAMED_KEYS.get(key)


def parse_hotkey(text) -> Hotkey:
    """The Hotkey for text like 'Ctrl+Alt+F6'. Raises ValueError (a message for the user) if unusable."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Press a key combination for the hotkey.")
    parts = [p.strip().lower() for p in text.split("+")]
    if any(not p for p in parts):
        raise ValueError("The hotkey is not valid (empty part).")
    key = _KEY_ALIASES.get(parts[-1], parts[-1])
    names = [_MOD_ALIASES.get(p, p) for p in parts[:-1]]
    mods = 0
    for name in names:
        if name not in _MODIFIERS:
            raise ValueError(f"Unknown modifier '{name}'. Use Ctrl, Alt, Shift or Win.")
        if mods & _MODIFIERS[name]:
            raise ValueError(f"The modifier '{name}' is used twice.")
        mods |= _MODIFIERS[name]
    if key in _MODIFIERS:
        raise ValueError("The hotkey needs a normal key as well as the modifiers.")
    vk = _key_vk(key)
    if vk is None:
        raise ValueError(f"Unknown key '{parts[-1]}'. Use a letter, digit, F1-F24 or a key like Space, Home or Delete.")
    if not mods and key not in _BARE_OK:
        raise ValueError("Add Ctrl, Alt or Shift (a plain key would stop working in every other program). "
                         "F1-F24 can be used alone.")
    norm = "+".join([n for n in _MOD_ORDER if _MODIFIERS[n] & mods] + [key])
    return Hotkey(mods, vk, norm)


def format_hotkey(text) -> str:
    """'ctrl+alt+f6' -> 'Ctrl+Alt+F6' (for the dashboard). Unparseable text is shown as it is."""
    try:
        norm = parse_hotkey(text).text
    except ValueError:
        return str(text or "")
    return "+".join(p.capitalize() if len(p) > 1 and not (p[0] == "f" and p[1:].isdigit()) else p.upper()
                    for p in norm.split("+"))


# Tk keysym -> our key name (used by the press-to-set field; pure so it is tested here)
_KEYSYMS = {"Prior": "pageup", "Next": "pagedown", "Return": "enter", "Escape": "escape", "BackSpace": "backspace",
            "space": "space", "Tab": "tab", "Pause": "pause", "Home": "home", "End": "end", "Insert": "insert",
            "Delete": "delete", "Left": "left", "Right": "right", "Up": "up", "Down": "down"}


def hotkey_from_keypress(keysym: str, ctrl: bool, alt: bool, shift: bool) -> str | None:
    """Hotkey text for a key press, or None if the key alone is not a usable key (a bare modifier etc.)."""
    key = _KEYSYMS.get(keysym)
    if key is None:
        key = keysym.lower() if _key_vk(keysym.lower()) is not None else None
    if key is None:
        return None
    mods = [n for n, on in (("ctrl", ctrl), ("alt", alt), ("shift", shift)) if on]
    return "+".join(mods + [key])


# ---------------------------------------------------------------- the record
@dataclass
class Macro:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    hotkey: str = "ctrl+alt+f6"
    type: str = "click_here"          # click_here | click_at | recorded
    x: int | None = None              # click_at only (screen pixels)
    y: int | None = None
    button: str = "left"              # left | right
    repeat: int = 0                   # 0 = keep going until the hotkey is pressed again (recorded: replays)
    intervalMs: int = 100             # pause between clicks (recorded: between replays)
    enabled: bool = True
    lastRun: str | None = None        # ISO local time the macro last started
    runCount: int = 0                 # how many times it was started
    events: list = field(default_factory=list)   # recorded only: see EVENT_KINDS above
    speed: float = 1                  # recorded only: replay speed, one of SPEEDS

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _int(value, default=None):
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _parse_dt(text) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if isinstance(text, str) and text else None
    except ValueError:
        return None


def _num(value, default=None):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def validate_events(events) -> list[str]:
    """Human-readable problems with a recording (at most one is reported); empty means it is usable."""
    if not isinstance(events, list) or not events:
        return ["Record something first (the recording is empty)."]
    if len(events) > MAX_EVENTS:
        return [f"The recording has too many events (at most {MAX_EVENTS})."]
    last_t = 0
    for i, ev in enumerate(events, 1):
        where = f"Recorded event {i}"
        if not isinstance(ev, dict) or ev.get("kind") not in EVENT_KINDS:
            return [f"{where} is not valid."]
        t = _int(ev.get("t"))
        if t is None or not last_t <= t <= MAX_EVENT_TIME_MS:
            return [f"{where} has a bad time."]
        last_t = t
        kind = ev["kind"]
        if kind in ("move", "down", "up"):
            for axis in ("x", "y"):
                value = _int(ev.get(axis))
                if value is None or not COORD_MIN <= value <= COORD_MAX:
                    return [f"{where} has a bad position."]
        if kind in ("down", "up") and ev.get("button") not in BUTTONS:
            return [f"{where} has a bad mouse button."]
        if kind in ("key_down", "key_up"):
            vk = _int(ev.get("vk"))
            if vk is None or not 1 <= vk <= 254:
                return [f"{where} has a bad key."]
    return []


def events_duration_ms(events) -> int:
    """Length of a recording in ms (0 for an empty or unreadable one)."""
    try:
        return int(events[-1]["t"]) if events else 0
    except (KeyError, TypeError, ValueError, IndexError):
        return 0


def events_summary(events) -> str:
    """'42 events, 3.4 s' for the dashboard and the editor."""
    n = len(events) if isinstance(events, list) else 0
    return f"{n} event{'' if n == 1 else 's'}, {events_duration_ms(events) / 1000:.1f} s"


def recording_summary(macro: Macro) -> str:
    return events_summary(macro.events)


def format_speed(speed) -> str:
    """0.5 -> '0.5x', 2.0 -> '2x'."""
    return f"{speed:g}x"


def validate_macro(macro: Macro, others=()) -> list[str]:
    """Human-readable problems; empty means the macro can be saved. `others` = the other macros
    (to catch a hotkey two macros share)."""
    problems = []
    if not isinstance(macro.name, str) or not macro.name.strip():
        problems.append("Give the macro a name.")
    elif len(macro.name) > MAX_NAME:
        problems.append(f"The name is too long (at most {MAX_NAME} characters).")
    try:
        hk = parse_hotkey(macro.hotkey)
    except ValueError as exc:
        problems.append(str(exc))
    else:
        if hk.text == parse_hotkey(PANIC_HOTKEY).text:
            problems.append(f"{format_hotkey(PANIC_HOTKEY)} is reserved: it stops every macro.")
        for other in others:
            if other.id != macro.id and _same_hotkey(other.hotkey, hk.text):
                problems.append(f"The hotkey {format_hotkey(hk.text)} is already used by '{other.name}'.")
                break
    if macro.type not in TYPES:
        problems.append("Choose a macro type.")
    if macro.button not in BUTTONS:
        problems.append("Choose the left or right mouse button.")
    if _int(macro.repeat) is None or not 0 <= macro.repeat <= MAX_REPEAT:
        problems.append(f"Repeat must be 0 (until the hotkey is pressed again) to {MAX_REPEAT}.")
    if _int(macro.intervalMs) is None or not MIN_INTERVAL_MS <= macro.intervalMs <= MAX_INTERVAL_MS:
        problems.append(f"Interval must be {MIN_INTERVAL_MS} to {MAX_INTERVAL_MS} ms.")
    if macro.type == "recorded":
        problems.extend(validate_events(macro.events))
        if _num(macro.speed) not in SPEEDS:
            problems.append("Speed must be " + ", ".join(format_speed(v) for v in SPEEDS) + ".")
    if macro.type == "click_at":
        for label, value in (("x", macro.x), ("y", macro.y)):
            if _int(value) is None or not COORD_MIN <= value <= COORD_MAX:
                problems.append(f"Position {label} must be a whole number from {COORD_MIN} to {COORD_MAX} "
                                "(use Capture position).")
    return problems


def _same_hotkey(a, b) -> bool:
    try:
        return parse_hotkey(a).text == parse_hotkey(b).text
    except ValueError:
        return False


def macro_from_dict(raw) -> Macro | None:
    """A Macro from a saved dict, or None if it is unusable. Unknown keys are ignored; a field with
    the wrong type falls back to its default, then the result must pass validate_macro."""
    if not isinstance(raw, dict):
        return None
    m = Macro()
    if isinstance(raw.get("id"), str) and raw["id"]:
        m.id = raw["id"]
    m.name = raw["name"].strip() if isinstance(raw.get("name"), str) else ""
    m.hotkey = raw["hotkey"].strip() if isinstance(raw.get("hotkey"), str) else ""
    m.type = raw.get("type") if raw.get("type") in TYPES else ""
    m.x, m.y = _int(raw.get("x")), _int(raw.get("y"))
    m.button = raw.get("button") if raw.get("button") in BUTTONS else ("left" if "button" not in raw else "")
    m.repeat = _int(raw.get("repeat"), 0 if "repeat" not in raw else -1)
    m.intervalMs = _int(raw.get("intervalMs"), 100 if "intervalMs" not in raw else -1)
    m.enabled = raw.get("enabled", True) is not False
    m.lastRun = raw.get("lastRun") if _parse_dt(raw.get("lastRun")) else None
    m.runCount = max(0, _int(raw.get("runCount"), 0))
    if m.type == "recorded":
        m.events = [dict(ev) if isinstance(ev, dict) else ev
                    for ev in raw["events"]] if isinstance(raw.get("events"), list) else []
        m.speed = _num(raw.get("speed"), 1 if "speed" not in raw else -1)
    if validate_macro(m):
        return None
    m.hotkey = parse_hotkey(m.hotkey).text
    if m.type != "click_at":
        m.x = m.y = None
    if m.type != "recorded":
        m.events, m.speed = [], 1
    return m


class MacroStore:
    """macros.json: {'version': 1, 'macros': [...]}. Never raises on a bad file."""

    def __init__(self, path: Path = MACRO_FILE) -> None:
        self.path = Path(path)
        self.problems: list[str] = []

    def load(self) -> list[Macro]:
        self.problems = []
        if not self.path.exists():
            return []
        raw = read_json(self.path, None)
        if not isinstance(raw, dict) or not isinstance(raw.get("macros"), list):
            self.problems.append("macros.json is unreadable or not in the expected format; no macros loaded "
                                 "(a copy was kept as macros.json.bad)")
            try:
                shutil.copyfile(self.path, self.path.with_name(self.path.name + ".bad"))
            except OSError:
                pass
            return []
        macros, seen = [], set()
        for i, entry in enumerate(raw["macros"], 1):
            macro = macro_from_dict(entry)
            if macro is None:
                self.problems.append(f"macro {i} is incomplete or invalid and was skipped")
            elif macro.id in seen:
                self.problems.append(f"macro {i} repeats an earlier id and was skipped")
            else:
                seen.add(macro.id)
                macros.append(macro)
        return macros

    def save(self, macros) -> None:
        write_json_atomic(self.path, {"version": 1, "macros": [m.to_dict() for m in macros]})


def describe(macro: Macro) -> str:
    """One line for logs: 'Farm clicker (Ctrl+Alt+F6, click here)'."""
    return f"{macro.name} ({format_hotkey(macro.hotkey)}, {TYPE_LABELS.get(macro.type, macro.type).lower()})"
