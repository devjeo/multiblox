"""
Private servers (Feature C3), the parts that do not need Tk or Windows.

What Roblox's own web launch sends for a private server was captured once (join from the game's
private-server list):

    PlaceLauncher.ashx?request=RequestPrivateGame&browserTrackerId=..&placeId=..
        &accessCode=<uuid>&linkCode=<empty>&joinAttemptId=<uuid>&joinAttemptOrigin=privateServerListJoin

This module keeps that data in a small dict, "extra", that lives in memory only:

    {"accessCode": str | None, "linkCode": str | None, "origin": str | None}

It is never written to accounts.json, sessions.json, settings or any log (the codes are secrets;
safelog redacts them too). Share links (roblox.com/share?code=..) cannot be resolved yet: that call
has not been captured, so nothing here guesses it.

EXPERIMENTAL: only the list-join shape above is verified. Whether an access code keeps working
across rejoins is unknown until it is tested on a real machine.
"""

from __future__ import annotations

import threading
from datetime import datetime

from places import _CODE_RE, _ORIGIN_RE, PlaceInput

DEFAULT_ORIGIN = "privateServerListJoin"   # the only origin seen in a real capture


def is_private(extra) -> bool:
    return isinstance(extra, dict) and bool(extra.get("accessCode") or extra.get("linkCode"))


def validate(extra) -> tuple[str, str, str]:
    """(accessCode, linkCode, origin) ready for a URL ('' where absent). Raises ValueError for
    anything that is not a plain code, so nothing odd is ever put into a launch URL."""
    if not is_private(extra):
        raise ValueError("no private server code to launch with")
    access, link = extra.get("accessCode") or "", extra.get("linkCode") or ""
    for value in (access, link):
        if value and not _CODE_RE.match(value):
            raise ValueError("the private server code has an unexpected format")
    origin = extra.get("origin") or DEFAULT_ORIGIN
    if not _ORIGIN_RE.match(origin):
        origin = DEFAULT_ORIGIN
    return access, link, origin


def extra_from(parsed: PlaceInput | None) -> dict | None:
    """The launch data for a parsed link, or None unless it is a private server read from a launch link."""
    if parsed is None or not parsed.is_private or not parsed.verified:
        return None
    return {"accessCode": parsed.access_code, "linkCode": parsed.link_code, "origin": parsed.origin}


def assess(parsed: PlaceInput | None) -> tuple[bool, str]:
    """(can_arm, message for the dialog)."""
    if parsed is None:
        return False, ("Could not find a private server in that text. Paste the whole roblox-player:... "
                       "text you get when you join your private server. (A 'follow player' link has no "
                       "server in it.)")
    if parsed.share_code:
        return False, ("That is a share link. Share links cannot be resolved yet (that request has not been "
                       "captured). Join the private server in your browser and paste the roblox-player:... text.")
    if parsed.is_private and not parsed.verified:
        return False, ("That is a web link with a private-server code. Only the roblox-player:... text from "
                       "Roblox's own launch is accepted, because that is the shape that was verified.")
    if not parsed.is_private:
        return False, ("That link is for a normal (public) game, not a private server. Use the Game box and "
                       "the Server finder for that.")
    return True, f"Private server for place {parsed.place_id} found. Press Arm, then launch an account."


def describe(extra) -> str:
    return "private server (code hidden)" if is_private(extra) else "public server"


last_result: dict = {}
_last_lock = threading.Lock()


def record_result(event: str, place_id: int | None = None) -> None:
    """Remembers the latest arm/clear for Diagnostics. Never stores a code."""
    with _last_lock:
        last_result.clear()
        last_result.update(event=event, placeId=place_id, at=datetime.now().isoformat(timespec="seconds"))