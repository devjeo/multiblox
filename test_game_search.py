"""Game search (game_search): fake network, no Roblox."""

import json
import unittest
from urllib.parse import parse_qs, urlsplit

import game_search as gs
import net

PNG = (b"\x89PNG\r\n\x1a\n" + b"x" * 20)


def ok(payload):
    return net.Response(200, {}, json.dumps(payload).encode())


def item(universe, place, name, **kw):
    d = {"universeId": universe, "name": name, "playerCount": 10, "totalUpVotes": 90, "totalDownVotes": 10,
         "isSponsored": False, "creatorName": "", "rootPlaceId": place, "contentType": "Game",
         "ageRecommendationDisplayName": "Maturity: Minimal"}
    d.update(kw)
    return d


def group(*items):
    return {"contentGroupType": "Game", "contents": list(items), "topicId": "Invalid"}


class ParseTests(unittest.TestCase):
    def test_games_are_read_and_ads_skipped(self):
        payload = {"searchResults": [
            group(item(1, 111, "[\U0001F9DA\u200d\u2640\ufe0f] Grow a Garden \U0001F336\ufe0f", creatorName="The Garden Game")),
            group(item(2, 222, "An Ad", isSponsored=True)),
            group(item(3, 333, "Grow a Garden 2", playerCount=2486)),
            group(item(4, 111, "Duplicate place")),
            {"contentGroupType": "Group", "contents": [{"contentType": "Group", "name": "x"}]},
            "junk", group(item(5, 0, "No place"), item(0, 6, "No universe"), "bad"),
        ], "nextPageToken": "tok"}
        games, token = gs.parse_results(payload)
        self.assertEqual(token, "tok")
        self.assertEqual([g.place_id for g in games], [111, 333])
        self.assertEqual(games[0].name, "Grow a Garden")           # emoji and empty [] removed
        self.assertEqual(games[0].creator, "The Garden Game")
        self.assertEqual(games[0].age_label, "Minimal")
        self.assertEqual(games[0].rating, 90)
        self.assertEqual(games[1].players, 2486)
        self.assertEqual(len(gs.parse_results(payload, include_sponsored=True)[0]), 3)

    def test_rating_none_without_votes_and_bad_payloads(self):
        games, _ = gs.parse_results({"searchResults": [group(item(1, 1, "X", totalUpVotes=0, totalDownVotes=0))]})
        self.assertIsNone(games[0].rating)
        self.assertEqual(gs.parse_results(None), ([], None))
        self.assertEqual(gs.parse_results({"searchResults": None}), ([], None))

    def test_clean_name(self):
        self.assertEqual(gs.clean_name("\U0001F331 [UPD\U0001F353] Farm"), "[UPD] Farm")
        self.assertEqual(gs.clean_name("\U0001F331", fallback="Place 5"), "Place 5")


class SearchTests(unittest.TestCase):
    def test_request_shape(self):
        calls = []

        def fake(method, url, **kw):
            calls.append((method, url))
            return ok({"searchResults": []})
        gs.search_games("  grow a garden ", page_token="t1", session_id="sid", fetch=fake)
        parts = urlsplit(calls[0][1])
        self.assertEqual(parts.netloc + parts.path, "apis.roblox.com/search-api/omni-search")
        q = parse_qs(parts.query, keep_blank_values=True)
        self.assertEqual(q["searchQuery"], ["grow a garden"])
        self.assertEqual((q["pageToken"], q["sessionId"], q["pageType"]), (["t1"], ["sid"], ["all"]))

    def test_empty_text_and_errors(self):
        with self.assertRaises(ValueError):
            gs.search_games("   ", fetch=lambda *a, **k: ok({}))

        def boom(*a, **k):
            raise net.HttpError(429)
        with self.assertRaises(net.HttpError):
            gs.search_games("x", fetch=boom)
        self.assertIn("rate-limiting", gs.describe_error(net.HttpError(429)))
        self.assertIn("could not read", gs.describe_error(json.JSONDecodeError("m", "d", 0)))

    def test_looks_like_id(self):
        self.assertTrue(gs.looks_like_id("126884695634066"))
        self.assertTrue(gs.looks_like_id("https://www.roblox.com/games/126884695634066/Grow-a-Garden"))
        self.assertFalse(gs.looks_like_id("grow a garden"))
        self.assertFalse(gs.looks_like_id(""))


class IconTests(unittest.TestCase):
    def test_icon_urls_only_completed_https(self):
        seen = []

        def fake(method, url, **kw):
            seen.append(url)
            return ok({"data": [
                {"targetId": 1, "state": "Completed", "imageUrl": "https://t0.rbxcdn.com/a"},
                {"targetId": 2, "state": "Pending", "imageUrl": "https://t0.rbxcdn.com/b"},
                {"targetId": 3, "state": "Completed", "imageUrl": "http://evil/c"},
                {"targetId": 4, "state": "Completed", "imageUrl": None}, None]})
        self.assertEqual(gs.icon_urls([1, 2, 3, 4, 1], fetch=fake), {1: "https://t0.rbxcdn.com/a"})
        q = parse_qs(urlsplit(seen[0]).query)
        self.assertEqual(q["universeIds"], ["1,2,3,4"])
        self.assertEqual(q["format"], ["Png"])
        self.assertEqual(gs.icon_urls([], fetch=fake), {})
        self.assertEqual(len(seen), 1)

    def test_download_accepts_only_small_png(self):
        good = lambda *a, **k: net.Response(200, {}, PNG)
        self.assertEqual(gs.download("https://t0.rbxcdn.com/a", fetch=good), PNG)
        self.assertIsNone(gs.download("http://t0.rbxcdn.com/a", fetch=good))
        self.assertIsNone(gs.download("https://x/y", fetch=lambda *a, **k: net.Response(200, {}, b"<html>")))
        big = PNG + b"0" * gs.MAX_IMAGE_BYTES
        self.assertIsNone(gs.download("https://x/y", fetch=lambda *a, **k: net.Response(200, {}, big)))


if __name__ == "__main__":
    unittest.main()