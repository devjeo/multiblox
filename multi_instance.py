"""
Phase 1: Roblox multi-instance proof of concept (Windows only).

While this program is running, it holds the named Win32 mutex that the Roblox
client checks at startup, so additional clients are allowed to launch. A
background watcher logs every Roblox process that starts or closes, to the
console and to multi_instance.log (in %APPDATA%\RobloxAccountManager).

Usage:
    python multi_instance.py                # asks before closing running Roblox clients
    python multi_instance.py --force        # closes running Roblox clients without asking
    python multi_instance.py --with-event   # also hold ROBLOX_singletonEvent (testing only;
                                            # may cause the older client to close)
    python multi_instance.py --no-close-event   # do NOT close the singleton event handle
                                                # inside running Roblox clients

Commands (type in the console):
    on      start holding the singleton objects
    off     release them (new clients will be blocked again)
    status  show state and running Roblox processes
    kill    close all running Roblox clients
    quit    release everything and exit

No third-party packages needed (ctypes + built-in taskkill only).
"""

import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

from account_store import DATA_DIR
from safelog import redact

if sys.platform != "win32":
    sys.exit("This tool only works on Windows.")

ERROR_ALREADY_EXISTS = 183
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE = ctypes.c_void_p(-1).value
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_DUP_HANDLE = 0x0040
DUPLICATE_CLOSE_SOURCE = 0x1
DUPLICATE_SAME_ACCESS = 0x2
STILL_ACTIVE = 259

LOG_PATH = DATA_DIR / "multi_instance.log"   # under %APPDATA%, not next to the script (breaks in a PyInstaller exe)
LOG_MAX_BYTES = 1_000_000  # rotated to multi_instance.log.1 beyond this, so it never grows forever

# Names Roblox has used for its single-instance check. Kept in lists because
# they can change between Roblox versions.
MUTEX_NAMES = ["ROBLOX_singletonMutex"]
EVENT_NAMES = ["ROBLOX_singletonEvent"]


def set_singleton_names(mutexes: list[str], events: list[str]) -> None:
    """Override the names at runtime (Settings). Empty lists keep the current names.
    A holder that is already ON keeps its old handles until it is switched off and on."""
    if mutexes:
        MUTEX_NAMES[:] = mutexes
    if events:
        EVENT_NAMES[:] = events

# Game clients: must NOT be running when we take the singleton objects.
ROBLOX_PROCESS_NAMES = {"robloxplayerbeta.exe"}
# Everything the watcher reports on (informational only).
WATCHED_PROCESS_NAMES = {
    "robloxplayerbeta.exe",
    "robloxplayerlauncher.exe",
    "robloxcrashhandler.exe",
}

# If a game client closes within this many seconds of another one starting,
# the log flags it as probably replaced by the new launch.
REPLACED_WINDOW_SECONDS = 10.0

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
_log_lock = threading.Lock()


def log(message: str) -> None:
    """Print with a timestamp and append to the log file (secrets redacted, size capped)."""
    message = redact(message)
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    with _log_lock:
        print(line, flush=True)
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            try:
                if LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                    os.replace(LOG_PATH, LOG_PATH.with_name(LOG_PATH.name + ".1"))
            except OSError:
                pass  # no file yet, or it is locked: just keep appending
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
        except OSError:
            pass


# --------------------------------------------------------------------------
# Process helpers
# --------------------------------------------------------------------------
def list_processes(names: set[str]) -> list[tuple[int, int, str]]:
    """Returns [(pid, parent_pid, exe_name), ...] for processes whose exe name
    (lowercase) is in `names`."""
    found = []
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, INVALID_HANDLE):
        return found
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in names:
                found.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return found


def find_roblox_processes() -> list[tuple[int, str]]:
    """Returns [(pid, exe_name), ...] for running Roblox game clients."""
    return [(pid, name) for pid, _ppid, name in list_processes(ROBLOX_PROCESS_NAMES)]


def kill_roblox_processes() -> bool:
    """Force-closes every Roblox client (and its child processes).
    Returns True if none remain afterwards."""
    # Several rounds: a client that is mid-teleport can start a new process while we kill the old one.
    for _round in range(5):
        procs = find_roblox_processes()
        if not procs:
            return True
        for pid, _name in procs:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        for _ in range(12):
            if not find_roblox_processes():
                break
            time.sleep(0.25)
    return not find_roblox_processes()


