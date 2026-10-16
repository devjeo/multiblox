"""
Small HTTP helper for NEW code (existing urllib call sites are left alone).

    request(method, url, *, headers=None, json_body=None, cookie=None,
            timeout=15.0, retries=2) -> Response

  * Retries timeouts, connection errors, 5xx and 429 (honouring Retry-After,
    capped at 10 s) with a 0.5 * 2**attempt backoff. Other 4xx are never retried.
  * A POST that carries a cookie does the CSRF handshake: on 403 with an
    x-csrf-token header it retries once with that token. GET never does.
  * Raises HttpError on final failure. HttpError's text never contains the URL,
    headers or body (a Discord webhook URL is a secret); they are attributes.
  * Nothing is logged here.

_open and _sleep are seams so tests can fake the network and skip waiting.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RobloxAccountManager/0.7"
MAX_RETRY_AFTER = 10.0


def _open(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _header(headers: dict, name: str):
    low = name.lower()
    for key, value in headers.items():
        if str(key).lower() == low:
            return value
    return None


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    def json(self):
        """The body parsed as JSON; None for an empty body (e.g. HTTP 204)."""
        if not self.body.strip():
            return None
        return json.loads(self.body.decode("utf-8"))

    def header(self, name: str):
        return _header(self.headers, name)


class HttpError(Exception):
    """`status` is 0 when there was no HTTP answer at all (network failure)."""

    def __init__(self, status: int, body: bytes = b"", url: str = "", reason: str = "") -> None:
        super().__init__(f"HTTP {status}" if status else f"network error ({reason or 'unknown'})")
        self.status, self.body, self.url, self.reason = status, body, url, reason


def _once(method, url, headers, data, timeout):
    """One attempt. Returns (status, headers, body, network_error_text or None)."""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _open(req, timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            return int(status), dict(resp.headers.items()), resp.read(), None
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read() or b""
        except Exception:
            body = b""
        try:
            hdrs = dict(exc.headers.items()) if exc.headers is not None else {}
        except Exception:
            hdrs = {}
        return exc.code, hdrs, body, None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        return 0, {}, b"", str(getattr(exc, "reason", None) or exc)


def request(method: str, url: str, *, headers: dict | None = None, json_body=None,
            cookie: str | None = None, timeout: float = 15.0, retries: int = 2) -> Response:
    method = method.upper()
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
    elif method == "POST":
        data = b"{}"
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    if cookie:
        hdrs["Cookie"] = f".ROBLOSECURITY={cookie}"
        hdrs["Referer"] = "https://www.roblox.com/"
    if headers:
        hdrs.update(headers)

    attempt = 0
    csrf_done = False
    while True:
        status, resp_headers, body, net_error = _once(method, url, hdrs, data, timeout)
        if 0 < status < 400:
            return Response(status, resp_headers, body)
        if status == 403 and method == "POST" and cookie and not csrf_done:
            token = _header(resp_headers, "x-csrf-token")
            if token:
                hdrs["X-CSRF-TOKEN"] = token
                csrf_done = True
                continue
        if (status == 0 or status == 429 or status >= 500) and attempt < retries:
            delay = 0.5 * 2 ** attempt
            if status == 429:
                try:
                    delay = float(_header(resp_headers, "retry-after"))
                except (TypeError, ValueError):
                    pass
                delay = min(max(delay, 0.0), MAX_RETRY_AFTER)
            _sleep(delay)
            attempt += 1
            continue
        raise HttpError(status, body, url, net_error or "")