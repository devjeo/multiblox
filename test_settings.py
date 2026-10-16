"""
Settings tests (the class moved out of gui.py). Run:  python -m unittest test_settings -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import autorejoin
from settings import DEFAULTS, Settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "settings.json"

    def test_defaults_when_there_is_no_file(self):
        s = Settings(self.path, apply_now=False)
        self.assertEqual(dict(s), DEFAULTS)

    def test_saved_values_load_and_roundtrip(self):
        s = Settings(self.path, apply_now=False)
        s["rejoinCooldown"] = 20
        s["groupGames"] = {"farm": {"placeId": 5, "jobId": None}}
        s.save()
        again = Settings(self.path, apply_now=False)
        self.assertEqual(again["rejoinCooldown"], 20)
        self.assertEqual(again["groupGames"], {"farm": {"placeId": 5, "jobId": None}})

    def test_wrong_type_and_unknown_keys_are_ignored(self):
        self.path.write_text(json.dumps({"rejoinCooldown": "fast", "closeToTray": False,
                                         "notAKey": 1, "groupGames": ["not", "a", "dict"]}), encoding="utf-8")
        s = Settings(self.path, apply_now=False)
        self.assertEqual(s["rejoinCooldown"], DEFAULTS["rejoinCooldown"])
        self.assertIs(s["closeToTray"], False)
        self.assertEqual(s["groupGames"], {})
        self.assertNotIn("notAKey", s)

    def test_corrupt_file_falls_back_to_defaults(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(dict(Settings(self.path, apply_now=False)), DEFAULTS)
        self.path.write_text("[1, 2, 3]", encoding="utf-8")  # valid JSON, wrong shape
        self.assertEqual(dict(Settings(self.path, apply_now=False)), DEFAULTS)

    def test_group_games_default_is_not_shared_between_instances(self):
        a = Settings(self.path, apply_now=False)
        a["groupGames"]["farm"] = {"placeId": 1, "jobId": None}
        b = Settings(self.path, apply_now=False)
        self.assertEqual(b["groupGames"], {})
        self.assertEqual(DEFAULTS["groupGames"], {})

    def test_apply_pushes_values_into_autorejoin(self):
        names = ["REJOIN_COOLDOWN_SECONDS", "MAX_ATTEMPTS", "ATTEMPT_WINDOW_SECONDS",
                 "PRESENCE_POLL_SECONDS", "SETTLE_SECONDS"]
        saved = {n: getattr(autorejoin, n) for n in names}
        self.addCleanup(lambda: [setattr(autorejoin, n, v) for n, v in saved.items()])
        s = Settings(self.path, apply_now=False)
        s["rejoinCooldown"], s["maxAttempts"], s["attemptWindowMinutes"] = 3, 7, 2
        s.apply()
        self.assertEqual((autorejoin.REJOIN_COOLDOWN_SECONDS, autorejoin.MAX_ATTEMPTS,
                          autorejoin.ATTEMPT_WINDOW_SECONDS), (3, 7, 120))


if __name__ == "__main__":
    unittest.main()