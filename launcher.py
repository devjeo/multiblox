"""
Phase 3: launch saved Roblox accounts into a game (Windows only).

For each launch it:
  1. decrypts the account's cookie (from account_store.py),
  2. asks Roblox for a short-lived, single-use authentication ticket
     (handling the 403 "CSRF token" challenge),
  3. opens a roblox-player: link containing that ticket, which starts a Roblox
     client already logged in as that account.

It also runs the Phase 1 multi-instance logic (mutex + event handle closing),
so DO NOT run multi_instance.py at the same time. Keep account_store.py and
multi_instance.py in the same folder as this file.

Run (as administrator, for the event-handle step):
    python launcher.py

Commands:
    list              show saved accounts
    addplace          save a game (paste its roblox.com link or place ID)
    places            show saved games
    removeplace       delete a saved game
    launch            launch ONE account into a place (optionally a specific server)
    joinall           launch several/all accounts into the same place, one after another
    autorejoin        turn auto-rejoin on/off for accounts (rejoins after a disconnect)
    stop              close an account's client on purpose (no auto-rejoin)
    resume            retry an account that auto-rejoin gave up on
    setgroup          put an account in a group (then use 'group:<name>' wherever it asks which accounts)
    setnote           set an account's note
    status            show running Roblox clients and auto-rejoin state
    quit              exit (releases multi-instance)
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import groups
import private_links
from account_store import Account, AccountStore
from autorejoin import RejoinManager
from places import PlaceStore, fetch_place_name, parse_place_input

AUTH_TICKET_URL = "https://auth.roblox.com/v1/authentication-ticket"
PLACE_DETAILS_URL = "https://games.roblox.com/v1/games/multiget-place-details?placeIds={}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RobloxAccountManager/0.3"

class LaunchError(Exception):
    """`fatal` means retrying won't help (e.g. the cookie was rejected).
    `reason` says why, for code that reacts differently: 'cookie_rejected',
    'cannot_decrypt', 'rate_limited' or 'other'."""

    def __init__(self, message: str, fatal: bool = False, reason: str = "other"):
        super().__init__(message)
        self.fatal = fatal
        self.reason = reason


# --------------------------------------------------------------------------
# Roblox web calls
# --------------------------------------------------------------------------
def request_auth_ticket(cookie: str) -> str:
    """Returns a one-time authentication ticket for the account that owns `cookie`."""
    headers = {
        "Cookie": f".ROBLOSECURITY={cookie}",
        "Content-Type": "application/json",
        "Referer": "https://www.roblox.com/",
        "User-Agent": USER_AGENT,
    }
    csrf_token = None
    for _attempt in range(2):  # first try usually gets 403 + a CSRF token; second succeeds
        if csrf_token:
            headers["X-CSRF-TOKEN"] = csrf_token
        req = urllib.request.Request(AUTH_TICKET_URL, data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                ticket = resp.headers.get("rbx-authentication-ticket")
                if not ticket:
                    raise LaunchError("Roblox did not return an authentication ticket.")
                return ticket
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and exc.headers.get("x-csrf-token") and not csrf_token:
                csrf_token = exc.headers["x-csrf-token"]
                continue
            if exc.code in (401, 403):
                raise LaunchError(
                    "Roblox rejected this account's cookie (expired or logged out). "
                    "Re-add the account in account_store.py.",
                    fatal=True,
                    reason="cookie_rejected",
                ) from None
            if exc.code == 429:
                raise LaunchError("Roblox is rate-limiting requests. Wait a minute and try again.",
                                  reason="rate_limited") from None
            raise LaunchError(f"Roblox returned HTTP {exc.code} for the auth ticket.") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LaunchError(f"Could not reach Roblox: {exc}") from None
    raise LaunchError("Could not complete the CSRF handshake with Roblox.")


def fetch_game_name(cookie: str, place_id: int) -> str | None:
    """Best-effort lookup of the game's name (for display). Never raises."""
    req = urllib.request.Request(
        PLACE_DETAILS_URL.format(place_id),
        headers={"Cookie": f".ROBLOSECURITY={cookie}", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list) and data:
            return data[0].get("name")
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# Launch link
# --------------------------------------------------------------------------
def build_launch_uri(ticket: str, place_id: int, job_id: str | None = None, *,
                     extra: dict | None = None) -> str:
    """Builds the roblox-player: link. With job_id, joins that specific server. With `extra`
    holding a private-server code (see private_links), joins that private server instead and
    job_id is ignored. Raises ValueError for a malformed private-server code."""
    tracker = random.randint(10**11, 10**12 - 1)
    attempt = str(uuid.uuid4())
    if private_links.is_private(extra):
        access, link, origin = private_links.validate(extra)
        place_url = (
            "https://www.roblox.com/Game/PlaceLauncher.ashx?request=RequestPrivateGame"
            f"&browserTrackerId={tracker}&placeId={place_id}"
            f"&accessCode={urllib.parse.quote(access, safe='')}&linkCode={urllib.parse.quote(link, safe='')}"
            f"&joinAttemptId={attempt}&joinAttemptOrigin={origin}"
        )
    elif job_id:
        place_url = (
            "https://assetgame.roblox.com/game/PlaceLauncher.ashx?request=RequestGameJob"
            f"&browserTrackerId={tracker}&placeId={place_id}&gameId={job_id}"
            "&isPlayTogether=false"
        )
    else:
        place_url = (
            "https://assetgame.roblox.com/game/PlaceLauncher.ashx?request=RequestGame"
            f"&browserTrackerId={tracker}&placeId={place_id}&isPlayTogether=false"
            f"&joinAttemptId={attempt}&joinAttemptOrigin=PlayButton"
        )
    launch_time = int(time.time() * 1000)
    return (
        "roblox-player:1+launchmode:play"
        f"+gameinfo:{ticket}"
        f"+launchtime:{launch_time}"
        f"+placelauncherurl:{urllib.parse.quote(place_url, safe='')}"
        f"+browsertrackerid:{tracker}"
        "+robloxLocale:en_us+gameLocale:en_us+channel:+LaunchExp:InApp"
    )


def launch_account(store: AccountStore, account: Account, place_id: int, job_id: str | None = None,
                   *, extra: dict | None = None) -> None:
    """Launches one account. Raises LaunchError on failure. `extra` (private-server code) is used
    for this launch only; it is never saved."""
    private = private_links.is_private(extra)
    if private:
        job_id = None
        try:
            private_links.validate(extra)  # refuse a malformed code before any ticket is requested
        except ValueError as exc:
            raise LaunchError(str(exc), fatal=True) from None
    try:
        cookie = store.get_cookie(account)
    except OSError as exc:
        raise LaunchError(
            f"Could not decrypt the cookie (different Windows user or PC?): {exc}", fatal=True,
            reason="cannot_decrypt",
        ) from None
    try:
        game_name = fetch_game_name(cookie, place_id)
        ticket = request_auth_ticket(cookie)  # single-use and short-lived: launch right away
    finally:
        del cookie

    uri = build_launch_uri(ticket, place_id, job_id, extra=extra if private else None)
    del ticket
    try:
        os.startfile(uri)  # hands the link to Roblox's registered roblox-player handler
    except OSError as exc:
        raise LaunchError(
            f"Could not open the roblox-player link (is Roblox installed from roblox.com?): {exc}"
        ) from None

    account.savedPlaceId = place_id
    account.savedJobId = job_id
    if game_name:
        account.lastGameName = game_name
    store.save()


# --------------------------------------------------------------------------
# Console UI
# --------------------------------------------------------------------------
def cmd_list(store: AccountStore) -> None:
    if not store.accounts:
        print("No accounts saved. Run account_store.py and use 'add'.")
        return
    for i, a in enumerate(store.accounts, 1):
        last = f" | last: {a.lastGameName or a.savedPlaceId}" if (a.lastGameName or a.savedPlaceId) else ""
        grp = f" [{a.group}]" if a.group else ""
        print(f"  {i}. {a.username}{grp}{last}")


def cmd_places(places: PlaceStore) -> None:
    if not places.places:
        print("No places saved yet. Use 'addplace'.")
        return
    for i, p in enumerate(places.places, 1):
        print(f"  {i}. {p.name} (place {p.placeId})")


def cmd_addplace(places: PlaceStore) -> None:
    raw = input("Game link or place ID: ").strip()
    place_id, slug_name = parse_place_input(raw)
    if place_id is None:
        print("[!] Couldn't find a place ID. Paste the game's roblox.com link or just its number.")
        return
    name = fetch_place_name(place_id) or slug_name
    if name:
        print(f"Found: {name}")
        custom = input("Press Enter to keep this name, or type your own: ").strip()
        name = custom or name
    else:
        name = input("Couldn't look up the name. Name for this place: ").strip() or f"Place {place_id}"
    place, updated = places.add(place_id, name)
    print(f"[ok] {'Updated' if updated else 'Saved'}: {place.name} (place {place.placeId})")


def cmd_removeplace(places: PlaceStore) -> None:
    if not places.places:
        print("No places saved yet.")
        return
    cmd_places(places)
    place = places.find(input("Remove which place (number or name)? "))
    if not place:
        print("[!] No such place.")
        return
    if input(f"Delete {place.name}? [y/N] ").strip().lower() in ("y", "yes"):
        places.remove(place)
        print("[ok] Removed.")


def ask_place(default: int | None, places: PlaceStore) -> int | None:
    """Pick a saved place by number/name, or type a place ID / game link."""
    if places.places:
        cmd_places(places)
    hint = f" [Enter = {default}]" if default else ""
    raw = input(f"Place (saved number/name, ID, or link){hint}: ").strip()
    if not raw:
        if default:
            return default
        print("[!] No place chosen.")
        return None
    saved = places.find(raw)
    if saved:
        return saved.placeId
    place_id, _ = parse_place_input(raw)
    if place_id is not None:
        return place_id
    print("[!] Not a saved place, a place ID, or a roblox.com game link.")
    return None


def ask_job() -> str | None:
    raw = input("Server (job) ID - leave empty for any server: ").strip()
    if raw and len(raw) < 8:
        print("[!] That doesn't look like a job ID; ignoring it.")
        return None
    return raw or None


def cmd_launch(store: AccountStore, places: PlaceStore, manager: RejoinManager) -> None:
    if not store.accounts:
        print("No accounts saved.")
        return
    cmd_list(store)
    account = store.find(input("Which account (number or username)? "))
    if not account:
        print("[!] No such account.")
        return
    place_id = ask_place(account.savedPlaceId, places)
    if place_id is None:
        return
    job_id = ask_job()
    print(f"Launching {account.username} into place {place_id}...")
    try:
        manager.launch(account, place_id, job_id)
        print(f"[ok] {account.username} is running. "
              f"Auto-rejoin is {'ON' if account.autoRejoinEnabled else 'off'} for this account.")
    except LaunchError as exc:
        print(f"[!] {account.username}: {exc}")


def _running_pids() -> set[int]:
    from multi_instance import find_roblox_processes
    return {pid for pid, _ in find_roblox_processes()}


def parse_selection(store: AccountStore, raw: str) -> tuple[list[Account], list[str]]:
    """Turns '1,3', 'alice,bob', 'all' or 'group:farm' (mix them with commas) into
    (accounts, problems). Pure: it prints nothing, so it can be tested."""
    raw = raw.strip()
    if raw.lower() in ("all", "a"):
        return list(store.accounts), []
    chosen: list[Account] = []
    problems: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if part.lower().startswith("group:"):
            name = part[6:].strip()
            members = groups.filter_accounts(store.accounts, name) if name else []
            if not members:
                problems.append(f"No accounts in group '{name}'.")
            for acc in members:
                if acc not in chosen:
                    chosen.append(acc)
            continue
        acc = store.find(part)
        if acc and acc not in chosen:
            chosen.append(acc)
        elif not acc:
            problems.append(f"Skipping unknown account '{part}'.")
    return chosen, problems


def _select_accounts(store: AccountStore, prompt: str) -> list[Account]:
    chosen, problems = parse_selection(store, input(prompt))
    for problem in problems:
        print(f"[!] {problem}")
    return chosen


def cmd_joinall(store: AccountStore, places: PlaceStore, manager: RejoinManager) -> None:
    if not store.accounts:
        print("No accounts saved.")
        return
    cmd_list(store)
    chosen = _select_accounts(store, "Which accounts? (e.g. 1,3 - 'all' - or group:farm): ")
    if not chosen:
        return
    place_id = ask_place(chosen[0].savedPlaceId, places)
    if place_id is None:
        return
    job_id = ask_job()

    for n, account in enumerate(chosen, 1):
        print(f"({n}/{len(chosen)}) Launching {account.username}...")
        try:
            manager.launch(account, place_id, job_id)  # waits for the client + a short settle pause
            print(f"[ok] {account.username} is running.")
        except LaunchError as exc:
            print(f"[!] {account.username}: {exc}")


def cmd_autorejoin(store: AccountStore, manager: RejoinManager) -> None:
    if not store.accounts:
        print("No accounts saved.")
        return
    for i, a in enumerate(store.accounts, 1):
        print(f"  {i}. {a.username} - auto-rejoin {'ON' if a.autoRejoinEnabled else 'off'}")
    chosen = _select_accounts(store, "Which accounts? (e.g. 1,3 - 'all' - or group:farm): ")
    if not chosen:
        return
    mode = input("Turn auto-rejoin on or off? ").strip().lower()
    if mode not in ("on", "off"):
        print("[!] Type 'on' or 'off'.")
        return
    for a in chosen:
        manager.set_enabled(a, mode == "on")
    print(f"[ok] Auto-rejoin {mode} for: {', '.join(a.username for a in chosen)}")


def _pick_one(store: AccountStore, prompt: str) -> Account | None:
    if not store.accounts:
        print("No accounts saved.")
        return None
    cmd_list(store)
    account = store.find(input(prompt))
    if not account:
        print("[!] No such account.")
    return account


def cmd_stop(store: AccountStore, manager: RejoinManager) -> None:
    account = _pick_one(store, "Stop which account (number or username)? ")
    if account:
        print("[ok] Closed." if manager.stop_account(account) else "That account has no running client.")


def cmd_resume(store: AccountStore, manager: RejoinManager) -> None:
    account = _pick_one(store, "Resume which account (number or username)? ")
    if account:
        print("[ok] Trying again." if manager.resume(account) else "Nothing to resume for that account.")


def cmd_setgroup(store: AccountStore) -> None:
    account = _pick_one(store, "Which account (number or username)? ")
    if not account:
        return
    current = f" (now: {account.group})" if account.group else ""
    value = input(f"Group name for {account.username}{current} - empty clears it: ")
    try:
        store.set_group(account, value)
    except ValueError as exc:
        print(f"[!] {exc}")
        return
    print(f"[ok] {account.username}: group {account.group!r}." if account.group
          else f"[ok] {account.username}: group cleared.")


def cmd_setnote(store: AccountStore) -> None:
    account = _pick_one(store, "Which account (number or username)? ")
    if not account:
        return
    store.set_note(account, input(f"Note for {account.username} - empty clears it: "))
    print("[ok] Note saved." if account.note else "[ok] Note cleared.")


def cmd_status(manager: RejoinManager) -> None:
    from multi_instance import find_roblox_processes
    procs = find_roblox_processes()
    print(f"Roblox game clients running: {len(procs)}")
    for pid, name in procs:
        print(f"  {name} PID {pid}")
    lines = manager.status_lines()
    if lines:
        print("Tracked accounts:")
        for line in lines:
            print(line)


def kill_pid(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def main() -> None:
    if sys.platform != "win32":
        sys.exit("This tool only works on Windows.")
    print(__doc__)

    # Phase 1: multi-instance (imported here so this file can be tested off-Windows)
    from multi_instance import ProcessWatcher, SingletonHolder, make_event_closer

    store = AccountStore()
    places = PlaceStore()
    holder = SingletonHolder()
    if not holder.enable():
        print("[!] Multi-instance is not active; a second client will replace the first.")
    watcher = ProcessWatcher(holder_state=holder.describe, on_game_start=make_event_closer(holder))
    watcher.start()

    # Phase 4: auto-rejoin
    from multi_instance import log as mi_log
    manager = RejoinManager(
        store=store,
        launch_fn=lambda account, place_id, job_id, extra=None: launch_account(
            store, account, place_id, job_id, extra=extra),
        list_pids_fn=_running_pids,
        kill_fn=kill_pid,
        log_fn=mi_log,
    )
    manager.start()

    try:
        while True:
            try:
                cmd = input("> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if cmd == "list":
                cmd_list(store)
            elif cmd == "addplace":
                cmd_addplace(places)
            elif cmd == "places":
                cmd_places(places)
            elif cmd == "removeplace":
                cmd_removeplace(places)
            elif cmd == "launch":
                cmd_launch(store, places, manager)
            elif cmd == "joinall":
                cmd_joinall(store, places, manager)
            elif cmd == "autorejoin":
                cmd_autorejoin(store, manager)
            elif cmd == "stop":
                cmd_stop(store, manager)
            elif cmd == "resume":
                cmd_resume(store, manager)
            elif cmd == "setgroup":
                cmd_setgroup(store)
            elif cmd == "setnote":
                cmd_setnote(store)
            elif cmd == "status":
                cmd_status(manager)
            elif cmd in ("quit", "exit", "q"):
                break
            elif cmd:
                print("Commands: list, addplace, places, removeplace, launch, joinall, "
                      "autorejoin, stop, resume, setgroup, setnote, status, quit")
    finally:
        manager.stop()
        watcher.stop()
        holder.disable()


if __name__ == "__main__":
    main()