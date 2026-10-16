"""
Feature B tests (notifications). Run:  python -m unittest test_notifier -v

No Windows, Roblox or network: fake channels, a fake clock, the World fake
machine from test_phase6, and a fake cipher for secrets_store.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import diagnostics
import net
import notify_discord
import secrets_store
from launcher import LaunchError
from notifier import Notifier
from notify_discord import DiscordChannel, format_text, is_discord_webhook
from notify_tray import TrayChannel
from settings import DEFAULTS, Settings
from test_phase6 import World, make_accounts, make_manager, tick_for
from test_shared import WEBHOOK, fake_cipher

import autorejoin


class FakeChannel:
    def __init__(self, name="fake", ok=True, boom=None):
        self.name, self.ok, self.boom, self.sent = name, ok, boom, []

    def send(self, n):
        if self.boom:
            raise self.boom
        self.sent.append(n)
        return self.ok


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.logs = Clock(), []
        self.ch = FakeChannel()
        self.n = Notifier(self.clock, {"fake": self.ch}, ["disconnect", "rejoin_gaveup"], 120, self.logs.append)
        self.n.start()
        self.addCleanup(self.n.stop)

    def send(self, event="disconnect", account="user1", detail="d", **kw):
        self.n.notify(event, account, detail, **kw)
        self.assertTrue(self.n.wait_idle(5))

    def test_same_event_same_account_within_cooldown_sends_once(self):
        self.send()
        self.clock.t += 119
        self.send()
        self.assertEqual(len(self.ch.sent), 1)

    def test_different_accounts_send_twice(self):
        self.send(account="user1")
        self.send(account="user2")
        self.assertEqual([x.account for x in self.ch.sent], ["user1", "user2"])

    def test_different_events_for_one_account_send_twice(self):
        self.send(event="disconnect")
        self.send(event="rejoin_gaveup")
        self.assertEqual(len(self.ch.sent), 2)

    def test_after_cooldown_sends_again(self):
        self.send()
        self.clock.t += 120
        self.send()
        self.assertEqual(len(self.ch.sent), 2)

    def test_disabled_event_is_dropped(self):
        self.send(event="launch")
        self.assertEqual(self.ch.sent, [])

    def test_force_bypasses_filter_and_cooldown(self):
        self.send(event="test", force=True)
        self.send(event="test", force=True)
        self.send(event="disconnect")
        self.send(event="disconnect", force=True)
        self.assertEqual(len(self.ch.sent), 4)

    def test_notify_does_not_block_on_a_slow_channel(self):
        import threading
        gate = threading.Event()
        slow = FakeChannel("slow")
        slow.send = lambda n: gate.wait(5) or True
        self.n.reconfigure({"slow": slow}, ["disconnect"], 0)
        self.n.notify("disconnect", "a", "")      # returns at once although send() is stuck
        self.n.notify("disconnect", "b", "")
        self.assertFalse(self.n.wait_idle(0.05))
        gate.set()
        self.assertTrue(self.n.wait_idle(5))

    def test_failing_channel_does_not_stop_others_or_worker(self):
        bad, good = FakeChannel("bad", boom=RuntimeError("down")), FakeChannel("good")
        self.n.reconfigure({"bad": bad, "good": good}, ["disconnect"], 0)
        self.send(account="user1")
        self.send(account="user2")                 # the worker is still alive
        self.assertEqual(len(good.sent), 2)
        self.assertEqual(self.n.last_status, {"bad": False, "good": True})
        self.assertEqual(sum("bad: could not send" in m for m in self.logs), 1, "reported once, not per event")

    def test_channel_returning_false_is_reported(self):
        self.n.reconfigure({"x": FakeChannel("x", ok=False)}, ["disconnect"], 0)
        self.send()
        self.assertEqual(self.n.last_status, {"x": False})

    def test_reconfigure_changes_events_channels_and_cooldown(self):
        other = FakeChannel("other")
        self.n.reconfigure({"other": other}, ["launch"], 0)
        self.send(event="launch")
        self.send(event="launch")
        self.send(event="disconnect")
        self.assertEqual((len(other.sent), self.ch.sent), (2, []))

    def test_no_channels_is_harmless(self):
        self.n.reconfigure({}, ["disconnect"], 0)
        self.send()
        self.assertEqual(self.n.last_status, {})

    def test_webhook_never_in_log_output(self):
        class Leaky:
            def send(self, n):
                raise RuntimeError(f"POST {WEBHOOK} failed")
        self.n.reconfigure({"leaky": Leaky()}, ["disconnect"], 0)
        self.send(detail=f"saw {WEBHOOK}")
        self.assertTrue(self.logs)
        self.assertNotIn("AbC-dEf_", "\n".join(self.logs))
        self.assertNotIn("webhooks/123", "\n".join(self.logs))


class DiscordChannelTests(unittest.TestCase):
    def note(self, event="disconnect", account="user1", detail="client process exited"):
        from notifier import Notification
        from datetime import datetime
        return Notification(event, account, detail, datetime(2026, 10, 16))

    def test_posts_text_and_disables_mentions(self):
        calls = []
        ch = DiscordChannel(webhook_fn=lambda: WEBHOOK, request_fn=lambda *a, **k: calls.append((a, k)))
        self.assertTrue(ch.send(self.note(detail="@everyone it broke")))
        (method, url), kw = calls[0]
        self.assertEqual((method, url), ("POST", WEBHOOK))
        self.assertEqual(kw["json_body"]["allowed_mentions"], {"parse": []})
        self.assertIn("**[disconnect]** user1: @everyone it broke", kw["json_body"]["content"])

    def test_refuses_non_discord_urls_without_any_request(self):
        calls = []
        for url in ("https://evil.example.com/api/webhooks/1/abc", "http://discord.com/api/webhooks/1/abc",
                    "https://discord.com.evil.io/api/webhooks/1/abc", "https://discord.com/api/webhooks/x/abc",
                    "", None, 5):
            ch = DiscordChannel(webhook_fn=lambda u=url: u, request_fn=lambda *a, **k: calls.append(a))
            self.assertFalse(ch.send(self.note()), url)
        self.assertEqual(calls, [])

    def test_missing_webhook_returns_false(self):
        self.assertFalse(DiscordChannel(webhook_fn=lambda: None, request_fn=lambda *a, **k: 1 / 0).send(self.note()))

    def test_http_failure_returns_false_and_never_raises(self):
        def boom(*a, **k):
            raise net.HttpError(404, b"", WEBHOOK)
        self.assertFalse(DiscordChannel(webhook_fn=lambda: WEBHOOK, request_fn=boom).send(self.note()))

    def test_text_is_capped(self):
        self.assertLessEqual(len(format_text(self.note(detail="x" * 5000))), notify_discord.MAX_TEXT)

    def test_url_validation(self):
        for good in (WEBHOOK, WEBHOOK + "/", "https://ptb.discord.com/api/webhooks/1/a-b_c",
                     "https://discordapp.com/api/v10/webhooks/1/a"):
            self.assertTrue(is_discord_webhook(good), good)
        self.assertFalse(is_discord_webhook(WEBHOOK + "?thread_id=1"))

    def test_reads_the_webhook_from_secrets_store_at_send_time(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "s.json"
        for p in fake_cipher():
            p.start()
            self.addCleanup(p.stop)
        calls = []
        ch = DiscordChannel(webhook_fn=lambda: secrets_store.get(notify_discord.WEBHOOK_SLOT, path=path),
                            request_fn=lambda *a, **k: calls.append(a))
        self.assertFalse(ch.send(self.note()))             # nothing saved yet
        secrets_store.set(notify_discord.WEBHOOK_SLOT, WEBHOOK, path=path)
        self.assertTrue(ch.send(self.note()))              # picked up without recreating the channel
        self.assertEqual(calls[0][1], WEBHOOK)


class TrayChannelTests(unittest.TestCase):
    def test_notifies_through_the_icon_in_pystray_argument_order(self):
        class Icon:
            HAS_NOTIFICATION = True
            def __init__(self): self.calls = []
            def notify(self, message, title=None): self.calls.append((message, title))
        icon = Icon()
        from notifier import Notification
        from datetime import datetime
        n = Notification("disconnect", "user1", "client process exited", datetime(2026, 10, 16))
        self.assertTrue(TrayChannel(lambda: icon).send(n))
        message, title = icon.calls[0]
        self.assertEqual(message, "user1: client process exited")
        self.assertIn("disconnect", title)

    def test_no_icon_or_a_broken_icon_returns_false(self):
        from notifier import Notification
        from datetime import datetime
        n = Notification("disconnect", "u", "d", datetime(2026, 10, 16))
        self.assertFalse(TrayChannel(lambda: None).send(n))
        broken = mock.Mock()
        broken.notify.side_effect = RuntimeError("no shell")
        self.assertFalse(TrayChannel(lambda: broken).send(n))


# --------------------------------------------------------------------------
# RejoinManager hooks (use the World fake machine)
# --------------------------------------------------------------------------
class ManagerEventTests(unittest.TestCase):
    def setUp(self):
        self.world, self.events, self.ready = World(), [], []

    def manager(self, accounts, **kw):
        kw.setdefault("notify_fn", lambda e, a, d: self.events.append((e, a, d)))
        kw.setdefault("on_ready_fn", self.ready.append)
        return make_manager(self.world, accounts, **kw)[0]

    def names(self, event):
        return [e for e in self.events if e[0] == event]

    def test_manager_emits_disconnect(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        [(event, account, detail)] = self.names("disconnect")
        self.assertEqual(account, "user1")
        self.assertIn("exited", detail)

    def test_disconnect_with_auto_rejoin_off_still_notifies_and_says_so(self):
        [a] = accounts = make_accounts(1, rejoin=False)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertIn("auto-rejoin is off", self.names("disconnect")[0][2])

    def test_manager_emits_gaveup(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        for _ in range(autorejoin.MAX_ATTEMPTS + 1):
            self.world.crash(a)
            tick_for(mgr, self.world, 3)
        self.assertEqual(mgr.clients[a.id].state, "gave_up")
        self.assertEqual(len(self.names("rejoin_gaveup")), 1)

    def test_fatal_launch_error_emits_gaveup_but_not_cookie_invalid(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.world.fail_next.append(LaunchError("cannot decrypt", fatal=True, reason="cannot_decrypt"))
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(len(self.names("rejoin_gaveup")), 1)
        self.assertEqual(self.names("cookie_invalid"), [])

    def test_cookie_rejected_emits_cookie_invalid(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.world.fail_next.append(LaunchError("rejected", fatal=True, reason="cookie_rejected"))
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(len(self.names("cookie_invalid")), 1)
        self.assertEqual(len(self.names("rejoin_gaveup")), 1)

    def test_transient_launch_error_emits_neither(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.world.fail_next.append(LaunchError("rate limited", reason="rate_limited"))
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(self.names("rejoin_gaveup") + self.names("cookie_invalid"), [])

    def test_launch_event_and_on_ready_fire_for_manual_and_rejoin_launches(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.assertEqual((len(self.names("launch")), len(self.ready)), (1, 1))
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual((len(self.names("launch")), len(self.ready)), (2, 2))
        self.assertIs(self.ready[-1], mgr.clients[a.id])
        self.assertEqual(self.ready[-1].pid, mgr.clients[a.id].pid)

    def test_manager_swallows_notify_exception(self):
        def bad(event, account, detail):
            raise RuntimeError("notifier exploded")
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts, notify_fn=bad, on_ready_fn=bad)
        mgr.launch(a, 111, None)
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(len(self.world.launches), 2, "rejoin must still happen")
        self.assertEqual(mgr.clients[a.id].state, "running")

    def test_intentional_stop_emits_nothing(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        self.events.clear()
        mgr.stop_account(a)
        tick_for(mgr, self.world, 5)
        self.assertEqual(self.events, [])

    def test_stop_racing_a_tick_that_already_saw_the_client_emits_nothing(self):
        [a] = accounts = make_accounts(1)
        mgr = self.manager(accounts)
        mgr.launch(a, 111, None)
        client = mgr.clients[a.id]
        self.events.clear()
        mgr.stop_account(a)                      # pops + cancels the client and kills it
        mgr._on_disconnect(client, "client process exited")   # a tick holding a stale reference
        self.assertEqual(self.events, [])

    def test_without_callbacks_nothing_changes(self):
        [a] = accounts = make_accounts(1)
        mgr = make_manager(self.world, accounts)[0]
        mgr.launch(a, 111, None)
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertEqual(len(self.world.launches), 2)

    def test_end_to_end_with_a_real_notifier(self):
        ch, logs = FakeChannel(), []
        notifier = Notifier(self.world.clock, {"fake": ch}, ["disconnect"], 120, logs.append)
        notifier.start()
        self.addCleanup(notifier.stop)
        [a] = accounts = make_accounts(1)
        mgr = make_manager(self.world, accounts, notify_fn=notifier.notify)[0]
        mgr.launch(a, 111, None)
        self.world.crash(a)
        tick_for(mgr, self.world, 3)
        self.assertTrue(notifier.wait_idle(5))
        self.assertEqual([(n.event, n.account) for n in ch.sent], [("disconnect", "user1")])


class LaunchErrorTests(unittest.TestCase):
    def test_reason_defaults_to_other_and_old_calls_still_work(self):
        self.assertEqual(LaunchError("x").reason, "other")
        self.assertEqual(LaunchError("x", True).reason, "other")
        self.assertTrue(LaunchError("x", fatal=True).fatal)

    def test_request_auth_ticket_sets_reasons(self):
        import urllib.error
        from launcher import request_auth_ticket
        for code, reason, fatal in ((401, "cookie_rejected", True), (429, "rate_limited", False),
                                    (500, "other", False)):
            def fake(req, timeout=0, code=code):
                raise urllib.error.HTTPError(req.full_url, code, "x", {}, None)
            with mock.patch("launcher.urllib.request.urlopen", fake):
                with self.assertRaises(LaunchError) as cm:
                    request_auth_ticket("c")
            self.assertEqual((cm.exception.reason, cm.exception.fatal), (reason, fatal), code)

    def test_launch_account_decrypt_failure_has_a_reason(self):
        from launcher import launch_account

        class Store:
            def get_cookie(self, account):
                raise OSError("wrong user")
        with self.assertRaises(LaunchError) as cm:
            launch_account(Store(), make_accounts(1)[0], 111)
        self.assertEqual((cm.exception.reason, cm.exception.fatal), ("cannot_decrypt", True))


# --------------------------------------------------------------------------
# Settings + diagnostics + the cross-cutting secret check
# --------------------------------------------------------------------------
class SettingsKeysTests(unittest.TestCase):
    def test_notification_defaults(self):
        self.assertEqual(DEFAULTS["notifyEvents"], ["disconnect", "rejoin_gaveup", "cookie_invalid"])
        self.assertEqual((DEFAULTS["notifyDiscord"], DEFAULTS["notifyTray"], DEFAULTS["notifyCooldownSeconds"]),
                         (False, True, 120))
        self.assertNotIn("webhook", " ".join(DEFAULTS).lower(), "the webhook must not live in settings.json")

    def test_events_list_roundtrips_and_is_not_shared(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "settings.json"
        s = Settings(path, apply_now=False)
        s["notifyEvents"].append("launch")
        s["notifyDiscord"] = True
        s.save()
        again = Settings(path, apply_now=False)
        self.assertEqual(again["notifyEvents"][-1], "launch")
        self.assertTrue(again["notifyDiscord"])
        self.assertEqual(DEFAULTS["notifyEvents"], ["disconnect", "rejoin_gaveup", "cookie_invalid"])


class NotificationDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "secrets.json"
        for p in fake_cipher():
            p.start()
            self.addCleanup(p.stop)
        patch = mock.patch.object(secrets_store, "SECRETS_FILE", self.path)
        patch.start()
        self.addCleanup(patch.stop)

    def settings(self, **kw):
        return {**DEFAULTS, **kw}

    def by_name(self, results):
        return {r.name: r for r in results}

    def test_without_settings_it_is_skipped(self):
        self.assertEqual(diagnostics.check_notifications(None).level, "SKIP")

    def test_no_channel_enabled_warns(self):
        r = diagnostics.check_notifications(self.settings(notifyTray=False, notifyDiscord=False))
        self.assertEqual(r[0].level, "WARN")

    def test_discord_on_without_webhook_warns(self):
        r = self.by_name(diagnostics.check_notifications(self.settings(notifyDiscord=True)))
        self.assertEqual(r["Discord webhook"].level, "WARN")

    def test_discord_on_with_webhook_is_ok_and_does_not_show_it(self):
        secrets_store.set(notify_discord.WEBHOOK_SLOT, WEBHOOK)
        results = diagnostics.check_notifications(self.settings(notifyDiscord=True))
        r = self.by_name(results)
        self.assertEqual(r["Discord webhook"].level, "OK")
        report = diagnostics.format_report(results)
        self.assertNotIn("AbC-dEf_", report)
        self.assertNotIn("webhooks/123", report)

    def test_saved_value_that_is_not_a_webhook_warns(self):
        secrets_store.set(notify_discord.WEBHOOK_SLOT, "https://example.com/hook")
        r = self.by_name(diagnostics.check_notifications(self.settings(notifyDiscord=True)))
        self.assertEqual(r["Discord webhook"].level, "WARN")

    def test_last_send_status_is_reported_per_channel(self):
        n = Notifier(Clock(), {}, [], 0, lambda m: None)
        n.last_status.update({"discord": False, "tray": True})
        r = self.by_name(diagnostics.check_notifications(self.settings(), n))
        self.assertEqual((r["Last discord send"].level, r["Last tray send"].level), ("WARN", "OK"))

    def test_run_all_includes_notifications_and_secrets_file(self):
        results = diagnostics.run_all(store=None, holder=None, include_network=False,
                                      extras={"settings": self.settings(), "notifier": None})
        self.assertTrue(any(r.name == "Notifications" for r in results))
        self.assertTrue(any(r.name == "secrets.json" for r in results))
        self.assertTrue(all("crashed" not in r.detail for r in results))


class NoSecretLeaksTests(unittest.TestCase):
    """Cross-cutting: nothing stored in secrets_store may appear in any log or report."""

    def test_stored_secrets_never_appear_in_logs_or_reports(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "secrets.json"
        for p in fake_cipher():
            p.start()
            self.addCleanup(p.stop)
        patches = [mock.patch.object(secrets_store, "SECRETS_FILE", path)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        secrets_store.set(notify_discord.WEBHOOK_SLOT, WEBHOOK)
        stored = [secrets_store.get(n) for n in secrets_store.list_names()]
        self.assertTrue(stored)

        logs = []

        def failing_request(method, url, **kw):
            raise net.HttpError(500, b"", url)           # carries the URL as an attribute, like net does
        notifier = Notifier(Clock(), {"discord": DiscordChannel(request_fn=failing_request)},
                            ["disconnect"], 0, logs.append)
        notifier.start()
        self.addCleanup(notifier.stop)
        for i in range(3):
            notifier.notify("disconnect", f"user{i}", f"detail {WEBHOOK}")
        notifier.wait_idle(5)
        results = diagnostics.check_notifications({**DEFAULTS, "notifyDiscord": True}, notifier)
        output = "\n".join(logs) + "\n" + diagnostics.format_report(results)
        for secret in stored:
            self.assertNotIn(secret, output)
            self.assertNotIn(secret.rsplit("/", 1)[-1], output)   # not even the token part


if __name__ == "__main__":
    unittest.main()