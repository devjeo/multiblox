"""
Shared-infrastructure tests (Phase 2): jsonio, secrets_store, net, workers, safelog.
Run:  python -m unittest test_shared -v

No Windows, Roblox or network needed: secrets_store gets a fake cipher and net
gets a scripted fake for urlopen.
"""

from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import jsonio
import net
import secrets_store
from safelog import redact
from workers import Worker

WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/AbC-dEf_ghIJKlmnopqrstuvwxyz0123456789"


def fake_cipher():
    """Patches secrets_store's cipher seams with a reversible fake (not secure; tests only)."""
    return (mock.patch.object(secrets_store, "_protect", lambda b: b[::-1] + b"!"),
            mock.patch.object(secrets_store, "_unprotect", lambda b: b[:-1][::-1]))


class JsonioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "sub" / "x.json"

    def test_roundtrip_creates_folders_and_leaves_no_tmp_file(self):
        jsonio.write_json_atomic(self.path, {"a": [1, 2]})
        self.assertEqual(jsonio.read_json(self.path, None), {"a": [1, 2]})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["x.json"])

    def test_missing_and_corrupt_files_give_the_default(self):
        self.assertEqual(jsonio.read_json(self.path, "dflt"), "dflt")
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{nope", encoding="utf-8")
        self.assertEqual(jsonio.read_json(self.path, "dflt"), "dflt")


class SecretsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "secrets.json"
        for p in fake_cipher():
            p.start()
            self.addCleanup(p.stop)

    def test_set_get_list_delete(self):
        self.assertIsNone(secrets_store.get("a", path=self.path))
        secrets_store.set("a", "one", path=self.path)
        secrets_store.set("b", "two", path=self.path)
        self.assertEqual(secrets_store.get("a", path=self.path), "one")
        self.assertEqual(secrets_store.list_names(path=self.path), ["a", "b"])
        self.assertTrue(secrets_store.has("b", path=self.path))
        secrets_store.delete("a", path=self.path)
        self.assertIsNone(secrets_store.get("a", path=self.path))
        self.assertEqual(secrets_store.get("b", path=self.path), "two")

    def test_value_is_not_stored_in_plain_text(self):
        secrets_store.set("notify.webhookUrl", WEBHOOK, path=self.path)
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("discord.com", text)
        self.assertNotIn(WEBHOOK, text)
        self.assertEqual(json.loads(text)["version"], 1)

    def test_undecryptable_value_returns_none_instead_of_raising(self):
        secrets_store.set("a", "one", path=self.path)
        with mock.patch.object(secrets_store, "_unprotect", side_effect=OSError("wrong user")):
            self.assertIsNone(secrets_store.get("a", path=self.path))

    def test_corrupt_file_reads_as_empty(self):
        self.path.write_text("{oops", encoding="utf-8")
        self.assertIsNone(secrets_store.get("a", path=self.path))
        self.assertEqual(secrets_store.list_names(path=self.path), [])
        secrets_store.set("a", "x", path=self.path)  # and can be written again
        self.assertEqual(secrets_store.get("a", path=self.path), "x")

    def test_wrong_shape_entries_are_ignored(self):
        self.path.write_text(json.dumps({"version": 1, "secrets": {"ok": "AAAA", "bad": 5}}), encoding="utf-8")
        self.assertEqual(secrets_store.list_names(path=self.path), ["ok"])


# --------------------------------------------------------------------------
# net
# --------------------------------------------------------------------------
class FakeResp:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, headers=None, body=b""):
    return urllib.error.HTTPError("https://example.test/x", code, "msg", headers or {}, io.BytesIO(body))


