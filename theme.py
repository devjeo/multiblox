"""
Design tokens for the GUI, computed from a few numbers instead of picked by eye.

Pure Python: no tkinter, no Windows, so everything here is unit-tested (test_theme.py).

  * Spacing: a 4 px base unit; every gap in the app is a multiple of it (space(n)).
  * Type: a modular scale. size(step) = round(BASE_PT * RATIO ** step) with a major-third ratio.
  * Layout: the golden ratio splits the window between the account list and the side panel.
  * Colour: palettes are generated in HSL from one accent hue, and every text/background pair is
    checked against the WCAG contrast formula, so text is readable by construction.
"""

from __future__ import annotations

import colorsys
import math

UNIT = 4                 # px; the whole layout is built from multiples of this
BASE_PT = 10             # body text size in points
RATIO = 1.25             # major third
PHI = (1 + math.sqrt(5)) / 2
ACCENT_HUE = 222         # degrees; a calm blue
FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"


# ---------------------------------------------------------------- spacing and type
def space(n: float) -> int:
    """n units of UNIT px. space(2) = 8, space(4) = 16, space(6) = 24."""
    return int(round(n * UNIT))


def size(step: int) -> int:
    """Type size in points. step 0 = body, 1 = subheading, 2 = heading, -1 = caption."""
    return max(8, int(round(BASE_PT * RATIO ** step)))


def font(step: int = 0, weight: str = "normal", mono: bool = False) -> tuple:
    return (FONT_MONO if mono else FONT_UI, size(step), weight)


def row_height(step: int = 0) -> int:
    """Table row height: text size (pt -> px at 96 dpi) times 1.618, snapped to the 4 px grid."""
    px = size(step) * 96 / 72
    return int(round(px * PHI / UNIT)) * UNIT


# ---------------------------------------------------------------- golden-ratio layout
def split_golden(total: int, gap: int = 0) -> tuple[int, int]:
    """Splits `total` px into (main, side) with main : side = phi, after removing `gap`."""
    usable = max(0, total - gap)
    side = int(round(usable / (1 + PHI)))
    return usable - side, side


