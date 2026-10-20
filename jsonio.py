"""
Tiny JSON file helpers shared by the newer stores.

Same pattern the existing stores use: write to a .tmp file, then os.replace, so
a crash never leaves a half-written file. Reading never raises: a missing or
corrupt bookkeeping file gives the default instead of crashing the app.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def read_json(path, default=None):
    """Parsed JSON from `path`, or `default` if the file is missing, unreadable or not JSON."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, payload, indent: int = 2) -> None:
    """Atomically replaces `path` with `payload`. Raises OSError if it cannot write."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=indent)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise