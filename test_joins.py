"""
Join-tools tests (Feature C1: server finder, Feature C2: friend join). Run:  python -m unittest test_joins -v

No Windows, Roblox or network: `fetch` is a scripted fake, and the rate-limit test
fakes net's urlopen. (The private-server tests are added in a later phase.)
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import diagnostics
import friends
import net
import server_finder as sf
from server_finder import Server


def job(n):
    return f"job-{n:08d}"


def srv(n, playing, maximum=10, ping=None):
    return Server(job(n), playing, maximum, ping)


def raw(n, playing, maximum=10, ping=50):
    return {"id": job(n), "playing": playing, "maxPlayers": maximum, "ping": ping, "fps": 59.9, "playerTokens": []}


def page(items, cursor=None):
    return net.Response(200, {}, json.dumps({"previousPageCursor": None, "nextPageCursor": cursor,
                                             "data": items}).encode())


class Fetch:
    """Scripted stand-in for net.request: returns / raises each outcome in turn."""

    def __init__(self, *outcomes):
        self.outcomes, self.urls = list(outcomes), []

    def __call__(self, method, url, **kw):
        assert method == "GET"
        self.urls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


POOL = [srv(1, 3, 10, 80), srv(2, 9, 10, 40), srv(3, 10, 10, 20), srv(4, 1, 10, 90), srv(5, 6, 10, 30)]


class ChooseTests(unittest.TestCase):
    def test_emptiest_returns_lowest_playing(self):
        self.assertEqual(sf.choose(POOL, "emptiest").job_id, job(4))

    def test_fullest_returns_highest_playing(self):
        self.assertEqual(sf.choose(POOL, "fullest").job_id, job(3))   # full, but "fullest" is literal

    def test_best_skips_full(self):
        self.assertEqual(sf.choose(POOL, "best").job_id, job(2))      # 9/10, not the full 10/10

    def test_min_players_filters(self):
        self.assertEqual(sf.choose(POOL, "emptiest", min_players=4).job_id, job(5))
        self.assertEqual(sf.choose(POOL, "best", min_players=4).job_id, job(2))
        self.assertEqual(sf.choose(POOL, "min_players", min_players=4).job_id, job(5))
        self.assertEqual(sf.choose(POOL, "min_players").job_id, job(4))

    def test_no_match_returns_none(self):
        self.assertIsNone(sf.choose([], "best"))
        self.assertIsNone(sf.choose(POOL, "emptiest", min_players=50))
        self.assertIsNone(sf.choose([srv(1, 10, 10)], "best"))        # only a full server

    def test_result_does_not_depend_on_input_order(self):
        for mode in sf.MODES:
            expected = sf.rank(POOL, mode)
            self.assertEqual(sf.rank(list(reversed(POOL)), mode), expected, mode)
            self.assertEqual(sf.rank(POOL[2:] + POOL[:2], mode), expected, mode)

    def test_ties_prefer_lower_ping_then_job_id(self):
        tied = [srv(1, 5, 10, 90), srv(2, 5, 10, 30), srv(3, 5, 10, None), srv(4, 5, 10, 30)]
        self.assertEqual([s.job_id for s in sf.rank(tied, "emptiest")], [job(2), job(4), job(1), job(3)])

    def test_unknown_mode_is_an_error(self):
        with self.assertRaises(ValueError):
            sf.choose(POOL, "random")


class ListServersTests(unittest.TestCase):
    def test_list_servers_parses_and_paginates(self):
        fetch = Fetch(page([raw(1, 3), raw(2, 9)], cursor="CUR+1/="), page([raw(3, 5)]))
        servers, cursor = sf.list_servers(555, fetch=fetch)
        self.assertEqual([s.job_id for s in servers], [job(1), job(2)])
        self.assertEqual((servers[0].playing, servers[0].max_players, servers[0].ping), (3, 10, 50))
        self.assertEqual(cursor, "CUR+1/=")
        servers, cursor = sf.list_servers(555, cursor=cursor, fetch=fetch)
        self.assertEqual((len(servers), cursor), (1, None))
        first, second = (urllib.parse.urlparse(u) for u in fetch.urls)
        self.assertTrue(first.path.endswith("/games/555/servers/Public"))
        self.assertNotIn("cursor", urllib.parse.parse_qs(first.query))
        self.assertEqual(urllib.parse.parse_qs(second.query)["cursor"], ["CUR+1/="])   # encoded and decoded intact
        self.assertEqual(urllib.parse.parse_qs(first.query)["limit"], ["100"])

    def test_bad_entries_and_shapes_are_skipped_not_raised(self):
        junk = [raw(1, 3), "nonsense", {"id": "short", "playing": 1, "maxPlayers": 5},
                {"id": job(2), "playing": "x", "maxPlayers": 5}, {"id": job(3), "playing": True, "maxPlayers": 5},
                {"id": job(4), "playing": 2, "maxPlayers": 0}, {"id": job(5), "playing": -1, "maxPlayers": 5},
                {"id": 123456789, "playing": 1, "maxPlayers": 5}, {"id": job(6), "playing": 2, "maxPlayers": 5}]
        servers, _ = sf.parse_servers({"data": junk})
        self.assertEqual([s.job_id for s in servers], [job(1), job(6)])
        self.assertIsNone(servers[1].ping)
        for bad in (None, [], "x", {}, {"data": None}, {"data": "nope"}):
            self.assertEqual(sf.parse_servers(bad), ([], None))

    def test_invalid_place_id_is_refused_before_any_request(self):
        fetch = Fetch()
        for bad in (0, -5, "123", True, None):
            with self.assertRaises(ValueError):
                sf.list_servers(bad, fetch=fetch)
        self.assertEqual(fetch.urls, [])

    def test_find_servers_reads_pages_dedupes_and_respects_max_pages(self):
        fetch = Fetch(page([raw(1, 3), raw(2, 4)], "c1"), page([raw(2, 4), raw(3, 5)], "c2"),
                      page([raw(4, 6)], "c3"))
        found = sf.find_servers(1, max_pages=2, fetch=fetch)
        self.assertEqual([s.job_id for s in found.servers], [job(1), job(2), job(3)])   # job 2 listed once
        self.assertEqual((found.pages, found.partial, len(fetch.urls)), (2, False, 2))

    def test_find_servers_stops_at_the_last_page(self):
        fetch = Fetch(page([raw(1, 3)], "c1"), page([raw(2, 3)], None))
        found = sf.find_servers(1, max_pages=5, fetch=fetch)
        self.assertEqual((len(found.servers), found.pages, len(fetch.urls)), (2, 2, 2))

    def test_a_later_page_failing_keeps_what_was_found(self):
        fetch = Fetch(page([raw(1, 3)], "c1"), net.HttpError(429))
        found = sf.find_servers(1, fetch=fetch)
        self.assertEqual(([s.job_id for s in found.servers], found.partial), ([job(1)], True))

    def test_the_first_page_failing_raises(self):
        with self.assertRaises(net.HttpError):
            sf.find_servers(1, fetch=Fetch(net.HttpError(429)))

    def test_find_and_choose(self):
        fetch = Fetch(page([raw(1, 3), raw(2, 9), raw(3, 10)], "c1"), page([raw(4, 1)]))
        self.assertEqual(sf.find_and_choose(1, "best", fetch=fetch).job_id, job(2))
        self.assertIsNone(sf.find_and_choose(1, "best", fetch=Fetch(page([]))))


class RateLimitTests(unittest.TestCase):
    def test_rate_limit_retries_then_raises(self):
        sleeps, calls = [], []

        def fake_open(req, timeout):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 429, "slow", {"Retry-After": "2"}, io.BytesIO(b""))
        with mock.patch.object(net, "_open", fake_open), mock.patch.object(net, "_sleep", sleeps.append):
            with self.assertRaises(net.HttpError) as cm:
                sf.find_servers(1)                       # the real net.request, so its retries are in play
        self.assertEqual(cm.exception.status, 429)
        self.assertEqual((len(calls), sleeps), (3, [2.0, 2.0]))
        self.assertIn("rate-limiting", sf.describe_error(cm.exception))

    def test_a_429_that_clears_up_succeeds(self):
        outcomes = [urllib.error.HTTPError("u", 429, "slow", {"Retry-After": "1"}, io.BytesIO(b"")),
                    mock.MagicMock()]
        resp = outcomes[1]
        resp.__enter__.return_value = resp
        resp.status, resp.headers = 200, {}
        resp.read.return_value = json.dumps({"data": [raw(1, 3)]}).encode()

        def fake_open(req, timeout):
            out = outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        with mock.patch.object(net, "_open", fake_open), mock.patch.object(net, "_sleep", lambda s: None):
            self.assertEqual(len(sf.find_servers(1).servers), 1)

    def test_error_messages(self):
        self.assertIn("Could not reach", sf.describe_error(net.HttpError(0, reason="dns down")))
        self.assertIn("no public server list", sf.describe_error(net.HttpError(404)))
        self.assertIn("HTTP 503", sf.describe_error(net.HttpError(503)))
        self.assertIn("positive", sf.describe_error(ValueError("place_id must be a positive integer")))


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        sf.last_result.clear()
        self.addCleanup(sf.last_result.clear)

    def test_last_result_is_reported(self):
        self.assertIn("not used yet", diagnostics.check_join_tools(include_network=False)[0].detail)
        sf.record_result(555, "best", 120, job(7))
        [r] = diagnostics.check_join_tools(include_network=False)
        self.assertEqual(r.level, "INFO")
        for part in ("place 555", "mode best", "120 servers", job(7)):
            self.assertIn(part, r.detail)
        sf.record_result(555, "best", 0, None, error="Roblox is rate-limiting server lists right now.")
        [r] = diagnostics.check_join_tools(include_network=False)
        self.assertEqual(r.level, "WARN")

    def test_endpoint_probe_classification(self):
        cases = [((200, {}), "OK"), ((400, {}), "OK"), ((404, {}), "WARN"), ((429, {}), "WARN"),
                 ((503, {}), "WARN"), ((None, "dns failure"), "FAIL")]
        for answer, level in cases:
            with mock.patch.object(diagnostics, "_probe", lambda url, method, a=answer: a):
                self.assertEqual(diagnostics.check_join_tools(True)[0].level, level, answer)

    def test_run_all_includes_join_tools_and_a_failing_probe_never_crashes_it(self):
        results = diagnostics.run_all(store=None, holder=None, include_network=False)
        self.assertTrue(any(r.name == "Server finder" for r in results))
        with mock.patch.object(diagnostics, "_probe", lambda url, method: (None, "offline")):
            results = diagnostics.run_all(store=None, holder=None, include_network=True)
        self.assertTrue(any(r.name == "Server list API" and r.level == "FAIL" for r in results))
        self.assertTrue(all("crashed" not in r.detail for r in results))


# ==========================================================================
# Feature C2: friend join
# ==========================================================================
def ok(payload):
    return net.Response(200, {}, json.dumps(payload).encode())


class AnyFetch:
    """Scripted stand-in for net.request that records method, url and keyword arguments."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