def window_size(screen_w: int, screen_h: int, fill: float = 0.62) -> tuple[int, int]:
    """A window on the golden rectangle, `fill` of the screen's height, kept on the 8 px grid and
    never larger than 94% of the screen."""
    h = screen_h * fill
    w = h * PHI
    w, h = min(w, screen_w * 0.94), min(h, screen_h * 0.94)
    snap = lambda v: int(v // (2 * UNIT)) * (2 * UNIT)
    return max(snap(w), 880), max(snap(h), 560)


def column_widths(total: int, weights: dict) -> dict:
    """Divides `total` px across columns by weight; the remainder goes to the heaviest column so the
    widths always sum to exactly `total`."""
    if not weights or total <= 0:
        return {k: 0 for k in weights}
    s = sum(weights.values())
    out = {k: int(total * w / s) for k, w in weights.items()}
    out[max(weights, key=weights.get)] += total - sum(out.values())
    return out


# ---------------------------------------------------------------- colour
def hsl(h: float, s: float, l: float) -> str:
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, min(1, max(0, l)), min(1, max(0, s)))
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def mix(a: str, b: str, t: float) -> str:
    """Linear blend: t = 0 gives `a`, t = 1 gives `b`."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join("{:02x}".format(round(x + (y - x) * t)) for x, y in zip(ca, cb))


def _channel(v: float) -> float:
    return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio, 1 to 21."""
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def readable(fg: str, bg: str, minimum: float = 4.5) -> str:
    """Nudges `fg` lighter or darker (in HSL lightness, 1% steps) until it reaches `minimum`
    contrast against `bg`. Returns it unchanged if it already does."""
    if contrast(fg, bg) >= minimum:
        return fg
    r, g, b = (int(fg[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    direction = 1 if contrast("#ffffff", bg) >= contrast("#000000", bg) else -1
    for _ in range(100):
        l = min(1, max(0, l + direction * 0.01))
        cand = hsl(h * 360, s, l)
        if contrast(cand, bg) >= minimum:
            return cand
    return "#ffffff" if direction == 1 else "#000000"


def _palette(dark: bool) -> dict:
    h = ACCENT_HUE
    if dark:
        bg, surface, raised, border = (hsl(h, .14, l) for l in (.075, .105, .145, .205))
        text, muted = hsl(h, .12, .93), hsl(h, .08, .66)
        accent = hsl(h, .90, .62)
        status = {"ok": hsl(145, .62, .52), "busy": hsl(38, .92, .58), "bad": hsl(4, .85, .64),
                  "idle": hsl(h, .08, .60)}
    else:
        bg, surface, raised, border = (hsl(h, .20, l) for l in (.965, 1.0, .985, .88))
        text, muted = hsl(h, .25, .12), hsl(h, .10, .38)
        accent = hsl(h, .82, .46)
        status = {"ok": hsl(145, .70, .27), "busy": hsl(32, .95, .30), "bad": hsl(4, .75, .42),
                  "idle": hsl(h, .08, .38)}
    status = {k: readable(v, surface) for k, v in status.items()}
    accent = readable(accent, surface, 3.0)
    if contrast("#ffffff", accent) < 4.5:  # keep white button text (the familiar look) if a shade allows it
        for step in range(62, 30, -1):
            cand = hsl(h, .90 if dark else .82, step / 100)
            if contrast("#ffffff", cand) >= 4.5 and contrast(cand, surface) >= 3.0:
                accent = cand
                break
    on_accent = max(("#ffffff", "#0b0f1a"), key=lambda c: contrast(c, accent))
    p = {
        "bg": bg, "surface": surface, "raised": raised, "border": border,
        "text": readable(text, surface), "muted": readable(muted, surface),
        "accent": accent, "accentText": on_accent,
        "accentHover": hsl(h, .90, .68) if dark else hsl(h, .82, .40),
        "selection": hsl(h, .45, .22) if dark else hsl(h, .90, .92),
        "danger": status["bad"],
    }
    p["accentHover"] = readable(p["accentHover"], surface, 3.0)
    p["selectionText"] = readable(p["text"], p["selection"])
    for k, v in status.items():
        p["tint_" + k] = mix(surface, v, 0.12)             # soft pill background behind a status colour
        p["st_" + k] = readable(v, p["tint_" + k])         # text stays readable on its own pill
    p["hoverDanger"] = mix(p["tint_bad"], p["st_bad"], 0.12)
    p["st_bad"] = readable(p["st_bad"], p["hoverDanger"])  # red text stays readable when the button is hovered
    p["danger"] = p["st_bad"]
    p["hoverButton"] = mix(raised, text, 0.08)
    p["hoverAccent"] = mix(accent, "#000000", 0.12)
    return p


PALETTES = {"dark": _palette(True), "light": _palette(False)}


def palette(mode: str = "dark") -> dict:
    return PALETTES["light" if mode == "light" else "dark"]


# Every pair the UI draws, checked by the tests: (foreground key, background key, minimum ratio).
REQUIRED_PAIRS = (
    ("text", "surface", 4.5), ("text", "bg", 4.5), ("muted", "surface", 4.5),
    ("accentText", "accent", 4.5), ("selectionText", "selection", 4.5),
    ("st_ok", "surface", 4.5), ("st_busy", "surface", 4.5),
    ("st_bad", "surface", 4.5), ("st_idle", "surface", 4.5), ("accent", "surface", 3.0),
    ("st_ok", "tint_ok", 4.5), ("st_bad", "tint_bad", 4.5), ("st_busy", "tint_busy", 4.5),
    ("accentText", "hoverAccent", 4.5), ("st_bad", "hoverDanger", 4.5), ("text", "hoverButton", 4.5),
)