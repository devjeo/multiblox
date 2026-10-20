"""
Named secrets, encrypted with Windows DPAPI (same protection as the cookies).

File: %APPDATA%\\RobloxAccountManager\\secrets.json
Shape: {"version": 1, "secrets": {"<name>": "<base64 of the DPAPI blob>"}}

First slot: "notify.webhookUrl" (the Discord webhook for notifications).

_protect / _unprotect are module-level seams: tests replace them with a fake
cipher so this works off Windows. get() never raises; it returns None when the
secret is missing or cannot be decrypted (different Windows user or PC).
"""

from __future__ import annotations

import base64
import threading
from pathlib import Path

from account_store import DATA_DIR
from jsonio import read_json, write_json_atomic

SECRETS_FILE = DATA_DIR / "secrets.json"
WEBHOOK_SLOT = "notify.webhookUrl"

_lock = threading.RLock()


def _protect(data: bytes) -> bytes:
    from account_store import protect
    return protect(data)


def _unprotect(data: bytes) -> bytes:
    from account_store import unprotect
    return unprotect(data)


def _load(path: Path | None) -> dict[str, str]:
    raw = read_json(path or SECRETS_FILE, {})
    items = raw.get("secrets") if isinstance(raw, dict) else None
    if not isinstance(items, dict):
        return {}
    return {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, str)}


def get(name: str, *, path: Path | None = None) -> str | None:
    blob = _load(path).get(name)
    if not blob:
        return None
    try:
        return _unprotect(base64.b64decode(blob)).decode("utf-8")
    except (OSError, ValueError):
        return None


def set(name: str, value: str, *, path: Path | None = None) -> None:  # noqa: A001 (name from the plan)
    """Encrypts and stores `value`. Raises OSError if it cannot encrypt or write."""
    with _lock:
        secrets = _load(path)
        secrets[name] = base64.b64encode(_protect(value.encode("utf-8"))).decode("ascii")
        write_json_atomic(path or SECRETS_FILE, {"version": 1, "secrets": secrets})


def delete(name: str, *, path: Path | None = None) -> None:
    with _lock:
        secrets = _load(path)
        if name in secrets:
            del secrets[name]
            write_json_atomic(path or SECRETS_FILE, {"version": 1, "secrets": secrets})


def list_names(*, path: Path | None = None) -> list[str]:
    return sorted(_load(path))


def has(name: str, *, path: Path | None = None) -> bool:
    """True if a value is stored under `name` (it may still fail to decrypt)."""
    return name in _load(path)