"""
Server finder (Feature C1): lists a game's PUBLIC servers and picks one by rule.

Uses games.roblox.com/v1/games/{placeId}/servers/Public, which needs no login.
Pure logic: no Tk, no Windows. The network call is the `fetch` parameter
(default net.request), so tests pass a fake.

Modes (see rank / choose). Every mode first drops servers with fewer than
`min_players` players; the order the API returns servers in is never relied on.

  emptiest     fewest players first
  fullest      most players first (may be completely full, and then cannot be joined)
  best         most players first, but only servers that still have room
  min_players  fewest players first among servers that have room
               (with min_players=N: "the quietest server that already has N people")

Ties are broken by lower ping, then job ID, so the result is deterministic.

Expect HTTP 429: Roblox rate-limits this list hard. net.request already retries
with backoff; if it still fails, HttpError is raised and describe_error() turns
it into a sentence for the dialog.
"""

from __future__ import annotations

import threading
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

import net

SERVERS_URL = "https://games.roblox.com/v1/games/{place_id}/servers/Public"
PAGE_SIZES = (10, 25, 50, 100)
MODES = ("best", "emptiest", "fullest", "min_players")
MODE_LABELS = {
    "best": "Best (fullest server that still has room)",
    "emptiest": "Emptiest",
    "fullest": "Fullest",
    "min_players": "Quietest with room",
}
MIN_JOB_ID_LENGTH = 8  # same rule the launcher uses to reject a mistyped job ID


@dataclass(frozen=True)
class Server:
    job_id: str
    playing: int
    max_players: int
    ping: int | None = None

    @property
    def has_room(self) -> bool:
        return self.playing < self.max_players


@dataclass(frozen=True)
class Found:
    servers: list          # de-duplicated, in the order received
    pages: int             # pages read
    partial: bool = False  # a later page failed, so the list may be incomplete


# --------------------------------------------------------------------------
# Parsing + listing
# --------------------------------------------------------------------------
def _int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def parse_servers(payload) -> tuple[list[Server], str | None]:
    """Roblox JSON -> (servers, next page cursor). Bad entries are skipped, never raised."""
    if not isinstance(payload, dict):
        return [], None
    servers = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        job = item.get("id")
        playing, maximum = _int(item.get("playing")), _int(item.get("maxPlayers"))
        if not isinstance(job, str) or len(job) < MIN_JOB_ID_LENGTH:
            continue
        if playing is None or playing < 0 or maximum is None or maximum <= 0:
            continue
        servers.append(Server(job, playing, maximum, _int(item.get("ping"))))
    cursor = payload.get("nextPageCursor")
    return servers, cursor if isinstance(cursor, str) and cursor else None


def list_servers(place_id: int, *, cursor: str | None = None, limit: int = 100,
                 fetch=net.request) -> tuple[list[Server], str | None]:
    """One page of public servers and the cursor for the next page (None = last page).
    Raises net.HttpError if Roblox does not answer (429 = rate limited)."""
    if isinstance(place_id, bool) or not isinstance(place_id, int) or place_id <= 0:
        raise ValueError("place_id must be a positive integer")
    if limit not in PAGE_SIZES:
        limit = 100
    query = {"sortOrder": "Asc", "excludeFullGames": "false", "limit": limit}
    if cursor:
        query["cursor"] = cursor
    url = SERVERS_URL.format(place_id=place_id) + "?" + urllib.parse.urlencode(query)
    return parse_servers(fetch("GET", url).json())


def find_servers(place_id: int, *, max_pages: int = 5, fetch=net.request) -> Found:
    """Reads up to `max_pages` pages. An error on the FIRST page is raised; an error on a later
    page just ends the search with what was already found (partial=True)."""
    seen: dict[str, Server] = {}
    cursor, pages, partial = None, 0, False
    while pages < max(1, max_pages):
        try:
            batch, cursor = list_servers(place_id, cursor=cursor, fetch=fetch)
        except net.HttpError:
            if pages == 0:
                raise
            partial = True
            break
        pages += 1
        for s in batch:  # servers come and go while we page, so the same one can show up twice
            seen.setdefault(s.job_id, s)
        if not cursor:
            break
    return Found(list(seen.values()), pages, partial)


# --------------------------------------------------------------------------
# Choosing
# --------------------------------------------------------------------------
def rank(servers, mode: str, *, min_players: int = 0) -> list[Server]:
    """The servers that qualify, best match first. Raises ValueError for an unknown mode."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; use one of {', '.join(MODES)}")
    pool = [s for s in servers if s.playing >= min_players]
    if mode in ("best", "min_players"):
        pool = [s for s in pool if s.has_room]
    tie = lambda s: (s.ping if s.ping is not None else 10**9, s.job_id)
    if mode in ("best", "fullest"):
        return sorted(pool, key=lambda s: (-s.playing, *tie(s)))
    return sorted(pool, key=lambda s: (s.playing, *tie(s)))


def choose(servers, mode: str, *, min_players: int = 0) -> Server | None:
    ranked = rank(servers, mode, min_players=min_players)
    return ranked[0] if ranked else None


def find_and_choose(place_id: int, mode: str, *, min_players: int = 0, max_pages: int = 5,
                    fetch=net.request) -> Server | None:
    found = find_servers(place_id, max_pages=max_pages, fetch=fetch)
    return choose(found.servers, mode, min_players=min_players)


# --------------------------------------------------------------------------
# Messages + last result (for the dialog and diagnostics)
# --------------------------------------------------------------------------
def describe_error(exc: Exception) -> str:
    if isinstance(exc, net.HttpError):
        if exc.status == 429:
            return "Roblox is rate-limiting server lists right now. Wait a minute and try again."
        if exc.status == 0:
            return f"Could not reach Roblox ({exc.reason or 'network error'})."
        if exc.status in (400, 404):
            return "Roblox says this place has no public server list (wrong place ID, or the game is private)."
        return f"Roblox returned HTTP {exc.status}."
    if isinstance(exc, ValueError):
        return str(exc)
    return f"Unexpected error: {exc!r}"


last_result: dict = {}
_last_lock = threading.Lock()


def record_result(place_id: int, mode: str, count: int, job_id: str | None, error: str | None = None) -> None:
    """Remembers the latest finder run so Diagnostics can show it."""
    with _last_lock:
        last_result.clear()
        last_result.update(placeId=place_id, mode=mode, count=count, jobId=job_id, error=error,
                           at=datetime.now().isoformat(timespec="seconds"))