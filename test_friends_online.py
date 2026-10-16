"""Online-friends list (friends.online_friends): fake network, no Roblox."""

import json
import sys
import unittest

import friends
import net


def ok(payload):
    return net.Response(200, {}, json.dumps(payload).encode())


def presence(uid, state, place=None, job=None, loc=""):
    return {"userId": uid, "userPresenceType": state, "placeId": place, "rootPlaceId": place,
            "gameId": job, "lastLocation": loc}


class FakeRoblox:
    def __init__(self, friend_list, presences):
        self.friend_list, self.presences, self.calls = friend_list, presences, []

    def __call__(self, method, url, *, json_body=None, cookie=None, **kw):
        self.calls.append((method, url, json_body, cookie))
        if url == friends.FRIENDS_URL.format(user_id=5):
            return ok({"data": self.friend_list})
        if url == friends.PRESENCE_URL:
            return ok({"userPresences": [p for p in self.presences if p["userId"] in json_body["userIds"]]})
        raise AssertionError(url)


class OnlineFriendsTests(unittest.TestCase):
    def setUp(self):
        self.friend_list = [
            {"id": 1, "name": "Offline_Guy", "displayName": "Offline_Guy"},
            {"id": 2, "name": "Lobby_Girl", "displayName": "Lobby"},
            {"id": 3, "name": "Hidden_Kid", "displayName": "Hidden_Kid"},
            {"id": 4, "name": "Joinable_Pal", "displayName": "Joinable_Pal"},
            {"id": 6, "name": "Studio_Dev", "displayName": "Studio_Dev"},
        ]
        self.presences = [
            presence(1, 0),
            presence(2, 1),
            presence(3, 2, place=None, job=None, loc="Some Game"),   # server hidden
            presence(4, 2, place=111, job="job-abcdefgh", loc="Chop A Forest"),
            presence(6, 3),
        ]

    def test_lists_only_online_friends_joinable_first(self):
        fake = FakeRoblox(self.friend_list, self.presences)
        entries = friends.online_friends("COOKIE", 5, fetch=fake)
        self.assertEqual([e.user.name for e in entries], ["Joinable_Pal", "Hidden_Kid", "Lobby_Girl", "Studio_Dev"])
        self.assertEqual([e.joinable for e in entries], [True, False, False, False])
        self.assertEqual(entries[0].status, "In a game - joinable")
        self.assertEqual(entries[1].status, "In a game - server hidden")
        self.assertEqual(entries[2].title, "Lobby (@Lobby_Girl)")
        self.assertEqual(friends.resolve_join_target(entries[0].presence), (111, "job-abcdefgh"))

    def test_cookie_only_goes_to_presence_not_the_friends_list(self):
        fake = FakeRoblox(self.friend_list, self.presences)
        friends.online_friends("COOKIE", 5, fetch=fake)
        by_url = {url: cookie for _m, url, _b, cookie in fake.calls}
        self.assertIsNone(by_url[friends.FRIENDS_URL.format(user_id=5)])
        self.assertEqual(by_url[friends.PRESENCE_URL], "COOKIE")

    def test_presence_requests_are_batched(self):
        many = [{"id": i, "name": f"user{i:03d}", "displayName": ""} for i in range(10, 10 + 120)]
        fake = FakeRoblox(many, [presence(u["id"], 1) for u in many])
        entries = friends.online_friends("C", 5, fetch=fake)
        self.assertEqual(len(entries), 120)
        sizes = [len(b["userIds"]) for _m, url, b, _c in fake.calls if url == friends.PRESENCE_URL]
        self.assertEqual(sizes, [50, 50, 20])

    def test_no_friends_makes_no_presence_call(self):
        fake = FakeRoblox([], [])
        self.assertEqual(friends.online_friends("C", 5, fetch=fake), [])
        self.assertEqual(len(fake.calls), 1)

    def test_bad_data_is_skipped_and_bad_user_id_is_explained(self):
        fake = FakeRoblox([{"id": "x"}, None, {"id": 2, "name": "Ok_Name"}], [presence(2, 1)])
        self.assertEqual([e.user.id for e in friends.online_friends("C", 5, fetch=fake)], [2])
        with self.assertRaises(ValueError) as cm:
            friends.online_friends("C", 0, fetch=fake)
        self.assertIn("user ID", friends.describe_error(cm.exception))

    def test_http_errors_propagate_for_the_dialog_to_describe(self):
        def boom(*a, **k):
            raise net.HttpError(429)
        with self.assertRaises(net.HttpError):
            friends.online_friends("C", 5, fetch=boom)


if __name__ == "__main__":
    unittest.main()