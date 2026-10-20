"""
Phase 2: Roblox account storage with encrypted cookies (Windows only).

Stores your OWN accounts' .ROBLOSECURITY cookies encrypted with Windows DPAPI
(current-user scope), so the saved file can only be decrypted by the same
Windows user on the same PC. Cookies are never printed or logged.

Run:
    python account_store.py

Menu:
    add     add an account (paste its .ROBLOSECURITY cookie; input is hidden)
    list    show saved accounts
    check   decrypt an account's cookie and confirm it is still valid
    remove  delete a saved account
    quit

Data file: %APPDATA%\\RobloxAccountManager\\accounts.json

How to get a cookie for your own account:
    1. Log in at roblox.com in a private/incognito window.
    2. F12 -> Application tab -> Cookies -> https://www.roblox.com
    3. Copy the Value of .ROBLOSECURITY.
    4. Close the window WITHOUT clicking "Log out" (logging out invalidates it).

Treat a cookie like a password: anyone who has it can use that account.
Never share it with anyone, and never paste it into a chat.
"""

from __future__ import annotations

import base64
import ctypes
import getpass
import json
import os
import sys
import threading
import urllib.error
import urllib.request
import uuid
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "RobloxAccountManager"
DATA_FILE = DATA_DIR / "accounts.json"
DATA_VERSION = 1
GROUP_MAX_LENGTH = 40
NOTE_MAX_LENGTH = 200
RESERVED_GROUP_NAMES = {"all groups"}  # the group filter's "show everything" entry

AUTH_URL = "https://users.roblox.com/v1/users/authenticated"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RobloxAccountManager/0.2"

# Extra bytes mixed into DPAPI so other programs calling DPAPI as the same
# user don't decrypt our data by accident. Not a secret.
_ENTROPY = b"RobloxAccountManager.v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


# --------------------------------------------------------------------------
# Windows DPAPI (via ctypes, no extra packages)
# --------------------------------------------------------------------------
class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _make_blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf  # keep `buf` referenced while the blob is in use


def _dpapi():
    if sys.platform != "win32":
        raise OSError("Windows DPAPI is only available on Windows.")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_p = ctypes.POINTER(DATA_BLOB)
    crypt32.CryptProtectData.argtypes = [
        blob_p, wintypes.LPCWSTR, blob_p, wintypes.LPVOID, wintypes.LPVOID, wintypes.DWORD, blob_p]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        blob_p, ctypes.POINTER(wintypes.LPWSTR), blob_p, wintypes.LPVOID, wintypes.LPVOID,
        wintypes.DWORD, blob_p]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def protect(data: bytes) -> bytes:
    """Encrypt bytes for the current Windows user."""
    crypt32, kernel32 = _dpapi()
    in_blob, _k1 = _make_blob(data)
    ent_blob, _k2 = _make_blob(_ENTROPY)
    out_blob = DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob), "RobloxAccountManager", ctypes.byref(ent_blob),
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


def unprotect(data: bytes) -> bytes:
    """Decrypt bytes that were protected by the same Windows user."""
    crypt32, kernel32 = _dpapi()
    in_blob, _k1 = _make_blob(data)
    ent_blob, _k2 = _make_blob(_ENTROPY)
    out_blob = DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
        None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))


# --------------------------------------------------------------------------
# Roblox API check
# --------------------------------------------------------------------------
class CookieError(Exception):
    """The cookie was rejected or could not be verified."""


def clean_cookie(raw: str) -> str:
    """Accepts a pasted cookie with or without '.ROBLOSECURITY=' / quotes / ';'."""
    value = raw.strip().strip('"').strip("'").strip()
    if value.upper().startswith(".ROBLOSECURITY="):
        value = value.split("=", 1)[1]
    return value.strip().strip(";").strip().strip('"').strip("'")


