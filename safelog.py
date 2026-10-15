"""
Log hygiene: strips anything that looks like a secret before it is printed or
written to a log / diagnostics report.

Redacted: .ROBLOSECURITY values (the "_|WARNING:-DO-NOT-SHARE..." cookie),
roblox-player launch tickets (gameinfo:...), CSRF/auth header values, and any
other very long token-like string. Normal log lines, PIDs and job IDs
(36 characters) are left alone.
"""

from __future__ import annotations

import re

_PLACEHOLDER = "<redacted>"

_PATTERNS = (
    re.compile(r"(\.ROBLOSECURITY\s*[=:]\s*)[^\s;\"']+", re.I),
    re.compile(r"_\|WARNING:[^\s\"';,]+"),
    re.compile(r"(gameinfo:)[^\s+&\"']+", re.I),
    re.compile(r"((?:x-csrf-token|rbx-authentication-ticket)\s*[=:]\s*)[^\s;\"']+", re.I),
    re.compile(r"[A-Za-z0-9_\-+/=.|]{80,}"),
)


def redact(text: str) -> str:
    out = text
    for pattern in _PATTERNS:
        if pattern.groups:
            out = pattern.sub(lambda m: m.group(1) + _PLACEHOLDER, out)
        else:
            out = pattern.sub(_PLACEHOLDER, out)
    return out