JOB = "11111111-2222-3333-4444-555555555555"


def pres(state=2, place=606849621, root=606849621, game=JOB, user=42, where="Jailbreak"):
    return {"userPresences": [{"userPresenceType": state, "lastLocation": where, "placeId": place,
                               "rootPlaceId": root, "gameId": game, "universeId": 1, "userId": user,
                               "lastOnline": "2026-10-16T08:00:00Z"}]}


class FindUserTests(unittest.TestCase):
    def test_find_user_by_id_and_name(self):
        by_id = AnyFetch(ok({"id": 42, "name": "Builderman", "displayName": "Builder"}))
        user = friends.find_user("42", fetch=by_id)
        self.assertEqual(user, friends.User(42, "Builderman", "Builder"))
        self.assertEqual(by_id.calls[0][:2], ("GET", "https://users.roblox.com/v1/users/42"))

        by_name = AnyFetch(ok({"data": [{"requestedUsername": "builderman", "id": 42, "name": "Builderman",
                                         "displayName": "Builder"}]}))
        self.assertEqual(friends.find_user("builderman", fetch=by_name).id, 42)
        method, url, kw = by_name.calls[0]
        self.assertEqual((method, url), ("POST", friends.USERNAMES_URL))
        self.assertEqual(kw["json_body"], {"usernames": ["builderman"], "excludeBannedUsers": True})
        self.assertNotIn("cookie", kw)  # name lookup is anonymous

    def test_at_sign_and_profile_link_are_understood(self):
        f = AnyFetch(ok({"id": 7, "name": "A_b"}), ok({"id": 9, "name": "Zed"}))
        self.assertEqual(friends.find_user("https://www.roblox.com/users/7/profile", fetch=f).id, 7)
        self.assertEqual(friends.find_user("  https://www.roblox.com/de/users/9/profile ", fetch=f).id, 9)
        by_name = AnyFetch(ok({"data": [{"id": 5, "name": "Cool_Kid"}]}))
        self.assertEqual(friends.find_user("@Cool_Kid", fetch=by_name).name, "Cool_Kid")
        self.assertEqual(by_name.calls[0][2]["json_body"]["usernames"], ["Cool_Kid"])

    def test_unknown_user_is_none_and_text_that_cannot_be_a_name_makes_no_request(self):
        for text in ("", "   ", "ab", "a" * 21, "has space", "bad-char!", "0", "-5", "\u0663\u0664"):
            f = AnyFetch()
            self.assertIsNone(friends.find_user(text, fetch=f), text)
            self.assertEqual(f.calls, [], text)
        self.assertIsNone(friends.find_user("nobody_here", fetch=AnyFetch(ok({"data": []}))))
        self.assertIsNone(friends.find_user("123456", fetch=AnyFetch(net.HttpError(404))))
        self.assertIsNone(friends.find_user("123456", fetch=AnyFetch(net.HttpError(400))))

    def test_other_failures_propagate(self):
        with self.assertRaises(net.HttpError) as cm:
            friends.find_user("123456", fetch=AnyFetch(net.HttpError(429)))
        self.assertIn("rate-limiting", friends.describe_error(cm.exception))
        with self.assertRaises(net.HttpError):
            friends.find_user("someone", fetch=AnyFetch(net.HttpError(0, reason="dns down")))

    def test_odd_payloads_are_none_not_exceptions(self):
        for payload in ([], "x", {"data": "x"}, {"data": [None, 5, {"id": "1", "name": "A"}, {"id": 3}]}):
            self.assertIsNone(friends.find_user("someone", fetch=AnyFetch(ok(payload))), payload)
        self.assertIsNone(friends.find_user("123456", fetch=AnyFetch(ok({"id": -1, "name": "A"}))))


