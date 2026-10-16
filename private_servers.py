"""
Private-server list (Feature C3b): the private servers of a game that an account can join -
its OWN servers and its FRIENDS' servers - read from

    GET https://games.roblox.com/v1/games/{placeId}/private-servers
        ?cursor=&sortOrder=Desc&excludeFullGames=false

This call needs a logged-in account (the cookie), unlike the public server list. Roblox answers
with the servers the account may see: the ones it owns and the ones its friends own. Each entry
has a name, an owner, a player count and (when Roblox allows joining it) an "accessCode", which
is what the launcher needs (see private_links).

Pure logic: no Tk, no Windows. The network call is the `fetch` parameter (default net.request),
so tests pass a fake. The cookie goes to net.request only; it is never logged or stored here.
Access codes are secrets: they are hidden from repr, kept in memory only, never written to disk
or logs, and never shown in the UI.
"""

from __future__ import annotations

import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime

import net
import private_links
from places import _CODE_RE

PRIVATE_SERVERS_URL = "https://games.roblox.com/v1/games/{place_id}/private-servers"
PAGE_SIZES = (10, 25, 50, 100)

KIND_MINE = "mine"
KIND_FRIEND = "friend"
KIND_LABELS = {KIND_MINE: "Mine", KIND_FRIEND: "Friend"}
FILTERS = ("all", KIND_MINE, KIND_FRIEND)
FILTER_LABELS = {"all": "All servers", KIND_MINE: "Only mine", KIND_FRIEND: "Only friends'"}


@dataclass(frozen=True)
class PrivateServer:
    name: str
    owner_id: int | None
    owner_name: str
    playing: int
    max_players: int
    kind: str                                    # KIND_MINE or KIND_FRIEND
    access_code: str | None = field(default=None, repr=False)

    @property
    def joinable(self) -> bool:
        return bool(self.access_code)

    @property
    def has_room(self) -> bool:
        return self.playing < self.max_players


@dataclass(frozen=True)
class Found:
    servers: list          # de-duplicated, mine first, then friends', each group by name
    pages: int
    partial: bool = False  # a later page failed, so the list may be incomplete


def _int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def parse_servers(payload, user_id: int | None) -> tuple[list[PrivateServer], str | None]:
    """Roblox JSON -> (servers, next page cursor). Bad entries are skipped, never raised.
    A server is \"mine\" when its owner is `user_id`; everything else Roblox lists is a friend's."""
    if not isinstance(payload, dict):
        return [], None
    servers = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        playing, maximum = _int(item.get("playing")), _int(item.get("maxPlayers"))
        if playing is None or playing < 0 or maximum is None or maximum <= 0:
            continue
        owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
        owner_id = _int(owner.get("id"))
        owner_name = owner.get("name") if isinstance(owner.get("name"), str) else ""
        name = item.get("name") if isinstance(item.get("name"), str) and item.get("name").strip() else "(unnamed)"
        code = item.get("accessCode")
        code = code if isinstance(code, str) and _CODE_RE.match(code) else None
        kind = KIND_MINE if user_id is not None and owner_id == user_id else KIND_FRIEND
        servers.append(PrivateServer(name.strip(), owner_id, owner_name, playing, maximum, kind, code))
    cursor = payload.get("nextPageCursor")
    return servers, cursor if isinstance(cursor, str) and cursor else None


def list_page(place_id: int, cookie: str, user_id: int | None, *, cursor: str | None = None,
              limit: int = 100, fetch=net.request) -> tuple[list[PrivateServer], str | None]:
    """One page of private servers and the cursor of the next page (None = last page).
    Raises net.HttpError if Roblox does not answer (401 = the account is not logged in)."""
    if isinstance(place_id, bool) or not isinstance(place_id, int) or place_id <= 0:
        raise ValueError("place_id must be a positive integer")
    if not cookie:
        raise ValueError("an account (cookie) is needed to list private servers")
    if limit not in PAGE_SIZES:
        limit = 100
    query = {"sortOrder": "Desc", "excludeFullGames": "false", "limit": limit}
    if cursor:
        query["cursor"] = cursor
    url = PRIVATE_SERVERS_URL.format(place_id=place_id) + "?" + urllib.parse.urlencode(query)
    return parse_servers(fetch("GET", url, cookie=cookie).json(), user_id)


def find_private_servers(place_id: int, cookie: str, user_id: int | None, *, max_pages: int = 5,
                         fetch=net.request) -> Found:
    """Reads up to `max_pages` pages. An error on the FIRST page is raised; an error on a later
    page ends the search with what was found (partial=True)."""
    seen: dict[tuple, PrivateServer] = {}
    cursor, pages, partial = None, 0, False
    while pages < max(1, max_pages):
        try:
            batch, cursor = list_page(place_id, cookie, user_id, cursor=cursor, fetch=fetch)
        except net.HttpError:
            if pages == 0:
                raise
            partial = True
            break
        pages += 1
        for s in batch:
            # servers have no public ID in this list, so the owner + name + code identify one
            seen.setdefault((s.owner_id, s.name, s.access_code), s)
        if not cursor:
            break
    ordered = sorted(seen.values(), key=lambda s: (s.kind != KIND_MINE, s.name.lower(), s.owner_name.lower()))
    return Found(ordered, pages, partial)


def apply_filter(servers, which: str) -> list[PrivateServer]:
    if which not in FILTERS:
        raise ValueError(f"unknown filter {which!r}; use one of {', '.join(FILTERS)}")
    return [s for s in servers if which == "all" or s.kind == which]


def extra_for(server: PrivateServer) -> dict | None:
    """The launch data (see private_links) for one listed server; None when Roblox gave no code."""
    if not server.access_code:
        return None
    return {"accessCode": server.access_code, "linkCode": None, "origin": private_links.DEFAULT_ORIGIN}


def describe_error(exc: Exception) -> str:
    if isinstance(exc, net.HttpError):
        if exc.status == 401:
            return "Roblox says this account is not logged in. Its cookie has probably expired: re-add the account."
        if exc.status == 403:
            return "Roblox refused the request (HTTP 403). Try another account, or wait a minute."
        if exc.status == 429:
            return "Roblox is rate-limiting server lists right now. Wait a minute and try again."
        if exc.status == 0:
            return f"Could not reach Roblox ({exc.reason or 'network error'})."
        if exc.status in (400, 404):
            return "Roblox says this place has no private-server list (wrong place ID, or the game has no private servers)."
        return f"Roblox returned HTTP {exc.status}."
    if isinstance(exc, ValueError):
        return str(exc)
    return f"Unexpected error: {exc!r}"


last_result: dict = {}
_last_lock = threading.Lock()


def record_result(place_id: int, count: int, error: str | None = None) -> None:
    """Remembers the latest listing for Diagnostics. Never stores a code."""
    with _last_lock:
        last_result.clear()
        last_result.update(placeId=place_id, count=count, error=error,
                           at=datetime.now().isoformat(timespec="seconds"))