"""
Feature A tests (account groups and notes). Run:  python -m unittest test_groups -v

No Windows, Roblox or network needed.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import diagnostics
import groups
from account_store import Account, AccountStore, NOTE_MAX_LENGTH
from launcher import parse_selection


def acct(n, group="", place=None):
    return Account(id=f"id{n}", username=f"user{n}", userId=100 + n, encryptedCookie="x",
                   group=group, savedPlaceId=place)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "accounts.json"

    def write_raw(self, accounts):
        self.path.write_text(json.dumps({"version": 1, "accounts": accounts}), encoding="utf-8")

    def test_old_accounts_file_loads_with_blank_group_and_note(self):
        self.write_raw([{"id": "a", "username": "old", "userId": 1, "encryptedCookie": "x",
                         "autoRejoinEnabled": True, "createdAt": "2026-01-01T00:00:00+00:00"}])
        store = AccountStore(self.path)
        self.assertEqual((store.accounts[0].group, store.accounts[0].note), ("", ""))
        self.assertTrue(store.accounts[0].autoRejoinEnabled)

    def test_unknown_keys_are_ignored(self):
        self.write_raw([{"id": "a", "username": "u", "userId": 1, "encryptedCookie": "x",
                         "someFutureField": 5, "group": "farm"}])
        store = AccountStore(self.path)
        self.assertEqual(store.accounts[0].group, "farm")

    def test_set_group_and_note_roundtrip(self):
        store = AccountStore(self.path)
        store.accounts.append(acct(1))
        store.set_group(store.accounts[0], "  farm   one ")
        store.set_note(store.accounts[0], "  main alt  ")
        again = AccountStore(self.path)
        self.assertEqual(again.accounts[0].group, "farm one")  # whitespace tidied
        self.assertEqual(again.accounts[0].note, "main alt")

    def test_empty_value_clears(self):
        store = AccountStore(self.path)
        store.accounts.append(acct(1, group="farm"))
        store.set_group(store.accounts[0], "")
        self.assertEqual(store.accounts[0].group, "")

    def test_note_is_capped(self):
        store = AccountStore(self.path)
        store.accounts.append(acct(1))
        store.set_note(store.accounts[0], "x" * 1000)
        self.assertEqual(len(store.accounts[0].note), NOTE_MAX_LENGTH)

    def test_reserved_group_name_is_refused(self):
        store = AccountStore(self.path)
        store.accounts.append(acct(1))
        with self.assertRaises(ValueError):
            store.set_group(store.accounts[0], "All Groups")
        self.assertEqual(store.accounts[0].group, "")

    def test_cookie_refresh_keeps_group_and_note(self):
        store = AccountStore(self.path)
        existing = acct(1, group="farm")
        existing.note = "keep me"
        store.accounts.append(existing)
        with mock.patch("account_store.fetch_authenticated_user", return_value={"id": 101, "name": "user1"}), \
                mock.patch("account_store.protect", return_value=b"blob"):
            account, updated = store.add_from_cookie("fake-cookie")
        self.assertTrue(updated)
        self.assertEqual((account.group, account.note), ("farm", "keep me"))


class GroupHelperTests(unittest.TestCase):
    def setUp(self):
        self.accounts = [acct(1, "farm"), acct(2, "Farm"), acct(3, "trade"), acct(4, "")]

    def test_names_are_unique_case_insensitive_and_sorted(self):
        self.assertEqual(groups.names(self.accounts), ["farm", "trade"])

    def test_filter_all_and_none_return_everything(self):
        self.assertEqual(len(groups.filter_accounts(self.accounts, groups.ALL)), 4)
        self.assertEqual(len(groups.filter_accounts(self.accounts, None)), 4)
        self.assertEqual(len(groups.filter_accounts(self.accounts, "")), 4)

    def test_filter_is_case_insensitive(self):
        self.assertEqual([a.id for a in groups.filter_accounts(self.accounts, "FARM")], ["id1", "id2"])

    def test_filter_unknown_group_returns_empty(self):
        self.assertEqual(groups.filter_accounts(self.accounts, "nope"), [])

    def test_resolve_target_prefers_ui_then_group_default_then_saved(self):
        games = {"Trade": {"placeId": 777, "jobId": "job-12345678"}}
        members = [acct(5, "trade", place=555)]
        # 1. the UI choice wins
        self.assertEqual(groups.resolve_target("trade", 111, "ui-job-1234", games, members), (111, "ui-job-1234"))
        # 2. then the group's saved default (name match ignores case)
        self.assertEqual(groups.resolve_target("trade", None, None, games, members), (777, "job-12345678"))
        # 3. then what a member last played
        self.assertEqual(groups.resolve_target("trade", None, None, {}, members), (555, None))
        # 4. otherwise nothing
        self.assertIsNone(groups.resolve_target("trade", None, None, {}, [acct(6, "trade")]))

    def test_bad_group_games_entries_are_skipped_and_reported(self):
        clean, problems = groups.normalize_group_games({
            "ok": {"placeId": 5, "jobId": "short"},
            "bad1": {"placeId": "x"},
            "bad2": "nonsense",
            "": {"placeId": 5},
            "bad3": {"placeId": True},
        })
        self.assertEqual(clean, {"ok": (5, None)})  # a too-short job ID is dropped
        self.assertEqual(len(problems), 4)
        self.assertEqual(groups.normalize_group_games("not a dict"), ({}, []))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AccountStore(Path(self.tmp.name) / "accounts.json")
        self.store.accounts = [acct(1, "farm"), acct(2, "farm"), acct(3, "trade")]

    def ids(self, raw):
        accounts, problems = parse_selection(self.store, raw)
        return [a.id for a in accounts], problems

    def test_existing_syntax_still_works(self):
        self.assertEqual(self.ids("all"), (["id1", "id2", "id3"], []))
        self.assertEqual(self.ids("1,user3"), (["id1", "id3"], []))
        self.assertEqual(self.ids("USER2"), (["id2"], []))

    def test_unknown_account_is_reported_not_raised(self):
        ids, problems = self.ids("1,ghost")
        self.assertEqual(ids, ["id1"])
        self.assertEqual(len(problems), 1)

    def test_group_syntax(self):
        self.assertEqual(self.ids("group:farm"), (["id1", "id2"], []))
        self.assertEqual(self.ids("group: FARM"), (["id1", "id2"], []))

    def test_group_can_be_mixed_with_accounts_without_duplicates(self):
        self.assertEqual(self.ids("group:farm,1,3"), (["id1", "id2", "id3"], []))

    def test_unknown_or_empty_group_is_reported(self):
        self.assertEqual(self.ids("group:nope")[1], ["No accounts in group 'nope'."])
        self.assertEqual(len(self.ids("group:")[1]), 1)


class DiagnosticsTests(unittest.TestCase):
    def test_check_groups_reports_counts_and_orphan_defaults(self):
        class Store:
            accounts = [acct(1, "farm"), acct(2, "farm"), acct(3, "")]
        settings = {"groupGames": {"farm": {"placeId": 1}, "ghosts": {"placeId": 2}, "broken": {}}}
        results = diagnostics.check_groups(Store, settings)
        by_level = {}
        for r in results:
            by_level.setdefault(r.level, []).append(r)
        self.assertIn("farm: 2", by_level["INFO"][0].detail)
        self.assertIn("(no group): 1", by_level["INFO"][0].detail)
        warns = " | ".join(r.detail for r in by_level["WARN"])
        self.assertIn("ghosts", warns)
        self.assertIn("broken", warns)
        self.assertNotIn("'farm' has a saved game but no", warns)

    def test_check_groups_without_a_store_is_skipped(self):
        self.assertEqual(diagnostics.check_groups(None).level, "SKIP")

    def test_run_all_includes_groups_and_accepts_extras(self):
        results = diagnostics.run_all(store=None, holder=None, include_network=False,
                                      extras={"settings": {}})
        self.assertTrue(any(r.name == "Groups" for r in results))


if __name__ == "__main__":
    unittest.main()