class PresenceTests(unittest.TestCase):
    def test_visible_server_resolves(self):
        p = friends.get_presence("COOKIE", 42, fetch=AnyFetch(ok(pres())))
        self.assertEqual(friends.resolve_join_target(p), (606849621, JOB))
        self.assertIn("Joinable", friends.describe_presence(p))
        self.assertIn("Jailbreak", friends.describe_presence(p))

    def test_presence_hidden_returns_none(self):
        # Default privacy: Roblox says "in game" but withholds where.
        p = friends.get_presence("COOKIE", 42, fetch=AnyFetch(ok(pres(place=None, root=None, game=None))))
        self.assertEqual(p.state, 2)
        self.assertIsNone(friends.resolve_join_target(p))
        self.assertIn("unavailable", friends.describe_presence(p))
        # A place without a server ID, or a server ID without a place, is just as unusable.
        self.assertIsNone(friends.resolve_join_target(friends.get_presence("C", 42, fetch=AnyFetch(ok(pres(game=None))))))
        self.assertIsNone(friends.resolve_join_target(friends.get_presence("C", 42, fetch=AnyFetch(ok(pres(place=None))))))
        # A too-short game ID is treated as missing, like the launcher's job ID check.
        self.assertIsNone(friends.resolve_join_target(friends.get_presence("C", 42, fetch=AnyFetch(ok(pres(game="abc"))))))

    def test_resolve_join_target_offline_none(self):
        for state in (0, 1, 3):  # offline, online, Studio: never joinable, even if ids are present
            p = friends.get_presence("C", 42, fetch=AnyFetch(ok(pres(state=state))))
            self.assertIsNone(friends.resolve_join_target(p), state)
            self.assertIn("Not joinable", friends.describe_presence(p))
        self.assertIsNone(friends.resolve_join_target(None))

    def test_request_shape_carries_cookie_to_net_only(self):
        f = AnyFetch(ok(pres()))
        friends.get_presence("SECRET-COOKIE", 42, fetch=f)
        method, url, kw = f.calls[0]
        self.assertEqual((method, url), ("POST", "https://presence.roblox.com/v1/presence/users"))
        self.assertEqual(kw["json_body"], {"userIds": [42]})
        self.assertEqual(kw["cookie"], "SECRET-COOKIE")

    def test_entry_for_another_user_or_bad_shapes_give_none(self):
        self.assertIsNone(friends.get_presence("C", 42, fetch=AnyFetch(ok(pres(user=99)))))
        for payload in ({}, [], {"userPresences": []}, {"userPresences": ["x", None]},
                        {"userPresences": [{"userId": 42}]}, {"userPresences": [{"userId": 42, "userPresenceType": "2"}]}):
            self.assertIsNone(friends.get_presence("C", 42, fetch=AnyFetch(ok(payload))), payload)

    def test_invalid_user_id_is_refused_before_any_request(self):
        for bad in (0, -1, True, "42", None):
            f = AnyFetch()
            with self.assertRaises(ValueError):
                friends.get_presence("C", bad, fetch=f)
            self.assertEqual(f.calls, [], bad)

    def test_a_rejected_cookie_is_reported_without_leaking_it(self):
        with self.assertRaises(net.HttpError) as cm:
            friends.get_presence("SECRET-COOKIE", 42, fetch=AnyFetch(net.HttpError(401, url=friends.PRESENCE_URL)))
        text = friends.describe_error(cm.exception)
        self.assertIn("rejected", text)
        self.assertNotIn("SECRET-COOKIE", text + str(cm.exception))

    def test_presence_goes_through_net_with_the_csrf_handshake(self):
        seen = []
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.status, resp.headers = 200, {}
        resp.read.return_value = json.dumps(pres()).encode()
        outcomes = [urllib.error.HTTPError(friends.PRESENCE_URL, 403, "csrf", {"x-csrf-token": "TOKEN"}, io.BytesIO(b"")),
                    resp]

        def fake_open(req, timeout):
            seen.append(dict(req.header_items()))
            out = outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        with mock.patch.object(net, "_open", fake_open), mock.patch.object(net, "_sleep", lambda s: None):
            p = friends.get_presence("COOKIE", 42)  # the real net.request
        self.assertEqual(friends.resolve_join_target(p), (606849621, JOB))
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[1].get("X-csrf-token"), "TOKEN")
        self.assertIn(".ROBLOSECURITY=COOKIE", seen[1].get("Cookie", ""))

    def test_error_messages(self):
        self.assertIn("Could not reach", friends.describe_error(net.HttpError(0, reason="dns down")))
        self.assertIn("HTTP 503", friends.describe_error(net.HttpError(503)))
        self.assertIn("positive", friends.describe_error(ValueError("user_id must be a positive integer")))
        self.assertIn("no status", friends.describe_presence(None))


class FriendDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        friends.last_result.clear()
        sf.last_result.clear()
        self.addCleanup(friends.last_result.clear)
        self.addCleanup(sf.last_result.clear)

    def test_unused_session_adds_no_friend_line(self):
        self.assertFalse(any(r.name == "Friend join" for r in diagnostics.check_join_tools(include_network=False)))

    def test_last_lookup_is_reported_without_secrets(self):
        friends.record_result(42, "unavailable")
        [r] = [r for r in diagnostics.check_join_tools(include_network=False) if r.name == "Friend join"]
        self.assertEqual(r.level, "INFO")
        self.assertIn("user 42", r.detail)
        self.assertIn("unavailable", r.detail)
        friends.record_result(42, "error", error="Roblox rejected the selected account's login.")
        [r] = [r for r in diagnostics.check_join_tools(include_network=False) if r.name == "Friend join"]
        self.assertEqual(r.level, "WARN")
        friends.record_result(None, "no such user")
        [r] = [r for r in diagnostics.check_join_tools(include_network=False) if r.name == "Friend join"]
        self.assertIn("no user", r.detail)

    def test_user_api_probe_classification(self):
        cases = [((200, {}), "OK"), ((404, {}), "WARN"), ((429, {}), "WARN"), ((503, {}), "WARN"),
                 ((None, "dns failure"), "FAIL")]
        for answer, level in cases:
            with mock.patch.object(diagnostics, "_probe", lambda url, method, a=answer: a):
                [r] = [r for r in diagnostics.check_join_tools(True) if r.name == "User lookup API"]
            self.assertEqual(r.level, level, answer)


if __name__ == "__main__":
    unittest.main()