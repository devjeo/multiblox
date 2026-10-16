"""
Game search (the online half of the game picker): turns plain text into games.

  search_games(text)       Roblox's own site search (apis.roblox.com/search-api/omni-search)
  icon_urls(universe_ids)  thumbnails.roblox.com game icons, one request for the whole page
  download(url)            the icon's PNG bytes
  looks_like_id(text)      True for a place ID or a roblox.com game link (no search needed)
  clean_name(name)         a name that is safe to put in Tk widgets and in places.json

Needs no login. Pure logic: no Tk, no Windows. The network call is the `fetch` parameter
(default net.request), so tests pass a fake.

omni-search answers {"searchResults": [{"contentGroupType": "Game", "contents": [{universeId,
name, playerCount, totalUpVotes, totalDownVotes, rootPlaceId, creatorName, isSponsored, ...}]}],
"nextPageToken": "..."}. Sponsored entries are adverts and are left out unless asked for.
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.parse
import uuid
from dataclasses import dataclass

import net
from places import parse_place_input

OMNI_URL = "https://apis.roblox.com/search-api/omni-search"
ICONS_URL = "https://thumbnails.roblox.com/v1/games/icons"
ICON_SIZE = "50x50"
MAX_IMAGE_BYTES = 200_000       # an icon is a few KB; anything huge is not an icon
MAX_QUERY_LENGTH = 100


@dataclass(frozen=True)
class GameResult:
    universe_id: int
    place_id: int            # the game's root place, which is what the launcher needs
    name: str
    players: int = 0
    up_votes: int = 0
    down_votes: int = 0
    creator: str = ""
    age_label: str = ""
    sponsored: bool = False

    @property
    def rating(self) -> int | None:
        """Percent of votes that are up votes; None when nobody voted."""
        total = self.up_votes + self.down_votes
        return round(100 * self.up_votes / total) if total > 0 else None


def new_session_id() -> str:
    return str(uuid.uuid4())


def looks_like_id(text: str) -> bool:
    """A place ID or a roblox.com game link: the picker uses it directly instead of searching."""
    place_id, _ = parse_place_input(text or "")
    return place_id is not None


def clean_name(name: str, fallback: str = "") -> str:
    """Removes emoji and other symbols (older Tk builds choke on the ones outside the basic plane, and
    the rest draw as boxes), joiners and variation selectors, then tidies the brackets and spaces
    they leave behind."""
    text = "".join(ch for ch in (name or "")
                   if ord(ch) <= 0xFFFF and ch not in "\ufe0e\ufe0f"
                   and unicodedata.category(ch) not in ("So", "Cf", "Cs", "Co", "Cn"))
    text = re.sub(r"\[\s*\]|\(\s*\)|\{\s*\}", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -|")
    return text or fallback


def _int(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def parse_results(payload, *, include_sponsored: bool = False) -> tuple[list[GameResult], str | None]:
    """Roblox JSON -> (games, next page token). Bad entries are skipped, never raised."""
    if not isinstance(payload, dict):
        return [], None
    games, seen = [], set()
    for group in payload.get("searchResults") or []:
        if not isinstance(group, dict):
            continue
        for item in group.get("contents") or []:
            if not isinstance(item, dict) or item.get("contentType", "Game") != "Game":
                continue
            universe, place = _int(item.get("universeId")), _int(item.get("rootPlaceId"))
            if universe <= 0 or place <= 0 or place in seen:
                continue
            sponsored = bool(item.get("isSponsored"))
            if sponsored and not include_sponsored:
                continue
            name = clean_name(_str(item.get("name")), fallback=f"Place {place}")
            age = _str(item.get("ageRecommendationDisplayName")).replace("Maturity: ", "")
            seen.add(place)
            games.append(GameResult(universe, place, name, _int(item.get("playerCount")),
                                    _int(item.get("totalUpVotes")), _int(item.get("totalDownVotes")),
                                    clean_name(_str(item.get("creatorName"))), age, sponsored))
    token = payload.get("nextPageToken")
    return games, token if isinstance(token, str) and token else None


def search_games(query: str, *, page_token: str = "", session_id: str | None = None,
                 include_sponsored: bool = False, fetch=net.request) -> tuple[list[GameResult], str | None]:
    """One page of results for a text search and the token of the next page (None = no more).
    Raises ValueError for empty text and net.HttpError when Roblox does not answer."""
    text = (query or "").strip()[:MAX_QUERY_LENGTH]
    if not text:
        raise ValueError("Type a game name to search for.")
    params = {"searchQuery": text, "pageToken": page_token or "",
              "sessionId": session_id or new_session_id(), "pageType": "all"}
    url = OMNI_URL + "?" + urllib.parse.urlencode(params)
    return parse_results(fetch("GET", url).json(), include_sponsored=include_sponsored)


def icon_urls(universe_ids, *, fetch=net.request) -> dict[int, str]:
    """universe ID -> icon image URL, for the icons Roblox has finished. Never raises for a
    missing icon; a failed request raises net.HttpError (icons are optional, callers ignore it)."""
    ids = [i for i in dict.fromkeys(universe_ids) if isinstance(i, int) and not isinstance(i, bool) and i > 0]
    if not ids:
        return {}
    query = {"universeIds": ",".join(str(i) for i in ids[:100]), "returnPolicy": "PlaceHolder",
             "size": ICON_SIZE, "format": "Png", "isCircular": "false"}
    payload = fetch("GET", ICONS_URL + "?" + urllib.parse.urlencode(query)).json()
    found = {}
    for item in (payload.get("data") if isinstance(payload, dict) else None) or []:
        if not isinstance(item, dict) or item.get("state") != "Completed":
            continue
        target, image = _int(item.get("targetId")), item.get("imageUrl")
        if target > 0 and isinstance(image, str) and image.startswith("https://"):
            found[target] = image
    return found


def download(url: str, *, fetch=net.request) -> bytes | None:
    """The PNG at `url`, or None when it is not a small PNG (so it is never handed to Tk)."""
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    body = fetch("GET", url, headers={"Accept": "image/png,image/*;q=0.8,*/*;q=0.5"}).body
    if not body.startswith(b"\x89PNG\r\n\x1a\n") or len(body) > MAX_IMAGE_BYTES:
        return None
    return body


def describe_error(exc: Exception) -> str:
    if isinstance(exc, net.HttpError):
        if exc.status == 429:
            return "Roblox is rate-limiting searches right now. Wait a minute and try again."
        if exc.status == 0:
            return f"Could not reach Roblox ({exc.reason or 'network error'})."
        return f"Roblox returned HTTP {exc.status}."
    if isinstance(exc, json.JSONDecodeError):      # a ValueError too, so it must be checked first
        return "Roblox sent an answer this tool could not read."
    if isinstance(exc, ValueError):
        return str(exc)
    return f"Unexpected error: {exc!r}"