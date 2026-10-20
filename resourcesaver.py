"""
Resource saver: lower what each Roblox client costs by writing client flags.

Roblox reads ClientSettings/ClientAppSettings.json from the newest version folder
when a client STARTS, so a profile has to be in place before launch. Roblox only
honours an allowlist of flags (anything else is ignored, which is harmless). Which
flags are honoured changes between Roblox releases, so the profile table below is
plain data meant to be edited. Verify a flag with `python resourcesaver.py apply Low`
and a launch, and only keep flags that visibly change behaviour.

Safety rules (nothing else in the Roblox install is ever touched):

  * only <Roblox>/Versions/<newest>/ClientSettings/ClientAppSettings.json is written;
  * the ClientSettings folder is created only if it is absent, and removed again on
    restore if we created it and it is empty;
  * the original file (bytes, or "did not exist") is backed up ONCE into
    resource_backup.json in DATA_DIR (the install folder is wiped by Roblox updates)
    and that backup is never overwritten while we own the file;
  * every apply merges the profile over the ORIGINAL content, so re-applying or
    switching profile never compounds;
  * if the file was changed by something else since we wrote it, the current file
    becomes the new original (and is never overwritten on restore);
  * a malformed or non-object file is left untouched and reported.

No tkinter, no Win32: everything takes paths, so it is testable anywhere. Command line:

    python resourcesaver.py status | apply <Normal|Low|Minimum> | restore
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from account_store import DATA_DIR
from jsonio import read_json, write_json_atomic

STATE_FILE = DATA_DIR / "resource_backup.json"
SETTINGS_NAME = "ClientAppSettings.json"
FOLDER_NAME = "ClientSettings"

# ---------------------------------------------------------------------------
# Profiles. EDITABLE DATA. A profile is {flag name: value}; Normal is empty, which means
# "restore the original". Minimum must contain every Low flag (a test checks this).
# UNVERIFIED flags are marked: confirm them on your Roblox build before relying on them.
# ---------------------------------------------------------------------------
PROFILES: dict[str, dict] = {
    "Normal": {},
    "Low": {
        "DFIntTaskSchedulerTargetFps": 20,      # FPS cap (the likely one; verify)
    },
    "Minimum": {
        "DFIntTaskSchedulerTargetFps": 10,
        # Add flags here only after confirming they change behaviour on your machine.
    },
}
PROFILE_ORDER = ("Normal", "Low", "Minimum")


@dataclass(frozen=True)
class ApplyResult:
    status: str   # applied | unchanged | restored | skipped | warning | error
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("applied", "unchanged", "restored")


# ---------------------------------------------------------------------------
# Locating the install
# ---------------------------------------------------------------------------
def default_roots() -> list[Path]:
    roots = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Roblox" / "Versions")
    for var in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(var)
        if base:
            roots.append(Path(base) / "Roblox" / "Versions")
    return roots


def current_version_dir(roots=None) -> Path | None:
    """Newest Versions/<ver> folder (by modification time) that contains RobloxPlayerBeta.exe."""
    best, best_time = None, -1.0
    for root in (roots if roots is not None else default_roots()):
        try:
            children = list(Path(root).iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if (child / "RobloxPlayerBeta.exe").exists():
                    mtime = child.stat().st_mtime
                    if mtime > best_time:
                        best, best_time = child, mtime
            except OSError:
                continue
    return best


def settings_path(version_dir) -> Path:
    return Path(version_dir) / FOLDER_NAME / SETTINGS_NAME


_settings_path_for = settings_path  # ensure_applied has a parameter that shadows the name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _flags_for(profile) -> dict | None:
    if isinstance(profile, dict):
        return dict(profile)
    flags = PROFILES.get(profile)
    return dict(flags) if flags is not None else None


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _parse_object(data: bytes):
    """The parsed dict, or None if `data` is not a JSON object."""
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _load_state(state_path) -> dict | None:
    state = read_json(state_path, None)
    if not isinstance(state, dict) or not isinstance(state.get("path"), str):
        return None
    if not isinstance(state.get("existed"), bool):
        return None
    if state["existed"] and not isinstance(state.get("originalB64"), str):
        return None
    return state


def _original_bytes(state: dict) -> bytes | None:
    return base64.b64decode(state["originalB64"]) if state["existed"] else None


def _noop(_msg):
    pass


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
def _restore_state(state: dict, state_path, log) -> bool:
    """Puts the file described by `state` back and drops the state. Never overwrites a file
    that something else edited after we wrote it."""
    path = Path(state["path"])
    current = _read_bytes(path)
    original = _original_bytes(state)
    written = current is not None and _sha(current) == state.get("writtenSha")
    if current is None:
        pass  # nothing there; just clean up bookkeeping
    elif written:
        if original is not None:
            _write_atomic(path, original)
            log(f"[i] Resource saver: restored the original {SETTINGS_NAME}.")
        else:
            path.unlink()
            log(f"[i] Resource saver: removed {SETTINGS_NAME} (it did not exist before).")
    elif original is not None and current == original:
        pass  # already the original
    else:
        log(f"[!] Resource saver: {SETTINGS_NAME} was changed by something else, so it was left as it is.")
    if state.get("createdDir") and original is None:
        try:
            path.parent.rmdir()  # only succeeds if empty
        except OSError:
            pass
    try:
        Path(state_path).unlink()
    except FileNotFoundError:
        pass
    return True


def restore(*, settings_path, state_path, log_fn=None) -> bool:
    """Restores the original state. True if there was a backup to act on, False if not."""
    log = log_fn or _noop
    state = _load_state(state_path)
    if state is None:
        return False
    return _restore_state(state, state_path, log)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
def apply_profile(profile, *, settings_path, state_path, log_fn=None) -> ApplyResult:
    """Makes the settings file `original + profile flags`. `profile` is a profile name or a dict.
    The Normal profile (no flags) is the same as restore."""
    log = log_fn or _noop
    path = Path(settings_path)
    flags = _flags_for(profile)
    if flags is None:
        return ApplyResult("error", f"unknown profile {profile!r}")
    if not flags:
        did = restore(settings_path=path, state_path=state_path, log_fn=log)
        return ApplyResult("restored", "original settings restored" if did else "nothing to restore")

    state = _load_state(state_path)
    if state is not None and Path(state["path"]) != path:
        # Roblox updated: the old version folder is normally gone. Clean up if it is not.
        log("[i] Resource saver: a different Roblox version folder is in use; cleaning up the old one.")
        try:
            _restore_state(state, state_path, log)
        except OSError as exc:
            log(f"[!] Resource saver: could not clean up the old folder ({exc}).")
            try:
                Path(state_path).unlink()
            except OSError:
                pass
        state = None

    current = _read_bytes(path)
    new_state = state

    if state is not None:
        original = _original_bytes(state)
        is_ours = current is not None and _sha(current) == state.get("writtenSha")
        is_original = (current == original) if original is not None else (current is None)
        if not (is_ours or is_original):
            # Something else changed the file (or deleted it). The current content is the truth now.
            if current is None:
                log("[i] Resource saver: the settings file was removed by something else; "
                    "treating 'no file' as the original.")
                new_state = {"path": str(path), "existed": False, "createdDir": not path.parent.exists()}
            else:
                if _parse_object(current) is None:
                    return ApplyResult("warning", f"{SETTINGS_NAME} was edited and is not a JSON object; left untouched")
                log("[i] Resource saver: the settings file was edited by something else; "
                    "using the edited version as the new original.")
                new_state = {"path": str(path), "existed": True,
                             "originalB64": base64.b64encode(current).decode("ascii")}
    else:
        if current is None:
            new_state = {"path": str(path), "existed": False, "createdDir": not path.parent.exists()}
        else:
            if _parse_object(current) is None:
                log(f"[!] Resource saver: {SETTINGS_NAME} is not a valid JSON object; left untouched.")
                return ApplyResult("warning", f"{SETTINGS_NAME} is malformed or not a JSON object; left untouched")
            new_state = {"path": str(path), "existed": True,
                         "originalB64": base64.b64encode(current).decode("ascii")}

    original = _original_bytes(new_state)
    base = _parse_object(original) if original is not None else {}
    if base is None:  # cannot happen (checked above), but never write over something unparseable
        return ApplyResult("warning", f"{SETTINGS_NAME} is malformed; left untouched")
    merged = dict(base)
    merged.update(flags)
    data = (json.dumps(merged, indent=2) + "\n").encode("utf-8")
    sha = _sha(data)

    if current is not None and current == data and state is not None and state.get("writtenSha") == sha:
        return ApplyResult("unchanged", "already applied")

    # Save the bookkeeping BEFORE touching the install, so a crash can never lose the original.
    new_state["writtenSha"] = sha
    try:
        write_json_atomic(state_path, new_state)
        _write_atomic(path, data)
    except OSError as exc:
        log(f"[!] Resource saver: could not write the settings ({exc}).")
        return ApplyResult("error", str(exc))
    log(f"[i] Resource saver: applied {', '.join(f'{k}={v}' for k, v in flags.items())}.")
    return ApplyResult("applied", "applied")


# ---------------------------------------------------------------------------
# Cheap, throttled, idempotent: call it before every launch
# ---------------------------------------------------------------------------
_last: dict = {}


def ensure_applied(profile, *, settings_path=None, state_path=None, version_dir=None, roots=None,
                   log_fn=None, clock=time.monotonic, min_interval: float = 5.0) -> ApplyResult:
    """Applies `profile` unless it was applied moments ago. Resolves the newest Roblox folder
    itself when no path is given. Never raises."""
    log = log_fn or _noop
    try:
        if settings_path is None:
            vdir = version_dir or current_version_dir(roots)
            if vdir is None:
                return ApplyResult("skipped", "Roblox install not found")
            settings_path = _settings_path_for(vdir)
        sp = Path(settings_path)
        stp = Path(state_path) if state_path is not None else STATE_FILE
        key = (profile if isinstance(profile, str) else json.dumps(profile, sort_keys=True), str(sp), str(stp))
        now = clock()
        prev = _last.get(key)
        if prev is not None and now - prev[0] < min_interval and prev[1].ok:
            return prev[1]
        result = apply_profile(profile, settings_path=sp, state_path=stp, log_fn=log)
        _last[key] = (now, result)
        return result
    except Exception as exc:  # a launch must never fail because of this
        log(f"[!] Resource saver: unexpected error ({exc!r}); launching without it.")
        return ApplyResult("error", repr(exc))


def reset_throttle() -> None:
    _last.clear()


# ---------------------------------------------------------------------------
# Status (GUI label, diagnostics)
# ---------------------------------------------------------------------------
def status(*, version_dir=None, roots=None, state_path=None) -> dict:
    """{'versionDir', 'settingsPath', 'fileExists', 'valid', 'backup', 'activeFlags',
        'matches': name of the profile we wrote, or None, 'staleBackup'}"""
    vdir = version_dir or current_version_dir(roots)
    stp = Path(state_path) if state_path is not None else STATE_FILE
    state = _load_state(stp)
    info = {"versionDir": vdir, "settingsPath": None, "fileExists": False, "valid": None,
            "backup": state is not None, "activeFlags": {}, "matches": None, "staleBackup": False}
    if vdir is None:
        return info
    path = settings_path(vdir)
    info["settingsPath"] = path
    data = _read_bytes(path)
    info["fileExists"] = data is not None
    if data is not None:
        obj = _parse_object(data)
        info["valid"] = obj is not None
        if obj is not None:
            info["activeFlags"] = obj
    if state is not None and Path(state["path"]) != path:
        info["staleBackup"] = True
    ours = state is not None and data is not None and _sha(data) == state.get("writtenSha")
    if ours and info["valid"]:
        for name in PROFILE_ORDER:
            flags = PROFILES[name]
            if flags and all(info["activeFlags"].get(k) == v for k, v in flags.items()):
                info["matches"] = name
    return info


def describe(info: dict) -> str:
    if info["versionDir"] is None:
        return "Roblox install not found"
    if info["fileExists"] and info["valid"] is False:
        return "ClientAppSettings.json is not valid JSON (left untouched)"
    if info["matches"]:
        return f"Active profile: {info['matches']}"
    if info["backup"]:
        return "Active: custom flags (a backup of the original is kept)"
    return "No profile applied (original settings)"


def _main(argv: list[str]) -> int:
    cmd = argv[1].lower() if len(argv) > 1 else "status"
    vdir = current_version_dir()
    if vdir is None:
        print("Roblox install (Versions/<ver>/RobloxPlayerBeta.exe) not found.")
        return 1
    sp = settings_path(vdir)
    if cmd == "status":
        print(describe(status(version_dir=vdir)))
        print(f"File: {sp}")
        return 0
    if cmd == "apply" and len(argv) > 2 and argv[2].title() in PROFILES:
        result = apply_profile(argv[2].title(), settings_path=sp, state_path=STATE_FILE, log_fn=print)
        print(result.status, result.detail)
        return 0 if result.ok else 1
    if cmd == "restore":
        print("restored" if restore(settings_path=sp, state_path=STATE_FILE, log_fn=print) else "nothing to restore")
        return 0
    print(__doc__.split("Command line:")[1].strip())
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))