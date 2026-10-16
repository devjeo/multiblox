"""
Phase 6: diagnostics.

Checks every outside thing this tool depends on, so that when something stops
working (a Roblox update, a new PC, an expired cookie) you can see WHICH piece
broke instead of guessing:

  Python (64-bit?), administrator, data folder, saved files, DPAPI, Roblox
  install (and the Microsoft Store version, which cannot multi-instance), Roblox
  log naming, Roblox web endpoints (incl. the CSRF handshake), the multi-instance
  state + the Roblox mutex/event names actually present in running clients, and
  every saved account's cookie.

Run on its own:   python diagnostics.py [--offline]
or from the GUI:  the "Diagnostics..." button.

The report is passed through safelog.redact(), so it is safe to paste when
asking for help. Cookies are never read into the report.
"""

from __future__ import annotations

import json
import os
import struct
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import groups
from account_store import DATA_DIR
from safelog import redact

ON_WINDOWS = sys.platform == "win32"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RobloxAccountManager/0.6"

ENDPOINTS = (
    ("Roblox users API", "https://users.roblox.com/v1/users/authenticated", "GET"),
    ("Roblox presence API", "https://presence.roblox.com/v1/presence/users", "POST"),
    ("Roblox auth-ticket API", "https://auth.roblox.com/v1/authentication-ticket", "POST"),
)


@dataclass
class Result:
    level: str   # OK | WARN | FAIL | INFO | SKIP
    name: str
    detail: str


def _ok(name, detail=""): return Result("OK", name, detail)
def _warn(name, detail=""): return Result("WARN", name, detail)
def _fail(name, detail=""): return Result("FAIL", name, detail)
def _info(name, detail=""): return Result("INFO", name, detail)
def _skip(name, detail=""): return Result("SKIP", name, detail)


# --------------------------------------------------------------------------
# Individual checks (each returns a Result or a list of Results)
# --------------------------------------------------------------------------
def check_python():
    bits = struct.calcsize("P") * 8
    version = sys.version.split()[0]
    if ON_WINDOWS and bits != 64:
        return _fail("Python", f"{version} is {bits}-bit. The handle code needs 64-bit Python to read Roblox's handle table.")
    return _ok("Python", f"{version}, {bits}-bit")


def check_admin():
    if not ON_WINDOWS:
        return _skip("Administrator", "not Windows")
    import ctypes
    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return _warn("Administrator", "could not tell")
    if admin:
        return _ok("Administrator", "running elevated")
    return _warn("Administrator", "not elevated. Closing the singleton event inside Roblox clients may fail "
                                  "(run the terminal as administrator).")


def check_data_dir():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=DATA_DIR, delete=True):
            pass
        return _ok("Data folder", str(DATA_DIR))
    except OSError as exc:
        return _fail("Data folder", f"{DATA_DIR} is not writable: {exc}")


def check_data_files():
    results = []
    for name in ("accounts.json", "places.json", "settings.json", "sessions.json", "secrets.json"):
        path = DATA_DIR / name
        if not path.exists():
            results.append(_info(name, "not created yet"))
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
            results.append(_ok(name, f"valid JSON, {path.stat().st_size} bytes"))
        except (OSError, ValueError) as exc:
            results.append(_fail(name, f"unreadable or corrupt ({exc}). Delete it to start fresh "
                                      f"(for accounts.json you would need to re-add accounts)."))
    return results


def check_dpapi():
    if not ON_WINDOWS:
        return _skip("Cookie encryption (DPAPI)", "not Windows")
    from account_store import protect, unprotect
    try:
        ok = unprotect(protect(b"diagnostics")) == b"diagnostics"
        return _ok("Cookie encryption (DPAPI)", "round trip works") if ok else \
            _fail("Cookie encryption (DPAPI)", "decrypted data did not match")
    except OSError as exc:
        return _fail("Cookie encryption (DPAPI)", str(exc))


