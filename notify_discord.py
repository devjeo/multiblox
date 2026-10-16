"""
Discord channel: posts a message to a Discord webhook.

The webhook URL is a secret (anyone who has it can post to your channel). It is
read from secrets_store at send time, never kept on this object, never logged,
and refused unless it really is a Discord webhook URL.
"""

from __future__ import annotations

import re

import net
import secrets_store

WEBHOOK_SLOT = secrets_store.WEBHOOK_SLOT
MAX_TEXT = 1800

_WEBHOOK_RE = re.compile(
    r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_\-]+/?$",
    re.I)


def is_discord_webhook(url) -> bool:
    return isinstance(url, str) and bool(_WEBHOOK_RE.match(url.strip()))


def format_text(n) -> str:
    text = f"**[{n.event}]** {n.account}: {n.detail}".rstrip().rstrip(":")
    return text[:MAX_TEXT]


class DiscordChannel:
    name = "discord"

    def __init__(self, webhook_fn=None, request_fn=None) -> None:
        self._webhook_fn = webhook_fn or (lambda: secrets_store.get(WEBHOOK_SLOT))
        self._request = request_fn or net.request

    def send(self, n) -> bool:
        """True if Discord accepted the message. Never raises."""
        try:
            url = self._webhook_fn()
            if not is_discord_webhook(url):
                return False
            self._request("POST", url.strip(), retries=2, timeout=10.0,
                          json_body={"content": format_text(n), "allowed_mentions": {"parse": []}})
            return True
        except Exception:
            return False