"""Tests for theme.py and icons.py. Pure Python: no Tk, no Windows."""

import struct
import unittest
import zlib

import icons
import theme


class SpacingAndType(unittest.TestCase):
    def test_space_is_multiple_of_unit(self):
        for n in (1, 2, 3, 4, 6, 8):
            self.assertEqual(theme.space(n) % theme.UNIT, 0)
        self.assertEqual(theme.space(2), 8)

    def test_type_scale_is_monotonic(self):
        sizes = [theme.size(s) for s in range(-1, 5)]
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual(theme.size(0), theme.BASE_PT)

    def test_type_never_below_minimum(self):
        self.assertGreaterEqual(theme.size(-5), 8)

    def test_row_height_on_grid(self):
        self.assertEqual(theme.row_height() % theme.UNIT, 0)
        self.assertGreater(theme.row_height(), theme.size(0))


class Layout(unittest.TestCase):
    def test_golden_split_sums_and_ratio(self):
        for total in (900, 1100, 1366, 1920):
            main, side = theme.split_golden(total, gap=16)
            self.assertEqual(main + side, total - 16)
            self.assertAlmostEqual(main / side, theme.PHI, delta=0.02)

    def test_golden_split_tiny_total(self):
        self.assertEqual(sum(theme.split_golden(0)), 0)

    def test_window_size_fits_screen_and_grid(self):
        for sw, sh in ((1366, 768), (1920, 1080), (2560, 1440), (800, 600)):
            w, h = theme.window_size(sw, sh)
            self.assertEqual(w % 8, 0)
            self.assertEqual(h % 8, 0)
            self.assertGreaterEqual(w, 880)
            self.assertGreaterEqual(h, 560)
            if sw >= 1366:
                self.assertLessEqual(w, sw)
                self.assertLessEqual(h, sh)

    def test_column_widths_sum_exactly(self):
        weights = {"a": 3, "b": 5, "c": 2, "d": 7}
        for total in (333, 500, 1001):
            self.assertEqual(sum(theme.column_widths(total, weights).values()), total)

    def test_column_widths_empty(self):
        self.assertEqual(theme.column_widths(100, {}), {})
        self.assertEqual(theme.column_widths(0, {"a": 1}), {"a": 0})


class Colour(unittest.TestCase):
    def test_contrast_extremes(self):
        self.assertAlmostEqual(theme.contrast("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(theme.contrast("#777777", "#777777"), 1.0)

    def test_mix_endpoints_and_midpoint(self):
        self.assertEqual(theme.mix("#000000", "#ffffff", 0), "#000000")
        self.assertEqual(theme.mix("#000000", "#ffffff", 1), "#ffffff")
        self.assertEqual(theme.mix("#000000", "#ffffff", 0.5), "#808080")

    def test_readable_reaches_minimum(self):
        fixed = theme.readable("#808080", "#808080", 4.5)
        self.assertGreaterEqual(theme.contrast(fixed, "#808080"), 4.5)

    def test_readable_leaves_good_pairs_alone(self):
        self.assertEqual(theme.readable("#000000", "#ffffff"), "#000000")

    def test_all_required_pairs_pass_in_both_modes(self):
        for mode in ("dark", "light"):
            p = theme.palette(mode)
            for fg, bg, minimum in theme.REQUIRED_PAIRS:
                self.assertGreaterEqual(theme.contrast(p[fg], p[bg]), minimum,
                                        f"{mode}: {fg} on {bg}")

    def test_palette_values_are_hex(self):
        for mode in ("dark", "light"):
            for key, value in theme.palette(mode).items():
                self.assertRegex(value, r"^#[0-9a-f]{6}$", key)

    def test_unknown_mode_falls_back_to_dark(self):
        self.assertEqual(theme.palette("nonsense"), theme.palette("dark"))

    def test_dark_is_darker_than_light(self):
        self.assertLess(theme.luminance(theme.palette("dark")["bg"]),
                        theme.luminance(theme.palette("light")["bg"]))


class Icons(unittest.TestCase):
    def test_every_icon_draws_something_but_not_everything(self):
        for name in icons.icon_names():
            cov = icons.coverage(name, 24, samples=2)
            total = sum(sum(r) for r in cov) / (24 * 24)
            self.assertGreater(total, 0.02, name)
            self.assertLess(total, 0.80, name)

    def test_png_is_valid(self):
        data = icons.render_png("play", 16, "#ffffff")
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        pos, chunks, idat = 8, [], b""
        while pos < len(data):
            n = struct.unpack(">I", data[pos:pos + 4])[0]
            tag, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
            crc = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0]
            self.assertEqual(crc, zlib.crc32(tag + body) & 0xFFFFFFFF)
            chunks.append(tag)
            if tag == b"IDAT":
                idat += body
            pos += 12 + n
        self.assertEqual(chunks, [b"IHDR", b"IDAT", b"IEND"])
        self.assertEqual(len(zlib.decompress(idat)), 16 * (1 + 16 * 4))

    def test_icon_is_horizontally_balanced_where_symmetric(self):
        cov = icons.coverage("plus", 20, samples=4)
        for row in cov:
            self.assertAlmostEqual(sum(row[:10]), sum(row[10:]), delta=0.5)

    def test_iconset_caches(self):
        made = []
        s = icons.IconSet(lambda **kw: made.append(kw) or object(), 16)
        a, b = s.get("play", "#fff000"), s.get("play", "#fff000")
        self.assertIs(a, b)
        self.assertEqual(len(made), 1)
        s.get("play", "#000fff")
        self.assertEqual(len(made), 2)


if __name__ == "__main__":
    unittest.main()