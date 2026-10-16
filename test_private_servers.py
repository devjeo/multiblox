"""Private-server list (private_servers): fake network, no Roblox."""

import json
import unittest
from urllib.parse import parse_qs, urlsplit

import net
import private_links
import private_servers as ps

CODE_A = "11111111-aaaa-bbbb-cccc-000000000001"
CODE_B = "22222222-aaaa-bbbb-cccc-000000000002"


def ok(payload):
    return net.Response(200, {}, json.dumps(payload).encode())


def entry(name, owner_id, owner_name, playing=1, maximum=10, code=None):
    d = {"id": None, "name": name, "playing": playing, "maxPlayers": maximum, "vipServerId": 1,
         "owner": {"id": owner_id, "name": owner_name}}
    if code:
        d["accessCode"] = code
    return d


class ParseTests(unittest.TestCase):
    def test_mine_vs_friend_and_codes(self):
        payload = {"data": [entry("Mine", 5, "me", code=CODE_A), entry("Pal's", 9, "pal", code=CODE_B),
                            entry("NoCode", 9, "pal")], "nextPageCursor": "abc"}
        servers, cursor = ps.parse_servers(payload, 5)
        self.assertEqual(cursor, "abc")
        self.assertEqual([s.kind for s in servers], ["mine", "friend", "friend"])
        self.assertEqual([s.joinable for s in servers], [True, True, False])
        self.assertNotIn(CODE_A, repr(servers[0]))   # codes are secrets

    def test_bad_entries_are_skipped(self):
        payload = {"data": [None, 3, {"name": "x"}, entry("Ok", 1, "a", playing=-1),
                            entry("Fine", 1, "a", code="bad code!")]}
        servers, cursor = ps.parse_servers(payload, 1)
        self.assertEqual([s.name for s in servers], ["Fine"])
        self.assertFalse(servers[0].joinable)         # malformed code is dropped
        self.assertIsNone(cursor)
        self.assertEqual(ps.parse_servers("junk", 1), ([], None))


class FindTests(unittest.TestCase):
    def test_request_shape_and_cookie(self):
        calls = []

        def fake(method, url, *, cookie=None, **kw):
            calls.append((method, url, cookie))
            return ok({"data": []})
        ps.find_private_servers(126884695634066, "COOKIE", 5, fetch=fake)
        method, url, cookie = calls[0]
        parts = urlsplit(url)
        self.assertEqual((method, cookie), ("GET", "COOKIE"))
        self.assertEqual(parts.path, "/v1/games/126884695634066/private-servers")
        q = parse_qs(parts.query)
        self.assertEqual(q["sortOrder"], ["Desc"])
        self.assertEqual(q["excludeFullGames"], ["false"])

    def test_pages_dedupe_and_order(self):
        pages = {None: {"data": [entry("Zed", 9, "pal", code=CODE_B), entry("Alpha", 5, "me", code=CODE_A)],
                        "nextPageCursor": "p2"},
                 "p2": {"data": [entry("Alpha", 5, "me", code=CODE_A), entry("Beta", 9, "pal")]}}

        def fake(method, url, *, cookie=None, **kw):
            return ok(pages[parse_qs(urlsplit(url).query).get("cursor", [None])[0]])
        found = ps.find_private_servers(1, "C", 5, fetch=fake)
        self.assertEqual([s.name for s in found.servers], ["Alpha", "Beta", "Zed"])   # mine first
        self.assertEqual(found.pages, 2)
        self.assertFalse(found.partial)

    def test_later_page_error_is_partial_first_page_error_raises(self):
        state = {"n": 0}

        def flaky(method, url, *, cookie=None, **kw):
            state["n"] += 1
            if state["n"] == 1:
                return ok({"data": [entry("A", 5, "me")], "nextPageCursor": "p2"})
            raise net.HttpError(429)
        found = ps.find_private_servers(1, "C", 5, fetch=flaky)
        self.assertTrue(found.partial)
        self.assertEqual(len(found.servers), 1)

        def boom(*a, **k):
            raise net.HttpError(401)
        with self.assertRaises(net.HttpError):
            ps.find_private_servers(1, "C", 5, fetch=boom)

    def test_input_validation(self):
        with self.assertRaises(ValueError):
            ps.find_private_servers(0, "C", 5, fetch=lambda *a, **k: ok({}))
        with self.assertRaises(ValueError):
            ps.find_private_servers(1, "", 5, fetch=lambda *a, **k: ok({}))


class HelpersTests(unittest.TestCase):
    def setUp(self):
        servers, _ = ps.parse_servers({"data": [entry("Mine", 5, "me", code=CODE_A),
                                                entry("Pal's", 9, "pal", code=CODE_B),
                                                entry("NoCode", 9, "pal")]}, 5)
        self.servers = servers

    def test_filter(self):
        self.assertEqual(len(ps.apply_filter(self.servers, "all")), 3)
        self.assertEqual([s.name for s in ps.apply_filter(self.servers, "mine")], ["Mine"])
        self.assertEqual(len(ps.apply_filter(self.servers, "friend")), 2)
        with self.assertRaises(ValueError):
            ps.apply_filter(self.servers, "nope")

    def test_extra_for_feeds_the_launcher(self):
        extra = ps.extra_for(self.servers[0])
        self.assertTrue(private_links.is_private(extra))
        self.assertEqual(private_links.validate(extra), (CODE_A, "", private_links.DEFAULT_ORIGIN))
        self.assertIsNone(ps.extra_for(self.servers[2]))

    def test_describe_error(self):
        self.assertIn("not logged in", ps.describe_error(net.HttpError(401)))
        self.assertIn("rate-limiting", ps.describe_error(net.HttpError(429)))
        self.assertIn("no private-server list", ps.describe_error(net.HttpError(404)))


if __name__ == "__main__":
    unittest.main()