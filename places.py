"""
Saved places (games) for the launcher.

Add a game once - by pasting its URL or its place ID - and pick it later by
number or name instead of typing the ID every time.

Stored in: %APPDATA%\\RobloxAccountManager\\places.json
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from account_store import DATA_DIR

PLACES_FILE = DATA_DIR / "places.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RobloxAccountManager/0.4"

_URL_ID_RE = re.compile(r"roblox\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?games/(\d+)(?:/([^/?#]+))?", re.I)
_QUERY_ID_RE = re.compile(r"[?&]placeId=(\d+)", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Parsing + name lookup
# --------------------------------------------------------------------------
def parse_place_input(text: str) -> tuple[int | None, str | None]:
    """Accepts a place ID or a roblox.com game link.
    Returns (place_id, name_guess_from_url) - either may be None."""
    text = text.strip()
    if text.isdigit():
        return int(text), None
    m = _URL_ID_RE.search(text)
    if m:
        slug = m.group(2)
        name = slug.replace("-", " ").strip() if slug else None
        return int(m.group(1)), name or None
    m = _QUERY_ID_RE.search(text)
    if m:
        return int(m.group(1)), None
    return None, None


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_place_name(place_id: int) -> str | None:
    """Best-effort: place ID -> universe ID -> game name. No login needed. Never raises."""
    try:
        universe = _get_json(f"https://apis.roblox.com/universes/v1/places/{place_id}/universe")
        universe_id = universe.get("universeId")
        if not universe_id:
            return None
        data = _get_json(f"https://games.roblox.com/v1/games?universeIds={universe_id}")
        items = data.get("data") or []
        return items[0].get("name") if items else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------
@dataclass
class Place:
    placeId: int
    name: str
    addedAt: str = field(default_factory=_now)


class PlaceStore:
    def __init__(self, path: Path = PLACES_FILE):
        self.path = Path(path)
        self.places: list[Place] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.places = []
            return
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        known = set(Place.__dataclass_fields__)
        self.places = [Place(**{k: v for k, v in p.items() if k in known}) for p in raw.get("places", [])]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "places": [asdict(p) for p in self.places]}, f, indent=2)
        os.replace(tmp, self.path)

    def add(self, place_id: int, name: str) -> tuple[Place, bool]:
        """Adds a place, or renames it if the ID is already saved. Returns (place, was_update)."""
        for p in self.places:
            if p.placeId == place_id:
                p.name = name
                self.save()
                return p, True
        place = Place(placeId=place_id, name=name)
        self.places.append(place)
        self.save()
        return place, False

    def remove(self, place: Place) -> None:
        self.places = [p for p in self.places if p.placeId != place.placeId]
        self.save()

    def find(self, selector: str) -> Place | None:
        """By list number (1-based, up to 3 digits), exact place ID, or name
        (exact first, then a unique partial match)."""
        selector = selector.strip()
        if not selector:
            return None
        if selector.isdigit():
            if len(selector) <= 3 and 1 <= int(selector) <= len(self.places):
                return self.places[int(selector) - 1]
            for p in self.places:
                if p.placeId == int(selector):
                    return p
            return None
        low = selector.lower()
        for p in self.places:
            if p.name.lower() == low:
                return p
        partial = [p for p in self.places if low in p.name.lower()]
        return partial[0] if len(partial) == 1 else None