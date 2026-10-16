"""
Friend join (Feature C2): look a user up, see which server they are in, join it.

  find_user(query)            username, user ID, @name or a roblox.com/users/<id> link -> User
  get_presence(cookie, id)    one user's presence; needs a logged-in account (the cookie)
  resolve_join_target(p)      (placeId, jobId) when the user is in a game AND Roblox shows where
  online_friends(cookie, id)  the account's friends that are online right now, joinable ones first

Pure logic: no Tk, no Windows. The network call is the `fetch` parameter (default
net.request), so tests pass a fake. Joining itself uses the existing job-ID launch path.

Privacy: Roblox only reveals a user's server to accounts allowed to see it (normally friends,
and only if the user's "who can join me" setting permits). When it is hidden, presence says
"in game" but placeId / gameId are empty; resolve_join_target returns None and
describe_presence says 'unavailable'. A cookie is passed to net.request only, never logged,
and never stored here.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime

import net

USER_BY_ID_URL = "https://users.roblox.com/v1/users/{user_id}"
USERNAMES_URL = "https://users.roblox.com/v1/usernames/users"
PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"
FRIENDS_URL = "https://friends.roblox.com/v1/users/{user_id}/friends"
ONLINE_URL = "https://friends.roblox.com/v1/users/{user_id}/friends/online"
PRESENCE_BATCH = 50  # user IDs per presence request

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
PROFILE_LINK_RE = re.compile(r"roblox\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?users/(\d{1,15})", re.IGNORECASE)
MIN_JOB_ID_LENGTH = 8  # same rule the launcher uses to reject a mistyped job ID
PRESENCE_NAMES = {0: "offline", 1: "online (not in a game)", 2: "in a game", 3: "in Studio"}
# The friends/online endpoint spells the presence type out instead of using a number.
PRESENCE_TYPE_BY_NAME = {"offline": 0, "online": 1, "ingame": 2, "instudio": 3}


@dataclass(frozen=True)
class User:
    id: int
    name: str
    display_name: str = ""


@dataclass(frozen=True)
class Presence:
    user_id: int
    state: int                      # 0 offline, 1 online, 2 in game, 3 in Studio
    place_id: int | None = None
    root_place_id: int | None = None
    game_id: str | None = None      # the server (job) ID
    last_location: str = ""


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------
def _int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _positive(value) -> int | None:
    n = _int(value)
    return n if n is not None and n > 0 else None


def parse_user(item) -> User | None:
    if not isinstance(item, dict):
        return None
    uid, name = _positive(item.get("id")), item.get("name")
    if uid is None or not isinstance(name, str) or not name:
        return None
    display = item.get("displayName")
    return User(uid, name, display if isinstance(display, str) else "")


def _job_id(value) -> str | None:
    return value if isinstance(value, str) and len(value) >= MIN_JOB_ID_LENGTH else None


def _presence_type(value) -> int | None:
    if isinstance(value, str):
        return PRESENCE_TYPE_BY_NAME.get(value.strip().lower())
    return _int(value)


def parse_online_presences(payload) -> dict[int, Presence]:
    """friends/online JSON -> {friend id: Presence}. Entries look like
    {"id": 1, "userPresence": {"UserPresenceType": "InGame", "placeId": .., "gameInstanceId": ..}}."""
    found: dict[int, Presence] = {}
    data = payload.get("data") if isinstance(payload, dict) else None
    for item in data or []:
        if not isinstance(item, dict):
            continue
        uid, up = _positive(item.get("id")), item.get("userPresence")
        if uid is None or not isinstance(up, dict):
            continue
        state = _presence_type(up.get("UserPresenceType", up.get("userPresenceType")))
        if state is None:
            continue
        location = up.get("lastLocation")
        found[uid] = Presence(uid, state, _positive(up.get("placeId")), _positive(up.get("rootPlaceId")),
                              _job_id(up.get("gameInstanceId") or up.get("gameId")),
                              location if isinstance(location, str) else "")
    return found


def parse_presence(payload, user_id: int) -> Presence | None:
    """Roblox JSON -> the entry for `user_id`. None when Roblox sent no entry for that user."""
    if not isinstance(payload, dict):
        return None
    for item in payload.get("userPresences") or []:
        if not isinstance(item, dict) or _int(item.get("userId")) != user_id:
            continue
        state = _int(item.get("userPresenceType"))
        if state is None:
            return None
        game_id = _job_id(item.get("gameId") or item.get("gameInstanceId"))
        location = item.get("lastLocation")
        return Presence(user_id, state, _positive(item.get("placeId")), _positive(item.get("rootPlaceId")),
                        game_id, location if isinstance(location, str) else "")
    return None


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------
def find_user(query: str, *, fetch=net.request) -> User | None:
    """All digits -> look up by ID. A roblox.com/users/<id> link -> by ID. Otherwise a username
    (a leading @ is ignored). None = no such user, or the text cannot be a Roblox name.
    Raises net.HttpError when Roblox does not answer (429 = rate limited)."""
    text = (query or "").strip()
    if text.startswith("@"):
        text = text[1:].strip()
    link = PROFILE_LINK_RE.search(text)
    if link:
        text = link.group(1)
    if text.isascii() and text.isdigit():
        user_id = int(text)
        if user_id <= 0:
            return None
        try:
            return parse_user(fetch("GET", USER_BY_ID_URL.format(user_id=user_id)).json())
        except net.HttpError as exc:
            if exc.status in (400, 404):  # unknown or banned user
                return None
            raise
    if not USERNAME_RE.match(text):
        return None
    body = {"usernames": [text], "excludeBannedUsers": True}
    payload = fetch("POST", USERNAMES_URL, json_body=body).json()
    data = payload.get("data") if isinstance(payload, dict) else None
    for item in data or []:
        user = parse_user(item)
        if user is not None:
            return user
    return None


def get_presence(cookie: str, user_id: int, *, fetch=net.request) -> Presence | None:
    """Presence of `user_id` as seen by the account that owns `cookie`. None = Roblox returned no
    entry. Raises net.HttpError on failure (401/403 usually means the cookie was rejected)."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("user_id must be a positive integer")
    resp = fetch("POST", PRESENCE_URL, json_body={"userIds": [user_id]}, cookie=cookie)
    return parse_presence(resp.json(), user_id)


