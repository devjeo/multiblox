"""
Per-account history tests. Run:  python -m unittest test_account_history -v
No Windows, Roblox or network needed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import account_history as ah

JOB1, JOB2, JOB3 = "aaaaaaaa-1111", "bbbbbbbb-2222", "cccccccc-3333"


class AccountHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "account_history.json"
        self.t = 100.0
        self.h = ah.AccountHistory(self.path, clock=self.tick)

    def tick(self):
        self.t += 1
        return self.t

    def test_clear_keeps_the_visit_to_the_server_the_account_is_in_now(self):
        self.h.add(1, 111, JOB1, "A")
        self.h.add(1, 111, JOB2, "A")
        self.h.add(2, 111, JOB3, "A")
        self.h.clear(1, keep={(111, JOB2)})
        self.assertEqual([v.job_id for v in self.h.list(1)], [JOB2])
        self.assertEqual([v.job_id for v in ah.AccountHistory(self.path).list(1)], [JOB2])   # also on disk
        self.assertEqual(len(self.h.list(2)), 1)                                               # others untouched
        self.h.clear(1)
        self.assertEqual(self.h.list(1), [])

    def test_each_account_has_its_own_list_newest_first_with_times(self):
        self.h.add(1, 111, JOB1, "Game A")
        self.h.add(2, 111, JOB1, "Game A")
        self.h.add(1, 222, JOB2, "Game B")
        self.assertEqual([v.job_id for v in self.h.list(1)], [JOB2, JOB1])
        self.assertEqual([v.job_id for v in self.h.list(2)], [JOB1])
        self.assertTrue(all(v.at > 0 for v in self.h.list(1)))

    def test_same_server_reported_twice_in_a_row_is_one_visit_but_a_return_is_new(self):
        self.assertTrue(self.h.add(1, 111, JOB1))
        self.assertFalse(self.h.add(1, 111, JOB1))
        self.h.add(1, 111, JOB2)
        self.assertTrue(self.h.add(1, 111, JOB1))
        self.assertEqual(len(self.h.list(1)), 3)

    def test_survives_a_restart(self):
        self.h.add(1, 111, JOB1, "Game A")
        again = ah.AccountHistory(self.path)
        self.assertEqual(again.latest(1).game, "Game A")

    def test_capped_per_account(self):
        for i in range(ah.MAX_VISITS_PER_ACCOUNT + 10):
            self.h.add(1, 111, f"job-{i:08d}")
        self.assertEqual(len(self.h.list(1)), ah.MAX_VISITS_PER_ACCOUNT)

    def test_bad_input_and_damaged_file_never_crash(self):
        self.assertFalse(self.h.add(0, 111, JOB1))
        self.assertFalse(self.h.add(1, 0, JOB1))
        self.assertFalse(self.h.add(1, 111, "short"))
        self.path.write_text('{"1": [1, {"placeId": "x"}, {"placeId": 5, "jobId": "dddddddd-4444", "at": 3}], "zz": []}')
        self.h.load()
        self.assertEqual([v.job_id for v in self.h.list(1)], ["dddddddd-4444"])
        self.path.write_text("not json")
        self.h.load()
        self.assertEqual(self.h.list(1), [])

    def test_clear_one_or_all(self):
        self.h.add(1, 111, JOB1)
        self.h.add(2, 111, JOB1)
        self.h.clear(1)
        self.assertEqual(self.h.list(1), [])
        self.assertEqual(len(self.h.list(2)), 1)
        self.h.clear()
        self.assertEqual(self.h.list(2), [])


if __name__ == "__main__":
    unittest.main()