def ask_yes_no(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# --------------------------------------------------------------------------
# Closing the singleton event handle inside a running Roblox client
# (needs 64-bit Python; run as administrator if access is denied)
# --------------------------------------------------------------------------
ntdll = ctypes.WinDLL("ntdll")
ntdll.NtQuerySystemInformation.argtypes = [
    wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
ntdll.NtQuerySystemInformation.restype = ctypes.c_long
ntdll.NtQueryObject.argtypes = [
    wintypes.HANDLE, wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
ntdll.NtQueryObject.restype = ctypes.c_long
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.DuplicateHandle.argtypes = [
    wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE),
    wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.DuplicateHandle.restype = wintypes.BOOL


class HANDLE_ENTRY_EX(ctypes.Structure):
    _fields_ = [
        ("Object", ctypes.c_void_p),
        ("UniqueProcessId", ctypes.c_size_t),
        ("HandleValue", ctypes.c_size_t),
        ("GrantedAccess", wintypes.ULONG),
        ("CreatorBackTraceIndex", wintypes.USHORT),
        ("ObjectTypeIndex", wintypes.USHORT),
        ("HandleAttributes", wintypes.ULONG),
        ("Reserved", wintypes.ULONG),
    ]


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p)]


_EVENT_TYPE_INDEX = None


def _snapshot_handles():
    """Returns an array of HANDLE_ENTRY_EX for every handle on the system."""
    size = 1 << 24
    for _ in range(8):
        buf = ctypes.create_string_buffer(size)
        ret = wintypes.ULONG(0)
        status = ntdll.NtQuerySystemInformation(64, buf, size, ctypes.byref(ret))  # SystemExtendedHandleInformation
        if (status & 0xFFFFFFFF) == 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
            size = max(size * 2, ret.value + (1 << 20))
            continue
        if status < 0:
            raise OSError(f"NtQuerySystemInformation failed: 0x{status & 0xFFFFFFFF:08X}")
        count = ctypes.c_size_t.from_buffer(buf).value
        header = 2 * ctypes.sizeof(ctypes.c_size_t)
        return (HANDLE_ENTRY_EX * count).from_buffer(buf, header)
    raise OSError("Handle table kept growing; giving up.")


def _event_type_index() -> int:
    """Finds which ObjectTypeIndex means 'Event' by creating one of our own."""
    global _EVENT_TYPE_INDEX
    if _EVENT_TYPE_INDEX is not None:
        return _EVENT_TYPE_INDEX
    h = kernel32.CreateEventW(None, False, False, None)
    try:
        me = os.getpid()
        for e in _snapshot_handles():
            if e.UniqueProcessId == me and e.HandleValue == h:
                _EVENT_TYPE_INDEX = e.ObjectTypeIndex
                return _EVENT_TYPE_INDEX
    finally:
        kernel32.CloseHandle(h)
    raise OSError("Could not determine the Event object type index.")


def _object_name(handle) -> str | None:
    size = 1024
    for _ in range(4):
        buf = ctypes.create_string_buffer(size)
        ret = wintypes.ULONG(0)
        status = ntdll.NtQueryObject(handle, 1, buf, size, ctypes.byref(ret))  # ObjectNameInformation
        if (status & 0xFFFFFFFF) in (0xC0000004, 0xC0000023, 0x80000005):
            size = max(size * 2, ret.value + 16)
            continue
        if status < 0:
            return None
        us = UNICODE_STRING.from_buffer(buf)
        if not us.Length or not us.Buffer:
            return ""
        return ctypes.wstring_at(us.Buffer, us.Length // 2)
    return None


_MUTANT_TYPE_INDEX = None


def _mutant_type_index() -> int:
    """Finds which ObjectTypeIndex means 'Mutant' (a mutex) by creating one of our own."""
    global _MUTANT_TYPE_INDEX
    if _MUTANT_TYPE_INDEX is not None:
        return _MUTANT_TYPE_INDEX
    h = kernel32.CreateMutexW(None, False, None)
    try:
        me = os.getpid()
        for e in _snapshot_handles():
            if e.UniqueProcessId == me and e.HandleValue == h:
                _MUTANT_TYPE_INDEX = e.ObjectTypeIndex
                return _MUTANT_TYPE_INDEX
    finally:
        kernel32.CloseHandle(h)
    raise OSError("Could not determine the Mutant object type index.")


def discover_singleton_names(pid: int) -> tuple[list[tuple[str, str]], str | None]:
    """READ-ONLY. Lists the named Event / Mutex handles inside process `pid` whose name
    contains 'roblox'. Used by diagnostics to spot a Roblox update that renamed them.
    Returns ([("event"|"mutex", full_object_name), ...], error_or_None)."""
    kinds = {_event_type_index(): "event", _mutant_type_index(): "mutex"}
    hproc = kernel32.OpenProcess(PROCESS_DUP_HANDLE, False, pid)
    if not hproc:
        return [], (f"could not open PID {pid} (error {ctypes.get_last_error()}). "
                    "Try running as administrator.")
    found: list[tuple[str, str]] = []
    try:
        me = kernel32.GetCurrentProcess()
        for e in _snapshot_handles():
            if e.UniqueProcessId != pid or e.ObjectTypeIndex not in kinds:
                continue
            dup = wintypes.HANDLE()
            if not kernel32.DuplicateHandle(hproc, e.HandleValue, me, ctypes.byref(dup),
                                            0, False, DUPLICATE_SAME_ACCESS):
                continue
            try:
                name = _object_name(dup)
            finally:
                kernel32.CloseHandle(dup)
            if name and "roblox" in name.lower():
                found.append((kinds[e.ObjectTypeIndex], name))
    finally:
        kernel32.CloseHandle(hproc)
    return found, None


def close_singleton_event(pid: int) -> tuple[int, str | None]:
    """Closes the ROBLOX_singletonEvent handle(s) inside process `pid`.
    Returns (handles_closed, error_message_or_None)."""
    suffixes = tuple(n.lower() for n in EVENT_NAMES)
    event_type = _event_type_index()
    hproc = kernel32.OpenProcess(PROCESS_DUP_HANDLE, False, pid)
    if not hproc:
        return 0, (f"could not open PID {pid} for handle access "
                   f"(error {ctypes.get_last_error()}). Try running as administrator.")
    closed = 0
    try:
        me = kernel32.GetCurrentProcess()
        for e in _snapshot_handles():
            if e.UniqueProcessId != pid or e.ObjectTypeIndex != event_type:
                continue
            dup = wintypes.HANDLE()
            if not kernel32.DuplicateHandle(hproc, e.HandleValue, me, ctypes.byref(dup),
                                            0, False, DUPLICATE_SAME_ACCESS):
                continue
            try:
                name = _object_name(dup)
            finally:
                kernel32.CloseHandle(dup)
            if name and name.lower().endswith(suffixes):
                out = wintypes.HANDLE()
                if kernel32.DuplicateHandle(hproc, e.HandleValue, me, ctypes.byref(out), 0, False,
                                            DUPLICATE_CLOSE_SOURCE | DUPLICATE_SAME_ACCESS):
                    kernel32.CloseHandle(out)
                    closed += 1
    finally:
        kernel32.CloseHandle(hproc)
    return closed, None


def make_event_closer(holder):
    """Callback run in its own thread whenever a new game client starts."""
    def handler(pid: int) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not holder.active:
                return
            try:
                closed, error = close_singleton_event(pid)
            except Exception as exc:
                log(f"        [event] error while closing singleton event in PID {pid}: {exc!r}")
                return
            if error:
                log(f"        [event] {error}")
                return
            if closed:
                log(f"        [event] closed {closed} ROBLOX_singletonEvent handle(s) inside PID {pid}")
                return
            time.sleep(1.0)  # the client may not have created the event yet
        log(f"        [event] ROBLOX_singletonEvent not found in PID {pid} within 20s")
    return handler


# --------------------------------------------------------------------------
# Process watcher (logs starts / closes)
# --------------------------------------------------------------------------
class ProcessWatcher(threading.Thread):
    """Polls for Roblox processes and logs when they start and close."""

    def __init__(self, holder_state, on_game_start=None, poll_seconds: float = 0.4):
        super().__init__(daemon=True)
        self._on_game_start = on_game_start  # callable(pid), run in its own thread
        self._holder_state = holder_state  # callable -> str describing mutex state
        self._poll = poll_seconds
        self._stop_event = threading.Event()
        # pid -> {"name", "start" (monotonic), "handle"}
        self._tracked: dict[int, dict] = {}
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._stop_event.set()

    def snapshot(self) -> list[tuple[int, str, float]]:
        """[(pid, name, uptime_seconds)] of currently tracked processes."""
        now = time.monotonic()
        with self._lock:
            return [(pid, i["name"], now - i["start"]) for pid, i in self._tracked.items()]

    def _game_clients(self) -> list[int]:
        return [p for p, i in self._tracked.items() if i["name"].lower() in ROBLOX_PROCESS_NAMES]

    def run(self) -> None:
        first_pass = True
        while not self._stop_event.is_set():
            try:
                self._tick(first_pass)
            except Exception as exc:  # never let the watcher die silently
                log(f"[watcher] error: {exc!r}")
            first_pass = False
            self._stop_event.wait(self._poll)

    def _tick(self, first_pass: bool) -> None:
        current = {pid: (ppid, name) for pid, ppid, name in list_processes(WATCHED_PROCESS_NAMES)}
        now = time.monotonic()

        with self._lock:
            # ---- new processes
            for pid, (ppid, name) in current.items():
                if pid in self._tracked:
                    continue
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                self._tracked[pid] = {"name": name, "start": now, "handle": handle}
                is_game = name.lower() in ROBLOX_PROCESS_NAMES
                if first_pass:
                    log(f"FOUND   {name} PID {pid} already running when watcher started")
                else:
                    extra = f" | game clients now: {len(self._game_clients())}" if is_game else ""
                    log(f"STARTED {name} PID {pid} (parent PID {ppid}){extra}")
                    if is_game:
                        log(f"        multi-instance: {self._holder_state()}")
                        if self._on_game_start:
                            threading.Thread(target=self._on_game_start, args=(pid,), daemon=True).start()

            # ---- closed processes
            for pid in [p for p in self._tracked if p not in current]:
                info = self._tracked.pop(pid)
                name = info["name"]
                lived = now - info["start"]
                code_text = "unknown"
                if info["handle"]:
                    code = wintypes.DWORD(0)
                    if kernel32.GetExitCodeProcess(info["handle"], ctypes.byref(code)):
                        code_text = "still active?" if code.value == STILL_ACTIVE else str(code.value)
                    kernel32.CloseHandle(info["handle"])

                log(f"CLOSED  {name} PID {pid} after {lived:.1f}s, exit code {code_text}")

                if name.lower() in ROBLOX_PROCESS_NAMES:
                    newer = [
                        p for p in self._game_clients()
                        if now - self._tracked[p]["start"] <= REPLACED_WINDOW_SECONDS
                    ]
                    if newer:
                        log(
                            f"        NOTE: closed within {REPLACED_WINDOW_SECONDS:.0f}s of another client "
                            f"starting (PID {', '.join(map(str, newer))}) - likely replaced by the new launch."
                        )
                    log(f"        game clients remaining: {len(self._game_clients())}")


# --------------------------------------------------------------------------
# Singleton holder
# --------------------------------------------------------------------------
class SingletonHolder:
    """Creates and holds the named objects so Roblox sees them as 'taken'."""

    def __init__(self, force: bool = False, hold_events: bool = False):
        self.force = force
        # Holding the event can make a new client signal the old one to quit,
        # so by default we only hold the mutex.
        self.hold_events = hold_events
        self._handles = []  # list of (kind, name, handle)

    @property
    def active(self) -> bool:
        return bool(self._handles)

    def _clear_running_clients(self) -> bool:
        """Make sure no Roblox client already owns the singleton objects."""
        running = find_roblox_processes()
        if not running:
            return True

        listing = ", ".join(f"{name} (PID {pid})" for pid, name in running)
        log(f"[!] Roblox is already running: {listing}")
        print("    A running client already owns the singleton objects, so a")
        print("    second client would just replace it. It must be closed first.")
        print("    (It may be running in the background with no visible window.)")

        if not self.force and not ask_yes_no("    Close it now? Any open game will be disconnected."):
            log("    Not closed. Multi-instance NOT enabled.")
            return False

        if kill_roblox_processes():
            log("[ok] Roblox clients closed.")
            return True

        log("[!] Could not close all Roblox processes. If Roblox runs as")
        print("    administrator, re-run this script as administrator.")
        return False

    def enable(self, trust_running: bool = False) -> bool:
        """trust_running=True skips the "close running clients" step. Only use it when every
        running client is one we launched ourselves (e.g. re-attached after an app restart):
        they already share the singleton objects, so there is nothing to clean up."""
        if self.active:
            log("[!] Already enabled.")
            return True

        if trust_running:
            log("[ok] Keeping the Roblox clients that are already running (launched by this tool).")
        elif not self._clear_running_clients():
            return False

        problems = []
        for kind, names, create in (
            ("mutex", MUTEX_NAMES, lambda n: kernel32.CreateMutexW(None, False, n)),
            ("event", EVENT_NAMES if self.hold_events else [],
             lambda n: kernel32.CreateEventW(None, False, False, n)),
        ):
            for name in names:
                handle = create(name)
                err = ctypes.get_last_error()
                if not handle:
                    problems.append(f"Could not create {kind} '{name}' (error {err}).")
                    continue
                if err == ERROR_ALREADY_EXISTS:
                    problems.append(
                        f"{kind} '{name}' was already owned by another program, "
                        "so we do not own it exclusively."
                    )
                self._handles.append((kind, name, handle))

        for p in problems:
            log(f"[!] {p}")
        if not self._handles:
            return False
        log(f"[ok] Multi-instance {self.describe()}")
        return True

    def disable(self) -> None:
        was_active = self.active
        for _kind, _name, handle in self._handles:
            kernel32.CloseHandle(handle)
        self._handles.clear()
        if was_active:
            log("[ok] Multi-instance OFF - released singleton objects.")

    def describe(self) -> str:
        if not self.active:
            return "OFF - Roblox will block a second instance."
        held = ", ".join(f"{kind}:{name}" for kind, name, _ in self._handles)
        return f"ON - holding {held}"


# --------------------------------------------------------------------------
# Console UI
# --------------------------------------------------------------------------
def print_status(holder: SingletonHolder, watcher: ProcessWatcher) -> None:
    print(f"[status] {holder.describe()}")
    procs = watcher.snapshot()
    games = [p for p in procs if p[1].lower() in ROBLOX_PROCESS_NAMES]
    print(f"[status] Roblox game clients running: {len(games)}")
    for pid, name, uptime in sorted(procs, key=lambda p: p[2], reverse=True):
        print(f"         {name} PID {pid} - up {uptime:.0f}s")
    print(f"[status] Log file: {LOG_PATH}")


def main() -> None:
    force = "--force" in sys.argv[1:]
    hold_events = "--with-event" in sys.argv[1:]
    holder = SingletonHolder(force=force, hold_events=hold_events)
    print(__doc__)
    log("=== multi_instance started ===")

    holder.enable()

    close_event = "--no-close-event" not in sys.argv[1:]
    watcher = ProcessWatcher(
        holder_state=holder.describe,
        on_game_start=make_event_closer(holder) if close_event else None,
    )
    watcher.start()

    if holder.active:
        print(f"Launch your Roblox clients now. Logging to {LOG_PATH}")
        print("Type 'quit' to exit.\n")
    else:
        print("Multi-instance is OFF. Type 'on' to try again.\n")

    try:
        while True:
            try:
                cmd = input("> ").strip().lower()
            except EOFError:
                break
            if cmd == "on":
                holder.enable()
            elif cmd == "off":
                holder.disable()
            elif cmd == "status":
                print_status(holder, watcher)
            elif cmd == "kill":
                if holder.force or ask_yes_no("Close ALL running Roblox clients?"):
                    log("[ok] Closed." if kill_roblox_processes() else "[!] Some processes remain.")
            elif cmd in ("quit", "exit", "q"):
                break
            elif cmd:
                print("Commands: on, off, status, kill, quit")
    except KeyboardInterrupt:
        pass
    finally:
        watcher.stop()
        holder.disable()
        log("=== multi_instance stopped ===")


if __name__ == "__main__":
    main()