def list_friends(user_id: int, *, fetch=net.request) -> list[User]:
    """The friends of `user_id` (a friends list is public, so no cookie is needed).
    Raises net.HttpError on failure."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("This account has no Roblox user ID saved. Re-add the account.")
    payload = fetch("GET", FRIENDS_URL.format(user_id=user_id)).json()
    data = payload.get("data") if isinstance(payload, dict) else None
    users = [u for u in (parse_user(item) for item in data or []) if u is not None]
    return users


def get_presences(cookie: str, user_ids: list[int], *, fetch=net.request) -> dict[int, Presence]:
    """Presence of many users at once, as seen by the account that owns `cookie`."""
    found: dict[int, Presence] = {}
    for i in range(0, len(user_ids), PRESENCE_BATCH):
        chunk = list(user_ids[i:i + PRESENCE_BATCH])
        payload = fetch("POST", PRESENCE_URL, json_body={"userIds": chunk}, cookie=cookie).json()
        for uid in chunk:
            p = parse_presence(payload, uid)
            if p is not None:
                found[uid] = p
    return found


@dataclass(frozen=True)
class FriendEntry:
    user: User
    presence: Presence

    @property
    def joinable(self) -> bool:
        return resolve_join_target(self.presence) is not None

    @property
    def title(self) -> str:
        u = self.user
        return u.name if not u.display_name or u.display_name == u.name else f"{u.display_name} (@{u.name})"

    @property
    def status(self) -> str:
        p = self.presence
        if p.state == 2:
            return "In a game - joinable" if self.joinable else "In a game - server hidden"
        return PRESENCE_NAMES.get(p.state, "unknown")


def _sort_key(entry: FriendEntry):
    p = entry.presence
    rank = 0 if entry.joinable else 1 if p.state == 2 else 2 if p.state == 1 else 3
    return rank, entry.title.lower()


def online_friends(cookie: str, user_id: int, *, fetch=net.request) -> list[FriendEntry]:
    """Friends of the account that are online (in a game, online, or in Studio), joinable first.
    Uses Roblox's own friends/online list (it carries each friend's server ID); if that call is
    refused, falls back to asking the presence API about every friend."""
    users = list_friends(user_id, fetch=fetch)
    if not users:
        return []
    try:
        payload = fetch("GET", ONLINE_URL.format(user_id=user_id), cookie=cookie).json()
        presences = parse_online_presences(payload)
    except net.HttpError as exc:
        if exc.status in (429, 401):
            raise
        presences = get_presences(cookie, [u.id for u in users], fetch=fetch)
    entries = [FriendEntry(u, presences[u.id]) for u in users
               if u.id in presences and presences[u.id].state != 0]
    return sorted(entries, key=_sort_key)


def resolve_join_target(presence: Presence | None) -> tuple[int, str] | None:
    """(placeId, jobId) if the user is in a game and Roblox shows which server; else None."""
    if presence is None or presence.state != 2:
        return None
    if presence.place_id is None or presence.game_id is None:
        return None
    return presence.place_id, presence.game_id


# --------------------------------------------------------------------------
# Messages + last result (for the dialog and diagnostics)
# --------------------------------------------------------------------------
def describe_presence(presence: Presence | None) -> str:
    if presence is None:
        return "Roblox returned no status for this user."
    if presence.state == 2:
        if resolve_join_target(presence) is None:
            return ("In a game, but unavailable: their privacy settings hide the server from this account "
                    "(or it is a private server).")
        where = f" ({presence.last_location})" if presence.last_location else ""
        return f"In a game{where}. Joinable."
    return f"Not joinable: {PRESENCE_NAMES.get(presence.state, 'unknown status')}."


def describe_error(exc: Exception) -> str:
    if isinstance(exc, net.HttpError):
        if exc.status == 429:
            return "Roblox is rate-limiting lookups right now. Wait a minute and try again."
        if exc.status == 0:
            return f"Could not reach Roblox ({exc.reason or 'network error'})."
        if exc.status in (401, 403):
            return "Roblox rejected the selected account's login. Refresh that account's cookie and try again."
        return f"Roblox returned HTTP {exc.status}."
    if isinstance(exc, ValueError):
        return str(exc)
    return f"Unexpected error: {exc!r}"


last_result: dict = {}
_last_lock = threading.Lock()


def record_result(user_id: int | None, outcome: str, error: str | None = None) -> None:
    """Remembers the latest friend lookup/join for Diagnostics. Never stores a cookie or a job ID."""
    with _last_lock:
        last_result.clear()
        last_result.update(userId=user_id, outcome=outcome, error=error,
                           at=datetime.now().isoformat(timespec="seconds"))