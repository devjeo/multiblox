"""
Resource saver tests (temp dirs only: no Windows, no Roblox). Run:  python -m unittest test_resourcesaver -v
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

import diagnostics
import resourcesaver as rs
from settings import DEFAULTS


class SaverCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.version = self.root / "Versions" / "version-aaa"
        self.version.mkdir(parents=True)
        (self.version / "RobloxPlayerBeta.exe").write_bytes(b"x")
        self.path = rs.settings_path(self.version)
        self.state = self.root / "data" / "resource_backup.json"
        self.logs: list[str] = []
        rs.reset_throttle()

    def apply(self, profile="Low", path=None):
        return rs.apply_profile(profile, settings_path=path or self.path, state_path=self.state,
                                log_fn=self.logs.append)

    def restore(self, path=None):
        return rs.restore(settings_path=path or self.path, state_path=self.state, log_fn=self.logs.append)

    def write_original(self, content: bytes):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(content)


class ApplyTests(SaverCase):
    def test_backup_created_once_and_not_overwritten(self):
        original = b'{"MyFlag": 1}\r\n'
        self.write_original(original)
        self.apply("Low")
        first = json.loads(self.state.read_text())
        self.assertEqual(base64.b64decode(first["originalB64"]), original)
        self.apply("Minimum")
        self.apply("Low")
        again = json.loads(self.state.read_text())
        self.assertEqual(base64.b64decode(again["originalB64"]), original)  # still the TRUE original

    def test_reapply_does_not_compound(self):
        self.write_original(b'{"MyFlag": 1}')
        self.apply("Low")
        after_first = self.path.read_bytes()
        self.assertEqual(self.apply("Low").status, "unchanged")
        self.assertEqual(self.path.read_bytes(), after_first)
        self.apply("Minimum")
        self.apply("Low")
        self.assertEqual(self.path.read_bytes(), after_first)  # switching back gives the same file

    def test_profile_merges_over_user_keys(self):
        self.write_original(json.dumps({"Keep": "me", "DFIntTaskSchedulerTargetFps": 999}).encode())
        self.apply("Low")
        data = json.loads(self.path.read_text())
        self.assertEqual(data["Keep"], "me")
        self.assertEqual(data["DFIntTaskSchedulerTargetFps"], rs.PROFILES["Low"]["DFIntTaskSchedulerTargetFps"])

    def test_restore_returns_original_bytes(self):
        original = b'{\r\n  "A": 1\r\n}'  # odd formatting on purpose: bytes must come back exactly
        self.write_original(original)
        self.apply("Low")
        self.assertNotEqual(self.path.read_bytes(), original)
        self.assertTrue(self.restore())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(self.state.exists())

    def test_restore_deletes_file_that_did_not_exist(self):
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.apply("Low").status, "applied")
        self.assertTrue(self.path.exists())
        self.assertTrue(self.restore())
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())  # the folder we created is gone too
        self.assertTrue((self.version / "RobloxPlayerBeta.exe").exists())  # nothing else touched

    def test_existing_clientsettings_folder_is_kept(self):
        self.path.parent.mkdir()
        (self.path.parent / "other.json").write_text("{}")
        self.apply("Low")
        self.restore()
        self.assertFalse(self.path.exists())
        self.assertTrue((self.path.parent / "other.json").exists())

    def test_malformed_left_untouched_and_warns(self):
        self.write_original(b"{ not json")
        result = self.apply("Low")
        self.assertEqual(result.status, "warning")
        self.assertEqual(self.path.read_bytes(), b"{ not json")
        self.assertFalse(self.state.exists())

    def test_non_object_left_untouched(self):
        self.write_original(b"[1, 2, 3]")
        self.assertEqual(self.apply("Low").status, "warning")
        self.assertEqual(self.path.read_bytes(), b"[1, 2, 3]")

    def test_external_edit_becomes_new_original(self):
        self.write_original(b'{"A": 1}')
        self.apply("Low")
        self.path.write_text('{"A": 2, "B": 3}')  # someone else edits it
        self.apply("Low")
        data = json.loads(self.path.read_text())
        self.assertEqual((data["A"], data["B"]), (2, 3))
        self.assertTrue(any("edited by something else" in line for line in self.logs))
        self.restore()
        self.assertEqual(json.loads(self.path.read_text()), {"A": 2, "B": 3})

    def test_restore_does_not_overwrite_an_external_edit(self):
        self.write_original(b'{"A": 1}')
        self.apply("Low")
        self.path.write_text('{"A": 9}')
        self.restore()
        self.assertEqual(json.loads(self.path.read_text()), {"A": 9})

    def test_normal_profile_equals_restore(self):
        original = b'{"A": 1}'
        self.write_original(original)
        self.apply("Low")
        self.assertEqual(self.apply("Normal").status, "restored")
        self.assertEqual(self.path.read_bytes(), original)

    def test_restore_without_backup_is_false(self):
        self.assertFalse(self.restore())

    def test_unknown_profile_is_an_error(self):
        self.assertEqual(self.apply("Turbo").status, "error")

    def test_minimum_is_superset_of_low(self):
        self.assertLessEqual(set(rs.PROFILES["Low"]), set(rs.PROFILES["Minimum"]))
        self.assertEqual(rs.PROFILES["Normal"], {})

    def test_new_version_folder_cleans_up_the_old_one(self):
        self.write_original(b'{"A": 1}')
        self.apply("Low")
        newer = self.root / "Versions" / "version-bbb"
        newer.mkdir()
        (newer / "RobloxPlayerBeta.exe").write_bytes(b"x")
        new_path = rs.settings_path(newer)
        self.apply("Low", path=new_path)
        self.assertEqual(self.path.read_bytes(), b'{"A": 1}')  # old folder put back
        self.assertEqual(json.loads(new_path.read_text())["DFIntTaskSchedulerTargetFps"], 20)
        self.assertEqual(json.loads(self.state.read_text())["path"], str(new_path))

    def test_crash_between_state_and_file_never_loses_original(self):
        # the state is written BEFORE the file, so a failed file write still leaves a valid backup
        self.write_original(b'{"A": 1}')
        real = rs._write_atomic
        rs._write_atomic = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            self.assertEqual(self.apply("Low").status, "error")
        finally:
            rs._write_atomic = real
        self.assertEqual(self.path.read_bytes(), b'{"A": 1}')
        self.assertTrue(self.restore())
        self.assertEqual(self.path.read_bytes(), b'{"A": 1}')


class LocateTests(SaverCase):
    def test_current_version_dir_picks_newest_with_player(self):
        old = self.root / "Versions" / "version-old"
        old.mkdir()
        (old / "RobloxPlayerBeta.exe").write_bytes(b"x")
        os.utime(old, (1000, 1000))
        empty = self.root / "Versions" / "version-empty"  # newest by time, but no player exe
        empty.mkdir()
        os.utime(self.version, (2000, 2000))
        os.utime(empty, (3000, 3000))
        self.assertEqual(rs.current_version_dir([self.root / "Versions"]), self.version)

    def test_current_version_dir_none_when_missing(self):
        self.assertIsNone(rs.current_version_dir([self.root / "nowhere"]))


class EnsureTests(SaverCase):
    def ensure(self, profile="Low", clock=None, **kw):
        return rs.ensure_applied(profile, settings_path=self.path, state_path=self.state,
                                 log_fn=self.logs.append, clock=clock or (lambda: 0.0), **kw)

    def test_throttled_and_idempotent(self):
        self.assertEqual(self.ensure().status, "applied")
        self.path.unlink()  # if it ran again, the file would come back
        self.assertEqual(self.ensure().status, "applied")  # throttled: cached result, no work
        self.assertFalse(self.path.exists())

    def test_runs_again_after_the_interval(self):
        times = iter([0.0, 100.0])
        self.assertEqual(self.ensure(clock=lambda: next(times)).status, "applied")
        self.assertEqual(self.ensure(clock=lambda: next(times)).status, "unchanged")

    def test_switching_profile_is_not_throttled(self):
        self.ensure("Low")
        self.ensure("Minimum")
        self.assertEqual(json.loads(self.path.read_text())["DFIntTaskSchedulerTargetFps"], 10)

    def test_never_raises(self):
        result = rs.ensure_applied("Low", settings_path=object(), state_path=self.state)
        self.assertEqual(result.status, "error")

    def test_skips_when_roblox_not_found(self):
        self.assertEqual(rs.ensure_applied("Low", roots=[self.root / "nowhere"]).status, "skipped")


class StatusAndDiagnosticsTests(SaverCase):
    def status(self):
        return rs.status(version_dir=self.version, state_path=self.state)

    def test_status_reports_active_profile(self):
        self.assertIsNone(self.status()["matches"])
        self.apply("Low")
        self.assertEqual(self.status()["matches"], "Low")
        self.assertIn("Low", rs.describe(self.status()))

    def test_diagnostics_flags_malformed_file(self):
        self.write_original(b"{ nope")
        out = diagnostics.check_resourcesaver({"performanceProfile": "Low", "applyProfileBeforeLaunch": True},
                                              version_dir=self.version, state_path=self.state)
        self.assertTrue(any(r.level == "WARN" and "not a valid JSON" in r.detail for r in out))

    def test_diagnostics_warns_when_profile_not_applied_yet(self):
        out = diagnostics.check_resourcesaver({"performanceProfile": "Low", "applyProfileBeforeLaunch": True},
                                              version_dir=self.version, state_path=self.state)
        self.assertTrue(any(r.level == "WARN" and "does not match" in r.detail for r in out))

    def test_diagnostics_ok_when_applied(self):
        self.apply("Low")
        out = diagnostics.check_resourcesaver({"performanceProfile": "Low", "applyProfileBeforeLaunch": True},
                                              version_dir=self.version, state_path=self.state)
        self.assertFalse([r for r in out if r.level in ("WARN", "FAIL")])

    def test_diagnostics_warns_about_stale_backup_after_update(self):
        self.apply("Low")
        newer = self.root / "Versions" / "version-bbb"
        newer.mkdir()
        out = diagnostics.check_resourcesaver({"performanceProfile": "Low", "applyProfileBeforeLaunch": False},
                                              version_dir=newer, state_path=self.state)
        self.assertTrue(any("older Roblox version" in r.detail for r in out))

    def test_settings_defaults_exist(self):
        self.assertEqual(DEFAULTS["performanceProfile"], "Normal")
        self.assertIs(DEFAULTS["applyProfileBeforeLaunch"], False)


if __name__ == "__main__":
    unittest.main()