"""
Join-tools tests (C1 server finder, C2 friend join, C3 private servers). Run:  python -m unittest test_joins -v

No Windows, Roblox or network: `fetch` is a scripted fake, and the rate-limit test
fakes net's urlopen. The private-server tests reuse World / make_manager from test_phase6.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest import mock

import autorejoin
import diagnostics
import friends
import launcher
import net
import private_links
import safelog
import server_finder as sf
from account_store import Account
from pathlib import Path
from places import PlaceInput, parse_place_input, parse_place_link
from server_finder import Server
from sessions import SessionStore, restore
from test_phase6 import FakeStore, World, make_accounts, make_manager, tick_for


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



# ==========================================================================
# Feature C3: private servers (experimental)
# ==========================================================================
PLACE = 126884695634066
ACCESS = "379bbea0-c161-4313-9fd5-49635cb7eb4c"
LINK = "0123456789abcdef0123456789abcdef"


def launch_text(inner, ticket="TICKET"):
    """A roblox-player: launch link around a PlaceLauncher URL, shaped like the real capture."""
    import urllib.parse
    return ("roblox-player:1+launchmode:play+gameinfo:" + ticket + "+launchtime:1792113104456"
            "+placelauncherurl:" + urllib.parse.quote(inner, safe="")
            + "+browsertrackerid:1792061276307001+robloxLocale:en_us+gameLocale:en_us+LaunchExp:InApp")


PRIVATE_INNER = ("https://www.roblox.com/Game/PlaceLauncher.ashx?request=RequestPrivateGame"
                 f"&browserTrackerId=1792061276307001&placeId={PLACE}&accessCode={ACCESS}&linkCode="
                 "&joinAttemptId=0b8ee635-9db3-41a1-bff8-c96a7bc17b42&joinAttemptOrigin=privateServerListJoin")
FOLLOW_INNER = ("https://www.roblox.com/Game/PlaceLauncher.ashx?request=RequestFollowUser"
                "&browserTrackerId=1792061276307001&userId=11152340993"
                "&joinAttemptId=b6fad764-5ee9-42ac-9a19-dd46cb271251&joinAttemptOrigin=JoinUser")
EXTRA = {"accessCode": ACCESS, "linkCode": None, "origin": "privateServerListJoin"}


class PlaceLinkTests(unittest.TestCase):
    def test_parse_place_link_variants(self):
        p = parse_place_link(launch_text(PRIVATE_INNER))             # the real capture's shape
        self.assertEqual((p.place_id, p.access_code, p.link_code, p.origin, p.verified, p.is_private),
                         (PLACE, ACCESS, None, "privateServerListJoin", True, True))
        self.assertEqual(parse_place_link(PRIVATE_INNER).access_code, ACCESS)               # bare launcher URL
        import urllib.parse
        self.assertEqual(parse_place_link(urllib.parse.quote(PRIVATE_INNER, safe="")).access_code, ACCESS)

        by_link = parse_place_link(PRIVATE_INNER.replace(f"accessCode={ACCESS}&linkCode=", f"accessCode=&linkCode={LINK}"))
        self.assertEqual((by_link.access_code, by_link.link_code, by_link.verified), (None, LINK, True))

        web = parse_place_link(f"https://www.roblox.com/games/{PLACE}/Grow-a-Garden?privateServerLinkCode={LINK}")
        self.assertEqual((web.place_id, web.link_code, web.verified, web.is_private), (PLACE, LINK, False, True))

        share = parse_place_link("https://www.roblox.com/share?code=abcdef0123456789&type=Server")
        self.assertEqual((share.share_code, share.place_id), ("abcdef0123456789", None))

        self.assertEqual(parse_place_link(str(PLACE)), PlaceInput(PLACE))
        self.assertEqual(parse_place_link(f"https://www.roblox.com/games/{PLACE}/x").place_id, PLACE)

        public = parse_place_link(launch_text(f"https://assetgame.roblox.com/game/PlaceLauncher.ashx?request=RequestGame"
                                              f"&browserTrackerId=1&placeId={PLACE}&isPlayTogether=false"))
        self.assertEqual((public.place_id, public.is_private), (PLACE, False))
        job = parse_place_link(launch_text(f"https://assetgame.roblox.com/game/PlaceLauncher.ashx?request=RequestGameJob"
                                           f"&browserTrackerId=1&placeId={PLACE}&gameId={JOB}&isPlayTogether=false"))
        self.assertEqual((job.place_id, job.job_id), (PLACE, JOB))

    def test_text_without_a_usable_server_is_none(self):
        for text in ("", "   ", "hello", "roblox-player:1+launchmode:play", launch_text(FOLLOW_INNER),
                     PRIVATE_INNER.replace("placeId=" + str(PLACE), "placeId=abc"),
                     PRIVATE_INNER.replace(f"accessCode={ACCESS}", "accessCode=a%20b%3Brm"),   # not a plain code
                     PRIVATE_INNER.replace(f"accessCode={ACCESS}", "accessCode="),              # nothing to join with
                     "https://www.roblox.com/share?code=bad%20code"):
            self.assertIsNone(parse_place_link(text), text)

    def test_old_parse_place_input_unchanged(self):
        self.assertEqual(parse_place_input("12345"), (12345, None))
        self.assertEqual(parse_place_input("https://www.roblox.com/games/606849621/Jailbreak"), (606849621, "Jailbreak"))
        self.assertEqual(parse_place_input("https://x.com/?placeId=77"), (77, None))
        self.assertEqual(parse_place_input("nonsense"), (None, None))

    def test_codes_are_hidden_from_repr(self):
        p = parse_place_link(launch_text(PRIVATE_INNER))
        self.assertNotIn(ACCESS, repr(p) + str(p))
        self.assertNotIn(LINK, repr(parse_place_link(f"https://www.roblox.com/games/{PLACE}/x?privateServerLinkCode={LINK}")))

    def test_assess_and_extra_from(self):
        ok_, msg = private_links.assess(parse_place_link(launch_text(PRIVATE_INNER)))
        self.assertTrue(ok_)
        self.assertEqual(private_links.extra_from(parse_place_link(launch_text(PRIVATE_INNER))), EXTRA)
        for text, word in ((launch_text(FOLLOW_INNER), "private server"), ("https://www.roblox.com/share?code=abcdef0123", "share link"),
                           (f"https://www.roblox.com/games/{PLACE}/x?privateServerLinkCode={LINK}", "web link"),
                           (str(PLACE), "public")):
            parsed = parse_place_link(text)
            ok_, msg = private_links.assess(parsed)
            self.assertFalse(ok_, text)
            self.assertIn(word, msg)
            self.assertIsNone(private_links.extra_from(parsed), text)   # only verified launch links arm
        self.assertFalse(private_links.assess(None)[0])
        self.assertNotIn(ACCESS, msg)


class LaunchUriTests(unittest.TestCase):
    def fixed(self):
        import contextlib
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(launcher.random, "randint", lambda a, b: 111111111111))
        stack.enter_context(mock.patch.object(launcher.uuid, "uuid4", lambda: "ATTEMPT-UUID"))
        stack.enter_context(mock.patch.object(launcher.time, "time", lambda: 1700000000.0))
        return stack

    def expected(self, place_url):
        import urllib.parse
        return ("roblox-player:1+launchmode:play+gameinfo:T+launchtime:1700000000000"
                f"+placelauncherurl:{urllib.parse.quote(place_url, safe='')}"
                "+browsertrackerid:111111111111+robloxLocale:en_us+gameLocale:en_us+channel:+LaunchExp:InApp")

    def test_build_launch_uri_unchanged_without_extra(self):
        any_server = ("https://assetgame.roblox.com/game/PlaceLauncher.ashx?request=RequestGame"
                      "&browserTrackerId=111111111111&placeId=5&isPlayTogether=false"
                      "&joinAttemptId=ATTEMPT-UUID&joinAttemptOrigin=PlayButton")
        one_job = ("https://assetgame.roblox.com/game/PlaceLauncher.ashx?request=RequestGameJob"
                   "&browserTrackerId=111111111111&placeId=5&gameId=jobjobjob1&isPlayTogether=false")
        with self.fixed():
            for extra in (None, {}, {"accessCode": None, "linkCode": ""}):
                self.assertEqual(launcher.build_launch_uri("T", 5, extra=extra), self.expected(any_server))
                self.assertEqual(launcher.build_launch_uri("T", 5, "jobjobjob1", extra=extra), self.expected(one_job))
            self.assertEqual(launcher.build_launch_uri("T", 5), self.expected(any_server))   # old call shape

    def test_private_uri_matches_the_captured_shape(self):
        with self.fixed():
            uri = launcher.build_launch_uri("T", PLACE, "ignored-job-id", extra=EXTRA)
        want = ("https://www.roblox.com/Game/PlaceLauncher.ashx?request=RequestPrivateGame"
                f"&browserTrackerId=111111111111&placeId={PLACE}&accessCode={ACCESS}&linkCode="
                "&joinAttemptId=ATTEMPT-UUID&joinAttemptOrigin=privateServerListJoin")
        self.assertEqual(uri, self.expected(want))
        self.assertNotIn("ignored-job-id", uri)             # a job ID never mixes with a private server
        self.assertEqual(parse_place_link(uri).access_code, ACCESS)    # and our own link parses back

    def test_link_code_only_extra_and_bad_codes(self):
        with self.fixed():
            uri = launcher.build_launch_uri("T", PLACE, extra={"linkCode": LINK})
        self.assertIn(f"accessCode%3D%26linkCode%3D{LINK}%26", uri)
        for bad in ({"accessCode": "a b"}, {"accessCode": "x&y=1" * 3}, {"linkCode": "../../etc"}, {"accessCode": "ab"}):
            with self.assertRaises(ValueError, msg=bad):
                launcher.build_launch_uri("T", PLACE, extra=bad)

    def test_launch_account_private_never_saves_the_code(self):
        a = Account(id="a1", username="u1", userId=1, encryptedCookie="x")
        store = FakeStore([a])
        opened = []
        with mock.patch.object(launcher, "fetch_game_name", lambda cookie, place: "Grow a Garden"), \
                mock.patch.object(launcher, "request_auth_ticket", lambda cookie: "TICKET"), \
                mock.patch.object(launcher.os, "startfile", opened.append, create=True):
            launcher.launch_account(store, a, PLACE, "some-job-id-0000", extra=EXTRA)
        self.assertEqual(len(opened), 1)
        self.assertEqual(parse_place_link(opened[0]).access_code, ACCESS)
        self.assertEqual((a.savedPlaceId, a.savedJobId), (PLACE, None))
        self.assertNotIn(ACCESS, json.dumps(a.__dict__, default=str))

    def test_launch_account_refuses_a_malformed_code_before_asking_roblox(self):
        a = Account(id="a1", username="u1", userId=1, encryptedCookie="x")
        store = FakeStore([a])
        with mock.patch.object(launcher, "request_auth_ticket", side_effect=AssertionError("must not be called")):
            with self.assertRaises(launcher.LaunchError) as cm:
                launcher.launch_account(store, a, PLACE, extra={"accessCode": "bad code!"})
        self.assertTrue(cm.exception.fatal)


class PrivateWorld(World):
    """World whose launcher records the private-server data it was given."""

    def __init__(self):
        super().__init__()
        self.calls = []   # (place_id, job_id, extra-or-'absent')

    def launch(self, account, place_id, job_id, **kw):
        self.calls.append((place_id, job_id, kw.get("extra", "absent")))
        super().launch(account, place_id, job_id)


class PrivateRejoinTests(unittest.TestCase):
    def setUp(self):
        self.world = PrivateWorld()

    def test_public_launch_still_uses_the_three_argument_call(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, "jobAAAAAAAA")
        self.assertEqual(self.world.calls, [(111, "jobAAAAAAAA", "absent")])
        self.assertIsNone(mgr.clients[a.id].extra)
        self.assertFalse(mgr.clients[a.id].private)

    def test_private_rejoin_never_drops_link_code(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, PLACE, "ignored-job-id", extra=EXTRA)
        for _ in range(autorejoin.MAX_ATTEMPTS):          # every attempt, not just the first
            self.world.crash(a)
            tick_for(mgr, self.world, 3)
        self.assertEqual(len(self.world.calls), 1 + autorejoin.MAX_ATTEMPTS)
        for place, job, extra in self.world.calls:
            self.assertEqual((place, job, extra), (PLACE, None, EXTRA))   # never a public server
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(mgr.clients[a.id].state, "gave_up")
        self.assertEqual(len(self.world.calls), 1 + autorejoin.MAX_ATTEMPTS)

    def test_private_session_not_persisted(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, PLACE, None, extra={"accessCode": ACCESS, "linkCode": LINK, "origin": "privateServerListJoin"})
        records = mgr.session_records()
        self.assertEqual([r.get("private") for r in records], [True])
        self.assertNotIn(ACCESS, json.dumps(records))
        self.assertNotIn(LINK, json.dumps(records))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.json"
            self.assertTrue(SessionStore(path).save(records))
            text = path.read_text(encoding="utf-8")
        self.assertNotIn(ACCESS, text)
        self.assertNotIn(LINK, text)
        self.assertNotIn(ACCESS, repr(mgr.clients[a.id]))
        self.assertNotIn(ACCESS, "\n".join(mgr.status_lines()))

    def test_public_records_are_unchanged(self):
        [a] = accounts = make_accounts(1)
        mgr, _ = make_manager(self.world, accounts)
        mgr.launch(a, 111, "jobAAAAAAAA")
        self.assertNotIn("private", mgr.session_records()[0])

    def test_adopted_private_client_is_never_rejoined_into_a_public_server(self):
        [a] = accounts = make_accounts(1)
        first, _ = make_manager(self.world, accounts)
        first.launch(a, PLACE, None, extra=EXTRA)
        records = first.session_records()
        logs = []
        second, store = make_manager(self.world, accounts)       # "the app restarted": no extra any more
        counts = restore(second, store, records, True, log=logs.append)
        self.assertEqual(counts["adopted"], 1)
        self.assertTrue(second.clients[a.id].private)
        launches_before = len(self.world.calls)
        self.world.crash(a)
        tick_for(second, self.world, 3)
        self.assertEqual(len(self.world.calls), launches_before)           # nothing was launched
        self.assertEqual(second.clients[a.id].state, "gave_up")

    def test_dead_private_client_is_not_relaunched_after_a_crash(self):
        [a] = accounts = make_accounts(1)
        first, _ = make_manager(self.world, accounts)
        first.launch(a, PLACE, None, extra=EXTRA)
        records = first.session_records()
        self.world.crash(a)
        logs = []
        second, store = make_manager(self.world, accounts)
        launches_before = len(self.world.calls)
        counts = restore(second, store, records, False, log=logs.append)   # unclean exit, auto-rejoin on
        self.assertEqual((counts["recovered"], counts["dropped"]), (0, 1))
        self.assertEqual(len(self.world.calls), launches_before)
        self.assertTrue(any("private" in line for line in logs))
        self.assertFalse(any(ACCESS in line for line in logs))

    def test_manager_gaveup_event_for_a_private_client_without_a_code(self):
        [a] = accounts = make_accounts(1)
        events = []
        first, _ = make_manager(self.world, accounts)
        first.launch(a, PLACE, None, extra=EXTRA)
        second, store = make_manager(self.world, accounts, notify_fn=lambda e, acc, d: events.append((e, d)))
        restore(second, store, first.session_records(), True, log=lambda m: None)
        self.world.crash(a)
        tick_for(second, self.world, 3)
        self.assertIn("rejoin_gaveup", [e for e, _ in events])
        self.assertTrue(all(ACCESS not in d for _, d in events))


class PrivateRedactionTests(unittest.TestCase):
    def test_access_code_is_redacted_in_every_form(self):
        for text in (f"accessCode={ACCESS}", f"accessCode%3D{ACCESS}%26linkCode%3D", f'"accessCode": "{ACCESS}"'.replace('": "', ":"),
                     launch_text(PRIVATE_INNER, ticket="X" * 300), f"url ...&accessCode={ACCESS}&linkCode={LINK}&joinAttemptId=1",
                     f"?privateServerLinkCode={LINK}"):
            out = safelog.redact(text)
            self.assertNotIn(ACCESS, out, text)
            self.assertNotIn(LINK, out, text)
            self.assertNotIn("X" * 50, out)
        self.assertEqual(safelog.redact("accessCode=&linkCode=&placeId=5"), "accessCode=&linkCode=&placeId=5")

    def test_job_ids_and_normal_lines_are_left_alone(self):
        line = f"[rejoin] user1: relaunched (attempt 1/5, same private server) job {JOB}"
        self.assertEqual(safelog.redact(line), line)


class PrivateDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        private_links.last_result.clear()
        self.addCleanup(private_links.last_result.clear)

    def test_unused_session_adds_no_line_and_use_is_marked_experimental(self):
        self.assertFalse(any("Private" in r.name for r in diagnostics.check_join_tools(include_network=False)))
        private_links.record_result("armed", PLACE)
        [r] = [r for r in diagnostics.check_join_tools(include_network=False) if "Private" in r.name]
        self.assertIn("experimental", r.name)
        self.assertIn(str(PLACE), r.detail)
        self.assertNotIn(ACCESS, r.detail)


if __name__ == "__main__":
    unittest.main()