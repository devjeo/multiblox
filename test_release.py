"""Release hygiene: version, changelog, and that logs never depend on the script's folder."""

import re
import unittest
from pathlib import Path

import autorejoin
from account_store import DATA_DIR
from version import __version__

HERE = Path(__file__).parent


class ReleaseTests(unittest.TestCase):
    def test_version_is_semver(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_changelog_top_entry_matches_version(self):
        text = (HERE / "CHANGELOG.md").read_text(encoding="utf-8")
        first = re.search(r"^## (\d+\.\d+\.\d+)", text, re.M).group(1)
        self.assertEqual(first, __version__)

    def test_logs_live_under_data_dir(self):
        # multi_instance exits at import off Windows, so check the source of both modules
        self.assertEqual(autorejoin.DEBUG_LOG.parent, DATA_DIR)
        for name in ("multi_instance.py", "autorejoin.py"):
            self.assertNotIn("__file__", (HERE / name).read_text(encoding="utf-8"), name)
        self.assertIn('LOG_PATH = DATA_DIR /', (HERE / "multi_instance.py").read_text(encoding="utf-8"))

    def test_report_header_shows_version(self):
        import diagnostics
        self.assertIn(__version__, diagnostics.format_report([]).splitlines()[0])

    def test_spec_bundles_on_demand_modules(self):
        spec = (HERE / "RobloxAccountManager.spec").read_text(encoding="utf-8")
        for name in ("pystray", "PIL", "local_modules"):
            self.assertIn(name, spec)
        for mod in ("schedule_window", "join_dialog"):
            self.assertTrue((HERE / f"{mod}.py").exists())

    def test_installer_version_matches(self):
        iss = (HERE / "installer.iss").read_text(encoding="utf-8")
        found = re.search(r'#define MyAppVersion "([^"]+)"', iss).group(1)
        self.assertEqual(found, __version__)


if __name__ == "__main__":
    unittest.main()