def fetch_authenticated_user(cookie: str) -> dict:
    """Asks Roblox who this cookie belongs to. Returns {'id', 'name', 'displayName'}."""
    req = urllib.request.Request(
        AUTH_URL,
        headers={
            "Cookie": f".ROBLOSECURITY={cookie}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise CookieError("Roblox rejected this cookie (invalid, expired, or logged out).") from None
        raise CookieError(f"Roblox returned HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CookieError(f"Could not reach Roblox: {exc}") from None
    if "id" not in data or "name" not in data:
        raise CookieError("Unexpected response from Roblox.")
    return data


# --------------------------------------------------------------------------
# Data model + store
# --------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Account:
    id: str
    username: str
    userId: int
    encryptedCookie: str  # base64 of the DPAPI blob
    savedPlaceId: int | None = None
    savedJobId: str | None = None
    lastGameName: str | None = None
    autoRejoinEnabled: bool = False
    createdAt: str = field(default_factory=_now)
    group: str = ""  # optional label to launch / filter accounts together
    note: str = ""   # free-form note shown in the account list


class AccountStore:
    def __init__(self, path: Path = DATA_FILE):
        self.path = Path(path)
        self.accounts: list[Account] = []
        self._io_lock = threading.RLock()  # saves can come from several threads
        self.load()

    # ---- persistence
    def load(self) -> None:
        if not self.path.exists():
            self.accounts = []
            return
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        known = set(Account.__dataclass_fields__)
        self.accounts = [
            Account(**{k: v for k, v in item.items() if k in known})
            for item in raw.get("accounts", [])
        ]

    def save(self) -> None:
        with self._io_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": DATA_VERSION, "accounts": [asdict(a) for a in self.accounts]}
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self.path)  # atomic: never leaves a half-written file

    # ---- operations
    def find(self, selector: str) -> Account | None:
        """Look up by list number (1-based), username, or id."""
        selector = selector.strip()
        if selector.isdigit() and 1 <= int(selector) <= len(self.accounts):
            return self.accounts[int(selector) - 1]
        for a in self.accounts:
            if selector.lower() in (a.username.lower(), a.id.lower()):
                return a
        return None

    def add_from_cookie(self, raw_cookie: str) -> tuple[Account, bool]:
        """Verifies the cookie with Roblox, encrypts and stores it.
        Returns (account, updated_existing)."""
        cookie = clean_cookie(raw_cookie)
        if not cookie:
            raise CookieError("Empty cookie.")
        user = fetch_authenticated_user(cookie)
        encrypted = base64.b64encode(protect(cookie.encode("utf-8"))).decode("ascii")

        for existing in self.accounts:
            if existing.userId == int(user["id"]):
                existing.encryptedCookie = encrypted
                existing.username = user["name"]
                self.save()
                return existing, True

        account = Account(
            id=str(uuid.uuid4()),
            username=user["name"],
            userId=int(user["id"]),
            encryptedCookie=encrypted,
        )
        self.accounts.append(account)
        self.save()
        return account, False

    def get_cookie(self, account: Account) -> str:
        """Decrypts and returns the cookie. Keep it in memory only as long as needed."""
        return unprotect(base64.b64decode(account.encryptedCookie)).decode("utf-8")

    def remove(self, account: Account) -> None:
        self.accounts = [a for a in self.accounts if a.id != account.id]
        self.save()

    def set_group(self, account: Account, group: str) -> None:
        """Sets (or clears, with an empty string) the account's group label."""
        value = " ".join(group.split())[:GROUP_MAX_LENGTH]
        if value.lower() in RESERVED_GROUP_NAMES:
            raise ValueError(f"'{value}' is reserved. Pick another group name.")
        account.group = value
        self.save()

    def set_note(self, account: Account, note: str) -> None:
        """Sets (or clears) the account's note. Long notes are cut to NOTE_MAX_LENGTH."""
        account.note = note.strip()[:NOTE_MAX_LENGTH]
        self.save()


# --------------------------------------------------------------------------
# Console UI
# --------------------------------------------------------------------------
def cmd_list(store: AccountStore) -> None:
    if not store.accounts:
        print("No accounts saved yet. Use 'add'.")
        return
    for i, a in enumerate(store.accounts, 1):
        extra = f" | last game: {a.lastGameName}" if a.lastGameName else ""
        print(f"  {i}. {a.username} (userId {a.userId}){extra}")


def cmd_add(store: AccountStore) -> None:
    print("Paste the .ROBLOSECURITY cookie. Nothing will appear on screen while you paste.")
    raw = getpass.getpass("Cookie: ")
    try:
        account, updated = store.add_from_cookie(raw)
    except CookieError as exc:
        print(f"[!] {exc}")
        return
    except OSError as exc:
        print(f"[!] Could not encrypt/save: {exc}")
        return
    verb = "Updated cookie for" if updated else "Added"
    print(f"[ok] {verb} {account.username} (userId {account.userId}). Cookie stored encrypted.")


def _pick(store: AccountStore, prompt: str) -> Account | None:
    if not store.accounts:
        print("No accounts saved yet.")
        return None
    cmd_list(store)
    choice = input(prompt).strip()
    account = store.find(choice)
    if not account:
        print("[!] No such account.")
    return account


def cmd_check(store: AccountStore) -> None:
    account = _pick(store, "Which account (number or username)? ")
    if not account:
        return
    try:
        cookie = store.get_cookie(account)
    except OSError as exc:
        print(f"[!] Could not decrypt (wrong Windows user or different PC?): {exc}")
        return
    print(f"[ok] Decrypted OK ({len(cookie)} characters). Cookie not displayed.")
    try:
        user = fetch_authenticated_user(cookie)
        print(f"[ok] Still valid - logged in as {user['name']} (userId {user['id']}).")
    except CookieError as exc:
        print(f"[!] {exc}")
    finally:
        del cookie


def cmd_remove(store: AccountStore) -> None:
    account = _pick(store, "Remove which account (number or username)? ")
    if not account:
        return
    if input(f"Delete {account.username} from this tool? [y/N] ").strip().lower() in ("y", "yes"):
        store.remove(account)
        print("[ok] Removed.")


def main() -> None:
    if sys.platform != "win32":
        sys.exit("This tool only works on Windows (it uses Windows DPAPI).")
    print(__doc__)
    store = AccountStore()
    print(f"Data file: {store.path}\n")
    commands = {"add": cmd_add, "list": cmd_list, "check": cmd_check, "remove": cmd_remove}
    while True:
        try:
            cmd = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd in ("quit", "exit", "q"):
            break
        if cmd in commands:
            commands[cmd](store)
        elif cmd:
            print("Commands: add, list, check, remove, quit")


if __name__ == "__main__":
    main()