"""
A small icon set drawn from geometry, so the app ships no image files and needs no Pillow.

Each icon is a list of primitives in a 0..1 square (lines, rings, arcs, filled polygons and
rounded rectangles). For every pixel we take 4x4 sub-samples, test each against the primitives with
a distance formula, and use the covered fraction as alpha (anti-aliasing). The result is encoded as
a PNG with zlib/struct and handed to tkinter.PhotoImage(data=...).

Pure Python: no tkinter import, so it is unit-tested off Windows (test_theme.py).
"""

from __future__ import annotations

import base64
import math
import struct
import zlib

STROKE = 0.095   # line width as a fraction of the icon size


# ---------------------------------------------------------------- primitives
def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _in_poly(px, py, pts) -> bool:
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > py) != (yj > py) and px < (xj - xi) * (py - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _rrect_dist(px, py, x, y, w, h, r) -> float:
    """Signed distance to a rounded rectangle (negative inside)."""
    qx, qy = abs(px - (x + w / 2)) - (w / 2 - r), abs(py - (y + h / 2)) - (h / 2 - r)
    return math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r


def _arc_dist(px, py, cx, cy, r, a0, a1) -> float:
    ang = math.degrees(math.atan2(py - cy, px - cx)) % 360
    span = (a1 - a0) % 360 or 360
    if (ang - a0) % 360 <= span:
        return abs(math.hypot(px - cx, py - cy) - r)
    ends = [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))) for a in (a0, a0 + span)]
    return min(math.hypot(px - ex, py - ey) for ex, ey in ends)


def _covered(prim, px, py, half: float) -> bool:
    kind = prim[0]
    if kind == "line":
        return _seg_dist(px, py, *prim[1:]) <= half
    if kind == "poly":          # stroked polyline
        pts = prim[1]
        return any(_seg_dist(px, py, *pts[i], *pts[i + 1]) <= half for i in range(len(pts) - 1))
    if kind == "ring":
        return abs(math.hypot(px - prim[1], py - prim[2]) - prim[3]) <= half
    if kind == "arc":
        return _arc_dist(px, py, *prim[1:]) <= half
    if kind == "dot":
        return math.hypot(px - prim[1], py - prim[2]) <= prim[3]
    if kind == "fill":          # filled polygon
        return _in_poly(px, py, prim[1])
    if kind == "rrect":         # filled rounded rectangle
        return _rrect_dist(px, py, *prim[1:]) <= 0
    if kind == "rrect_line":    # outlined rounded rectangle
        return abs(_rrect_dist(px, py, *prim[1:])) <= half
    return False


def _arrow_head(cx, cy, r, ang, size=0.17):
    """Filled triangle at the end of a circular arc, pointing along the direction of travel."""
    a = math.radians(ang)
    tip = (cx + r * math.cos(a), cy + r * math.sin(a))
    tx, ty = -math.sin(a), math.cos(a)           # tangent, clockwise
    nx, ny = math.cos(a), math.sin(a)
    base = (tip[0] - tx * size, tip[1] - ty * size)
    return ("fill", [(tip[0] + tx * size * .55, tip[1] + ty * size * .55),
                     (base[0] + nx * size * .6, base[1] + ny * size * .6),
                     (base[0] - nx * size * .6, base[1] - ny * size * .6)])