def check_roblox_install():
    if not ON_WINDOWS:
        return _skip("Roblox install", "not Windows")
    import winreg
    command = None
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"roblox-player\shell\open\command") as key:
            command = winreg.QueryValueEx(key, "")[0]
    except OSError:
        pass
    if not command:
        return _fail("Roblox install", "the roblox-player: link handler is not registered. Install Roblox "
                                       "from roblox.com (launching needs it).")
    results = []
    if "windowsapps" in command.lower():
        results.append(_fail("Roblox install", "this is the Microsoft Store version. It cannot run several "
                                               "clients; uninstall it and install Roblox from roblox.com."))
    else:
        results.append(_ok("Roblox install", command.split('"')[1] if command.startswith('"') else command))
    versions = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "Versions"
    try:
        folders = sorted(p.name for p in versions.iterdir() if (p / "RobloxPlayerBeta.exe").exists())
        results.append(_info("Roblox versions", ", ".join(folders) if folders else "no RobloxPlayerBeta.exe found"))
    except OSError:
        pass
    return results


def check_logs():
    import autorejoin
    log_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Roblox" / "logs"
    if not log_dir.is_dir():
        return _warn("Roblox logs", f"{log_dir} not found (starts existing after the first game launch). "
                                    "Auto-rejoin still works from process exit + presence.")
    try:
        logs = sorted(log_dir.glob("*Player*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as exc:
        return _warn("Roblox logs", str(exc))
    if not logs:
        return _warn("Roblox logs", "no *Player*.log files yet")
    newest = logs[0]
    if autorejoin._LOG_TS_RE.search(newest.name):
        return _ok("Roblox logs", f"newest log name is recognised ({newest.name})")
    return _warn("Roblox logs", f"newest log '{newest.name}' no longer matches the expected naming. Roblox "
                                "may have changed it; log-based detection falls back to file creation time.")


def _probe(url: str, method: str):
    """Returns (status_code, headers) for ANY HTTP response, or (None, error text)."""
    data = b"{}" if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/json",
                                          "Accept": "application/json", "Referer": "https://www.roblox.com/"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {})
    except Exception as exc:
        return None, str(exc)


def check_network():
    results = []
    for name, url, method in ENDPOINTS:
        code, extra = _probe(url, method)
        if code is None:
            results.append(_fail(name, f"unreachable: {extra}"))
        elif code in (404, 410):
            results.append(_fail(name, f"HTTP {code}: Roblox may have moved this endpoint ({url})"))
        elif code >= 500:
            results.append(_warn(name, f"HTTP {code}: Roblox seems to be having problems"))
        elif code == 429:
            results.append(_warn(name, "HTTP 429: rate limited right now"))
        else:
            note = ""
            if "authentication-ticket" in url:
                has_csrf = any(k.lower() == "x-csrf-token" for k in extra)
                note = " (CSRF handshake token offered)" if has_csrf else " (no CSRF token header seen)"
            results.append(_ok(name, f"reachable, HTTP {code} without a cookie is expected{note}"))
    return results


def check_multi_instance(holder):
    if holder is None:
        return _skip("Multi-instance", "not running inside the app")
    if not ON_WINDOWS:
        return _skip("Multi-instance", "not Windows")
    import multi_instance as mi
    results = []
    state = holder.describe()
    results.append(_ok("Multi-instance", state) if holder.active else _warn("Multi-instance", state))
    results.append(_info("Configured names", f"mutex: {', '.join(mi.MUTEX_NAMES)} | event: {', '.join(mi.EVENT_NAMES)}"))
    clients = mi.find_roblox_processes()
    results.append(_info("Roblox clients running", str(len(clients))))
    known = {n.lower() for n in mi.MUTEX_NAMES + mi.EVENT_NAMES}
    for pid, _name in clients[:6]:
        try:
            found, error = mi.discover_singleton_names(pid)
        except Exception as exc:
            results.append(_warn(f"Client PID {pid}", f"could not inspect handles: {exc!r}"))
            continue
        if error:
            results.append(_warn(f"Client PID {pid}", error))
            continue
        if not found:
            results.append(_info(f"Client PID {pid}", "no Roblox-named mutex/event handles (normal after the "
                                                       "event closer has run)"))
            continue
        unknown = [(k, n) for k, n in found if n.rsplit("\\", 1)[-1].lower() not in known]
        listing = "; ".join(f"{k} {n.rsplit(chr(92), 1)[-1]}" for k, n in found)
        if unknown:
            results.append(_warn(f"Client PID {pid}",
                                 f"Roblox-named objects not in your settings: {listing}. If multi-instance "
                                 "stopped working after a Roblox update, add these names in Settings."))
        else:
            results.append(_info(f"Client PID {pid}", listing))
    return results


def check_accounts(store, include_network):
    if store is None:
        return _skip("Accounts", "not available")
    if not store.accounts:
        return _info("Accounts", "none saved")
    results = []
    from account_store import CookieError, fetch_authenticated_user
    for a in store.accounts:
        try:
            cookie = store.get_cookie(a)
        except OSError as exc:
            results.append(_fail(f"Account {a.username}", f"cannot decrypt the cookie (different Windows user or PC?): {exc}"))
            continue
        if not include_network:
            results.append(_ok(f"Account {a.username}", "cookie decrypts (network check skipped)"))
            del cookie
            continue
        try:
            user = fetch_authenticated_user(cookie)
            if int(user["id"]) != a.userId:
                results.append(_warn(f"Account {a.username}", "cookie is valid but belongs to a different user ID"))
            else:
                results.append(_ok(f"Account {a.username}", "cookie valid"))
        except CookieError as exc:
            results.append(_fail(f"Account {a.username}", f"{exc} Re-add this account."))
        finally:
            del cookie
    return results


def check_groups(store, settings=None):
    if store is None:
        return _skip("Groups", "not available")
    results = []
    members = {n: len(groups.filter_accounts(store.accounts, n)) for n in groups.names(store.accounts)}
    ungrouped = sum(1 for a in store.accounts if not (a.group or "").strip())
    if members:
        listing = ", ".join(f"{n}: {c}" for n, c in members.items())
        if ungrouped:
            listing += f", (no group): {ungrouped}"
        results.append(_info("Groups", listing))
    else:
        results.append(_info("Groups", "no account is in a group"))
    raw = settings.get("groupGames") if settings is not None else None
    if raw:
        defaults, problems = groups.normalize_group_games(raw)
        for problem in problems:
            results.append(_warn("Group game", problem))
        for key in defaults:
            if not groups.filter_accounts(store.accounts, key):
                results.append(_warn("Group game", f"'{key}' has a saved game but no account is in that group"))
    return results


def check_notifications(settings, notifier=None):
    """Which channels are on, whether Discord has a usable webhook, and how the last send went.
    The webhook URL itself is never put in the report."""
    if settings is None:
        return _skip("Notifications", "not available")
    results = []
    discord, tray = bool(settings.get("notifyDiscord")), bool(settings.get("notifyTray"))
    events = [e for e in (settings.get("notifyEvents") or []) if isinstance(e, str)]
    if not discord and not tray:
        results.append(_warn("Notifications", "no channel is turned on, so nothing will be sent"))
    else:
        on = ", ".join(n for n, flag in (("Discord", discord), ("tray", tray)) if flag)
        results.append(_info("Notifications", f"channels: {on}; events: {', '.join(events) or 'none'}; "
                                              f"cooldown {settings.get('notifyCooldownSeconds', '?')} s"))
        if not events:
            results.append(_warn("Notification events", "no event is selected, so nothing will be sent"))
    if discord:
        import notify_discord
        import secrets_store
        try:
            url = secrets_store.get(notify_discord.WEBHOOK_SLOT)
        except Exception as exc:
            url = None
            results.append(_warn("Discord webhook", f"could not read the saved webhook ({exc!r})"))
        else:
            if not url:
                results.append(_warn("Discord webhook", "Discord is on but no webhook is saved (or it cannot be "
                                                        "decrypted on this PC). Paste one in Settings."))
            elif not notify_discord.is_discord_webhook(url):
                results.append(_warn("Discord webhook", "the saved value is not a Discord webhook URL"))
            else:
                results.append(_ok("Discord webhook", "saved and well-formed (not shown)"))
    status = getattr(notifier, "last_status", None) or {}
    for channel, ok in sorted(status.items()):
        results.append(_ok(f"Last {channel} send", "succeeded") if ok else
                       _warn(f"Last {channel} send", "failed (see the log for details)"))
    return results


def check_join_tools(include_network: bool = True):
    """games.roblox.com (the server finder's source), users.roblox.com (friend lookup) and the
    results of the last finder run and the last friend lookup."""
    import friends
    import server_finder
    results = []
    if include_network:
        # Any HTTP answer proves the host is reachable; 400/404 just mean this probe place is not listable.
        url = server_finder.SERVERS_URL.format(place_id=1818) + "?limit=10"
        code, extra = _probe(url, "GET")
        if code is None:
            results.append(_fail("Server list API", f"unreachable: {extra}"))
        elif code in (404, 410):
            results.append(_warn("Server list API", f"HTTP {code}: Roblox may have moved this endpoint ({url})"))
        elif code >= 500:
            results.append(_warn("Server list API", f"HTTP {code}: Roblox seems to be having problems"))
        elif code == 429:
            results.append(_warn("Server list API", "HTTP 429: rate limited right now (normal if you just used Find)"))
        else:
            results.append(_ok("Server list API", f"reachable, HTTP {code}"))
        # Friend lookup also needs presence.roblox.com, but that one requires a login, so only users is probed.
        code, extra = _probe(friends.USER_BY_ID_URL.format(user_id=1), "GET")
        if code is None:
            results.append(_fail("User lookup API", f"unreachable: {extra}"))
        elif code in (404, 410):
            results.append(_warn("User lookup API", f"HTTP {code}: Roblox may have moved this endpoint"))
        elif code == 429:
            results.append(_warn("User lookup API", "HTTP 429: rate limited right now (normal if you just used Look up)"))
        elif code >= 500:
            results.append(_warn("User lookup API", f"HTTP {code}: Roblox seems to be having problems"))
        else:
            results.append(_ok("User lookup API", f"reachable, HTTP {code}"))
    last = dict(server_finder.last_result)
    if not last:
        results.append(_info("Server finder", "not used yet in this session"))
    elif last.get("error"):
        results.append(_warn("Server finder", f"last run ({last['at']}, place {last['placeId']}): {last['error']}"))
    else:
        results.append(_info("Server finder", f"last run ({last['at']}, place {last['placeId']}, mode {last['mode']}): "
                                              f"{last['count']} servers, picked {last['jobId'] or 'none'}"))
    friend = dict(friends.last_result)  # shown only after a lookup, so an unused session adds no line
    if friend.get("error"):
        results.append(_warn("Friend join", f"last lookup ({friend['at']}): {friend['error']}"))
    elif friend:
        who = f"user {friend['userId']}" if friend.get("userId") else "no user"
        results.append(_info("Friend join", f"last lookup ({friend['at']}, {who}): {friend['outcome']}"))
    return results


# --------------------------------------------------------------------------
# Runner + report
# --------------------------------------------------------------------------
def run_all(store=None, holder=None, include_network: bool = True, progress=None,
            extras=None) -> list[Result]:
    """`extras`: optional dict of extra objects for newer checks, e.g. {'settings': Settings}."""
    extras = extras or {}
    steps = [
        ("Python", check_python), ("Administrator", check_admin), ("Data folder", check_data_dir),
        ("Saved files", check_data_files), ("Encryption", check_dpapi), ("Roblox install", check_roblox_install),
        ("Roblox logs", check_logs),
    ]
    if include_network:
        steps.append(("Roblox web endpoints", check_network))
    steps += [("Multi-instance", lambda: check_multi_instance(holder)),
              ("Accounts", lambda: check_accounts(store, include_network)),
              ("Groups", lambda: check_groups(store, extras.get("settings"))),
              ("Notifications", lambda: check_notifications(extras.get("settings"), extras.get("notifier"))),
              ("Join tools", lambda: check_join_tools(include_network))]
    results: list[Result] = []
    for label, fn in steps:
        if progress:
            progress(f"Checking {label}...")
        try:
            out = fn()
        except Exception as exc:  # a broken check must never hide the others
            out = _fail(label, f"the check itself crashed: {exc!r}")
        results.extend(out if isinstance(out, list) else [out])
    return results


def format_report(results: list[Result]) -> str:
    tags = {"OK": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]", "INFO": "[info]", "SKIP": "[skip]"}
    lines = [f"Roblox Account Manager diagnostics - {datetime.now().isoformat(timespec='seconds')}",
             f"Python {sys.version.split()[0]} on {sys.platform}", ""]
    for r in results:
        lines.append(f"{tags[r.level]} {r.name}" + (f": {r.detail}" if r.detail else ""))
    count = lambda level: sum(1 for r in results if r.level == level)
    lines += ["", f"Summary: {count('OK')} ok, {count('WARN')} warnings, {count('FAIL')} failures."]
    return redact("\n".join(lines))


def main() -> None:
    store = None
    try:
        from account_store import AccountStore
        store = AccountStore()
    except Exception:
        pass
    print(format_report(run_all(store=store, include_network="--offline" not in sys.argv[1:])))


if __name__ == "__main__":
    main()