class NetTests(unittest.TestCase):
    def script(self, *outcomes):
        """Makes net._open return/raise each outcome in turn; records the requests."""
        self.requests, self.sleeps = [], []
        queue = list(outcomes)

        def fake_open(req, timeout):
            self.requests.append(req)
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        for p in (mock.patch.object(net, "_open", fake_open),
                  mock.patch.object(net, "_sleep", lambda s: self.sleeps.append(s))):
            p.start()
            self.addCleanup(p.stop)

    def test_success_parses_json_and_empty_body_is_none(self):
        self.script(FakeResp(200, b'{"a": 1}'), FakeResp(204, b""))
        self.assertEqual(net.request("GET", "https://x.test/").json(), {"a": 1})
        self.assertIsNone(net.request("GET", "https://x.test/").json())

    def test_5xx_is_retried_with_backoff_then_succeeds(self):
        self.script(http_error(503), http_error(500), FakeResp(200))
        self.assertEqual(net.request("GET", "https://x.test/").status, 200)
        self.assertEqual(self.sleeps, [0.5, 1.0])
        self.assertEqual(len(self.requests), 3)

    def test_429_honours_retry_after_but_caps_it(self):
        self.script(http_error(429, {"Retry-After": "3"}), http_error(429, {"retry-after": "999"}), FakeResp(200))
        net.request("GET", "https://x.test/")
        self.assertEqual(self.sleeps, [3.0, net.MAX_RETRY_AFTER])

    def test_other_4xx_is_not_retried(self):
        self.script(http_error(404, body=b"nope"))
        with self.assertRaises(net.HttpError) as cm:
            net.request("GET", "https://x.test/")
        self.assertEqual((cm.exception.status, cm.exception.body, len(self.requests)), (404, b"nope", 1))
        self.assertEqual(self.sleeps, [])

    def test_retries_run_out_then_raise(self):
        self.script(http_error(500), http_error(500), http_error(500))
        with self.assertRaises(net.HttpError) as cm:
            net.request("GET", "https://x.test/", retries=2)
        self.assertEqual((cm.exception.status, len(self.requests)), (500, 3))

    def test_network_errors_are_retried_and_end_as_status_zero(self):
        self.script(urllib.error.URLError("dns down"), TimeoutError("slow"), urllib.error.URLError("dns down"))
        with self.assertRaises(net.HttpError) as cm:
            net.request("GET", "https://x.test/")
        self.assertEqual(cm.exception.status, 0)
        self.assertEqual(len(self.requests), 3)

    def test_post_with_cookie_does_the_csrf_handshake_once(self):
        self.script(http_error(403, {"x-csrf-token": "TOK"}), FakeResp(200))
        net.request("POST", "https://x.test/", cookie="c")
        self.assertIsNone(self.requests[0].get_header("X-csrf-token"))
        self.assertEqual(self.requests[1].get_header("X-csrf-token"), "TOK")
        self.assertEqual(self.requests[1].get_header("Cookie"), ".ROBLOSECURITY=c")
        self.assertEqual(self.sleeps, [])  # the handshake is not a "retry"

    def test_csrf_handshake_is_only_for_posts_with_a_cookie(self):
        self.script(http_error(403, {"x-csrf-token": "TOK"}))
        with self.assertRaises(net.HttpError):
            net.request("GET", "https://x.test/", cookie="c")
        self.assertEqual(len(self.requests), 1)
        self.script(http_error(403, {"x-csrf-token": "TOK"}))
        with self.assertRaises(net.HttpError):
            net.request("POST", "https://x.test/")
        self.assertEqual(len(self.requests), 1)

    def test_second_403_after_handshake_raises(self):
        self.script(http_error(403, {"x-csrf-token": "A"}), http_error(403, {"x-csrf-token": "B"}))
        with self.assertRaises(net.HttpError):
            net.request("POST", "https://x.test/", cookie="c")
        self.assertEqual(len(self.requests), 2)

    def test_json_body_is_sent_as_json(self):
        self.script(FakeResp(200))
        net.request("POST", "https://x.test/", json_body={"k": "v"})
        self.assertEqual(json.loads(self.requests[0].data), {"k": "v"})
        self.assertEqual(self.requests[0].get_header("Content-type"), "application/json")

    def test_error_text_never_contains_url_body_or_cookie(self):
        secret_url = "https://x.test/" + "S3CRET" * 5
        self.script(http_error(400, body=b"S3CRET-body"))
        with self.assertRaises(net.HttpError) as cm:
            net.request("POST", secret_url, cookie="S3CRET-cookie")
        self.assertNotIn("S3CRET", str(cm.exception))
        self.assertNotIn("S3CRET", repr(cm.exception))
        self.script(urllib.error.URLError("dns down"))
        with self.assertRaises(net.HttpError) as cm:
            net.request("GET", secret_url, retries=0)
        self.assertNotIn("S3CRET", str(cm.exception))


class WorkerTests(unittest.TestCase):
    def test_items_run_in_order_on_another_thread(self):
        seen, threads = [], set()
        w = Worker("t", lambda item: (seen.append(item), threads.add(threading.current_thread().name)))
        w.start()
        self.addCleanup(w.stop)
        for i in range(5):
            w.submit(i)
        self.assertTrue(w.wait_idle(5))
        self.assertEqual(seen, [0, 1, 2, 3, 4])
        self.assertEqual(threads, {"t"})

    def test_an_exception_is_logged_and_the_loop_continues(self):
        logs, seen = [], []

        def handle(item):
            if item == "boom":
                raise RuntimeError("bad item")
            seen.append(item)
        w = Worker("t", handle, logs.append)
        w.start()
        self.addCleanup(w.stop)
        for item in ("a", "boom", "b"):
            w.submit(item)
        self.assertTrue(w.wait_idle(5))
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(len(logs), 1)

    def test_stop_handles_queued_items_then_refuses_new_ones(self):
        seen = []
        w = Worker("t", seen.append)
        w.start()
        w.submit(1)
        w.stop()
        self.assertEqual(seen, [1])
        self.assertFalse(w.submit(2))

    def test_submit_before_start_waits_for_start(self):
        seen = []
        w = Worker("t", seen.append)
        w.submit("early")
        self.assertFalse(w.wait_idle(0.05))
        w.start()
        self.addCleanup(w.stop)
        self.assertTrue(w.wait_idle(5))
        self.assertEqual(seen, ["early"])


class SafelogTests(unittest.TestCase):
    def test_discord_webhook_variants_are_redacted(self):
        for url in (WEBHOOK,
                    "https://ptb.discord.com/api/webhooks/1/abc_DEF-ghi",
                    "https://canary.discordapp.com/api/v10/webhooks/42/tok-en_1",
                    "http://DISCORDAPP.com/api/webhooks/9/xyz"):
            out = redact(f"POST {url} failed")
            self.assertEqual(out, "POST <redacted> failed", url)

    def test_private_server_codes_are_redacted_plain_and_url_encoded(self):
        self.assertEqual(redact("x?privateServerLinkCode=12345678901234567890&a=1"),
                         "x?privateServerLinkCode=<redacted>&a=1")
        self.assertEqual(redact("placelauncherurl:...linkCode%3DABC123xyz%26b"),
                         "placelauncherurl:...linkCode%3D<redacted>%26b")

    def test_ordinary_lines_with_ids_are_untouched(self):
        line = "[notify] sent 'disconnect' for user1 via discord, tray; job 6f1c2f4e-9a3b-4c1d-8e7f-0123456789ab"
        self.assertEqual(redact(line), line)


if __name__ == "__main__":
    unittest.main()