# ---------------------------------------------------------------- the icon set
ICONS: dict[str, list] = {
    "play": [("fill", [(.30, .16), (.30, .84), (.84, .50)])],
    "stop": [("rrect", .20, .20, .60, .60, .10)],
    "plus": [("line", .50, .20, .50, .80), ("line", .20, .50, .80, .50)],
    "close": [("line", .24, .24, .76, .76), ("line", .76, .24, .24, .76)],
    "trash": [("line", .16, .26, .84, .26), ("line", .40, .26, .40, .14), ("line", .40, .14, .60, .14),
              ("line", .60, .14, .60, .26), ("poly", [(.24, .26), (.29, .86), (.71, .86), (.76, .26)]),
              ("line", .42, .42, .43, .72), ("line", .58, .42, .57, .72)],
    "refresh": [("arc", .50, .50, .30, 200, 330), ("arc", .50, .50, .30, 20, 150), _arrow_head(.5, .5, .30, 330),
                _arrow_head(.5, .5, .30, 150)],
    "grid": [("rrect_line", .17, .17, .28, .28, .06), ("rrect_line", .55, .17, .28, .28, .06),
             ("rrect_line", .17, .55, .28, .28, .06), ("rrect_line", .55, .55, .28, .28, .06)],
    "clock": [("ring", .50, .50, .36), ("line", .50, .50, .50, .26), ("line", .50, .50, .67, .58)],
    "search": [("ring", .43, .43, .25), ("line", .62, .62, .84, .84)],
    "sliders": [("line", .14, .30, .86, .30), ("line", .14, .70, .86, .70), ("dot", .36, .30, .11),
                ("dot", .64, .70, .11)],
    "pulse": [("poly", [(.08, .54), (.30, .54), (.40, .24), (.54, .78), (.64, .46), (.72, .54), (.92, .54)])],
    "power": [("arc", .50, .54, .30, 300, 240), ("line", .50, .12, .50, .50)],
    "window": [("rrect_line", .12, .18, .76, .64, .08), ("line", .12, .38, .88, .38)],
    "users": [("ring", .38, .34, .14), ("arc", .38, .86, .26, 200, 340), ("ring", .70, .40, .10),
              ("arc", .70, .86, .20, 215, 325)],
    "bell": [("arc", .50, .46, .26, 180, 360), ("line", .24, .46, .20, .72), ("line", .76, .46, .80, .72),
             ("line", .16, .72, .84, .72), ("line", .44, .84, .56, .84)],
    "link": [("rrect_line", .10, .36, .42, .28, .14), ("rrect_line", .48, .36, .42, .28, .14)],
}


def icon_names() -> list[str]:
    return sorted(ICONS)


# ---------------------------------------------------------------- rasteriser + PNG
def coverage(name: str, px_size: int, samples: int = 4) -> list[list[float]]:
    """Per-pixel coverage 0..1 for the icon at `px_size` x `px_size` (rows of floats)."""
    prims = ICONS[name]
    half = STROKE / 2
    pad = 0.0
    out: list[list[float]] = []
    for y in range(px_size):
        row = []
        for x in range(px_size):
            hit = 0
            for sy in range(samples):
                for sx in range(samples):
                    fx = (x + (sx + .5) / samples) / px_size
                    fy = (y + (sy + .5) / samples) / px_size
                    if any(_covered(p, fx, fy, half) for p in prims):
                        hit += 1
            row.append(hit / (samples * samples))
        out.append(row)
    return out


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def render_png(name: str, px_size: int, color: str, samples: int = 4) -> bytes:
    """RGBA PNG bytes of the icon in `color` ('#rrggbb') on a transparent background."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    raw = bytearray()
    for row in coverage(name, px_size, samples):
        raw.append(0)  # filter: none
        for c in row:
            raw += bytes((r, g, b, round(255 * c)))
    header = struct.pack(">IIBBBBB", px_size, px_size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b"")


def png_base64(name: str, px_size: int, color: str) -> str:
    return base64.b64encode(render_png(name, px_size, color)).decode("ascii")


class IconSet:
    """Builds tkinter PhotoImages on demand and keeps references (Tk drops images that are garbage
    collected). `photo_factory` is tkinter.PhotoImage in the app and a stub in tests."""

    def __init__(self, photo_factory, px_size: int = 16) -> None:
        self._factory, self.px, self._cache = photo_factory, px_size, {}

    def get(self, name: str, color: str, px_size: int | None = None):
        key = (name, color, px_size or self.px)
        if key not in self._cache:
            self._cache[key] = self._factory(data=png_base64(name, px_size or self.px, color))
        return self._cache[key]

    def clear(self) -> None:
        self._cache.clear()