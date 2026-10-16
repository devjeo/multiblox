"""
Phase 5: desktop window for the Roblox Account Manager (Windows only).

One window for everything the console tools do:
  - Account list with live status (running / rejoining / gave up ...), uptime,
    last game and a per-account auto-rejoin switch. Select several rows to act
    on them together (Ctrl/Shift-click, Ctrl+A for all).
  - Multi-instance ON/OFF switch (holds the mutex + closes the event handle).
  - Game picker (saved places from places.py) + optional server (job) ID.
  - Launch Selected / Join All / Stop / Resume / Auto-rejoin toggles.
  - Settings (rejoin cooldown, retry limits, polling), saved to settings.json.
  - Live log panel (everything multi_instance / autorejoin print shows up here).
  - Optional system tray (needs:  pip install pystray pillow). Without it the
    window simply closes/quits normally.
  - Restart recovery: clients that survive an app restart are re-attached, and
    after a crash accounts with auto-rejoin ON are relaunched (see sessions.py).
  - Diagnostics window (diagnostics.py): checks Roblox, the network, cookies and
    multi-instance, with a redacted report you can copy.

Run (as administrator, for the event-handle step):
    python gui.py

Keep this file in the same folder as account_store.py, multi_instance.py,
autorejoin.py, launcher.py and places.py. Do NOT run launcher.py or
multi_instance.py at the same time. Standard library only (tkinter).
"""

from __future__ import annotations

import ctypes
import io
import json
import os
import queue
import sys
import threading
import time
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

if sys.platform != "win32":
    sys.exit("This tool only works on Windows.")

import autorejoin
import diagnostics
import groups
import multi_instance
import notify_discord
import resourcesaver
import scheduler
import winapi
import winlayout
import secrets_store
from account_store import DATA_DIR, AccountStore, CookieError
from autorejoin import RejoinManager
from launcher import LaunchError, _running_pids, kill_pid, launch_account
from multi_instance import (
    ProcessWatcher,
    SingletonHolder,
    find_roblox_processes,
    log as mi_log,
    make_event_closer,
)
from notifier import EVENT_LABELS, Notifier
from notify_discord import DiscordChannel
from notify_tray import TrayChannel
from places import PlaceStore, fetch_place_name, parse_place_input
from sessions import SessionStore, restore as restore_sessions
from settings import DEFAULTS, Settings

APP_TITLE = "Roblox Account Manager"
PERF_WARN_CLIENTS = 5   # warn before this many clients run at once
LOG_MAX_LINES = 1000

STATE_LABELS = {
    "starting": ("Starting...", "busy"),
    "running": ("Running", "ok"),
    "rejoining": ("Rejoining...", "busy"),
    "waiting_retry": ("Retrying soon", "busy"),
    "gave_up": ("Gave up - use Resume", "bad"),
    "closed": ("Closed", "idle"),
}


# --------------------------------------------------------------------------
# stdout/stderr -> log panel (also keeps working under pythonw, where they are None)
# --------------------------------------------------------------------------
class _LogStream(io.TextIOBase):
    def __init__(self, q: queue.Queue, original=None) -> None:
        self.q, self.original = q, original
        self._buf = ""
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if self.original:
            try:
                self.original.write(s)
            except Exception:
                pass
        with self._lock:
            self._buf += s
            *lines, self._buf = self._buf.split("\n")
        for line in lines:
            if line.strip():
                self.q.put(line.rstrip())
        return len(s)

    def flush(self) -> None:
        if self.original:
            try:
                self.original.flush()
            except Exception:
                pass


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _fix_tk_tag_colors(style: ttk.Style) -> None:
    """Tk 8.6.9 ignores Treeview row tag colours unless this is applied."""
    def fixed(option):
        return [e for e in style.map("Treeview", query_opt=option) if e[:2] != ("!disabled", "!selected")]
    style.map("Treeview", foreground=fixed("foreground"), background=fixed("background"))


# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.ui_q: queue.Queue = queue.Queue()   # callables to run on the Tk thread
        self.log_q: queue.Queue = queue.Queue()  # log lines
        sys.stdout = _LogStream(self.log_q, sys.stdout)
        sys.stderr = _LogStream(self.log_q, sys.stderr)

        threading.excepthook = lambda a: mi_log(
            f"[!] Background error in {getattr(a.thread, 'name', '?')}: {a.exc_value!r}")
        self.settings = Settings()
        self.store = AccountStore()
        self.places = PlaceStore()
        self.launching: dict[str, str] = {}  # account id -> "Queued..." / "Launching..."
        self.tray = None
        self._closing = False
        self._winapi = None            # built on first use (window layout)
        self._autotiler = None
        self.last_tile: dict | None = None

        # Notifications. Built BEFORE the rejoin manager, which reports events to it. The tray
        # channel looks the icon up lazily because the tray is created later.
        self.notifier = Notifier(
            clock=time.monotonic, channels=self._notify_channels(),
            events=self.settings["notifyEvents"], cooldown_sec=self.settings["notifyCooldownSeconds"],
            log_fn=mi_log)
        self.notifier.start()

        # ---- backend (same wiring as launcher.main)
        self.holder = SingletonHolder()
        self.watcher = ProcessWatcher(
            holder_state=self.holder.describe, on_game_start=make_event_closer(self.holder))
        self.watcher.start()
        self.manager = RejoinManager(
            store=self.store,
            launch_fn=self._launch_fn,
            list_pids_fn=_running_pids,
            kill_fn=kill_pid,
            log_fn=mi_log,
            notify_fn=lambda event, account, detail: self.notifier.notify(event, account, detail),
            on_ready_fn=self._on_client_ready,
        )
        self.manager.start()

        # Scheduler (rules in schedules.json). It starts ticking after session restore (see _restore).
        self.rule_store = scheduler.RuleStore()
        self.scheduler = scheduler.Scheduler(
            self.rule_store.load(), self.rule_store, self.manager, self._launch_worker, datetime.now, mi_log,
            lambda: self.settings["scheduleMissedWindowMinutes"],
            group_games_fn=lambda: self.settings["groupGames"],
            multi_instance_fn=lambda: self.holder.active,
            enabled_fn=lambda: self.settings["schedulerEnabled"] and not self._closing)
        for problem in self.rule_store.problems:
            mi_log(f"[!] Schedule: {problem}.")

        # Crash/restart recovery. Records are loaded now, restored after multi-instance is on
        # (see startup). Nothing is written to sessions.json until that has happened.
        self.sessions = SessionStore()
        self._boot_records, self._boot_clean = (
            self.sessions.load() if self.settings["restoreSessions"] else ([], True))
        self._restored = False

        # Private server "armed" from Find server... > Private server. Memory only, never saved.
        self.launch_extra: dict | None = None
        self.launch_extra_place: int | None = None
        self._arming = False

        self._build_ui()
        self._setup_tray()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = lambda *exc: mi_log(f"[!] UI error: {exc[1]!r}")

        self.drain()
        self.refresh()
        root.after(300, self.startup)

    # ---- helpers --------------------------------------------------------
    def _launch_fn(self, account, place_id, job_id, extra=None):
        """The RejoinManager's launch function. Flags are read when a client starts, so the resource
        profile is put in place first; manual launches, rejoins and scheduled launches all pass here."""
        self._pre_launch()
        return launch_account(self.store, account, place_id, job_id, extra=extra)

    # ---- window layout --------------------------------------------------
    def winapi(self):
        """The Win32 window API, or None if it cannot be built."""
        if self._winapi is None:
            try:
                self._winapi = winapi.real_api()
            except Exception as exc:
                mi_log(f"[layout] window control is unavailable: {exc!r}")
        return self._winapi

    def _client_windows(self):
        """(api, window handles of running clients in account-list order, number of running clients)."""
        api = self.winapi()
        if api is None:
            return None, [], 0
        with self.manager._lock:
            pid_by_account = {aid: c.pid for aid, c in self.manager.clients.items()
                              if c.pid and c.state == "running"}
        hwnd_by_pid = api.enum_windows_for_pids(set(pid_by_account.values()))
        return api, winlayout.order_hwnds(self.store.accounts, pid_by_account, hwnd_by_pid), len(pid_by_account)

    def _tile_job(self) -> str:
        """Tiles the running clients. Runs on a worker thread; returns a status line."""
        api, hwnds, clients = self._client_windows()
        if api is None:
            return "Window control is unavailable."
        moved = winlayout.tile(api, hwnds, self.settings["layoutMonitor"], self.settings["layoutColumns"],
                               gap=self.settings["layoutGap"], log_fn=mi_log)
        self.last_tile = {"clients": clients, "matched": len(hwnds), "moved": moved, "at": time.time()}
        text = f"Tiled {moved} of {clients} client window(s)."
        mi_log(f"[layout] {text}")
        return text

    def tile_windows(self) -> None:
        self.run_bg(lambda: self.set_status(self._tile_job()))

    def minimize_windows(self) -> None:
        def work() -> None:
            api, hwnds, _ = self._client_windows()
            if api is not None:
                self.set_status(f"Minimized {winlayout.minimize_all(api, hwnds)} client window(s).")
        self.run_bg(work)

    def restore_windows(self) -> None:
        def work() -> None:
            api, hwnds, _ = self._client_windows()
            if api is not None:
                self.set_status(f"Restored {winlayout.restore_all(api, hwnds)} client window(s).")
        self.run_bg(work)

    def _on_client_ready(self, client) -> None:
        """RejoinManager callback (manager thread): a client's process is being tracked. Launches,
        rejoins and scheduled launches all come through here."""
        if not self.settings["layoutAutoTile"] or not client.pid or self._closing:
            return
        api = self.winapi()
        if api is None:
            return
        if self._autotiler is None:
            self._autotiler = winlayout.AutoTiler(
                api, self._tile_job, enabled_fn=lambda: self.settings["layoutAutoTile"] and not self._closing,
                log_fn=mi_log)
        self.run_bg(self._autotiler.on_ready, client.pid)

    def _pre_launch(self) -> None:
        if not self.settings["applyProfileBeforeLaunch"] or self.settings["performanceProfile"] == "Normal":
            return
        resourcesaver.ensure_applied(self.settings["performanceProfile"], log_fn=mi_log)  # never raises

    def ui(self, fn) -> None:
        """Run `fn` on the Tk thread (safe to call from any thread)."""
        self.ui_q.put(fn)

    def run_bg(self, fn, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True).start()

    def set_status(self, text: str) -> None:
        self.ui(lambda: self.status_var.set(text))

    def client_snapshot(self) -> dict:
        with self.manager._lock:
            return {aid: (c.state, c.pid, c.started, len(c.attempts))
                    for aid, c in self.manager.clients.items()}

    def _notify_channels(self) -> dict:
        """The channels the current settings turn on (the Discord URL is read at send time)."""
        channels: dict = {}
        if self.settings["notifyDiscord"]:
            channels["discord"] = DiscordChannel()
        if self.settings["notifyTray"]:
            channels["tray"] = TrayChannel(icon_fn=lambda: self.tray)
        return channels

    def apply_notify_settings(self) -> None:
        self.notifier.reconfigure(self._notify_channels(), self.settings["notifyEvents"],
                                  self.settings["notifyCooldownSeconds"])

    def selected_accounts(self) -> list:
        chosen = set(self.tree.selection())
        return [a for a in self.store.accounts if a.id in chosen]

    # ---- UI construction ------------------------------------------------
    def _build_ui(self) -> None:
        root = self.root
        root.title(APP_TITLE)
        root.geometry("1100x660")
        root.minsize(900, 520)

        style = ttk.Style()
        _fix_tk_tag_colors(style)
        style.configure("On.TButton", foreground="#0a7d2c")
        style.configure("Off.TButton", foreground="#b00020")
        style.configure("Treeview", rowheight=26)

        # top bar
        top = ttk.Frame(root, padding=(10, 8))
        top.pack(fill="x")
        self.mi_btn = ttk.Button(top, width=22, command=self.toggle_multi)
        self.mi_btn.pack(side="left")
        self.ar_btn = ttk.Button(top, width=20, command=self.toggle_all_rejoin)
        self.ar_btn.pack(side="left", padx=6)
        ttk.Button(top, text="Settings...", command=self.open_settings).pack(side="right")
        ttk.Button(top, text="Schedules...", command=self.open_schedules).pack(side="right", padx=6)
        ttk.Button(top, text="Diagnostics...", command=self.open_diagnostics).pack(side="right", padx=6)
        if not _is_admin():
            ttk.Label(top, foreground="#b36b00",
                      text="Not running as administrator - closing the singleton event may fail."
                      ).pack(side="right", padx=10)

        body = ttk.Frame(root, padding=(10, 0))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        # accounts (left)
        left = ttk.LabelFrame(body, text="Accounts", padding=6)
        left.grid(row=0, column=0, sticky="nsew")
        self.group_filter = tk.StringVar(value=groups.ALL)
        self._group_values: tuple = ()
        bar = ttk.Frame(left)
        bar.pack(side="top", fill="x", pady=(0, 4))
        ttk.Label(bar, text="Group:").pack(side="left")
        self.group_cb = ttk.Combobox(bar, textvariable=self.group_filter, state="readonly", width=22,
                                     values=[groups.ALL])
        self.group_cb.pack(side="left", padx=(6, 0))
        self.group_cb.bind("<<ComboboxSelected>>", lambda e: self._render_rows())

        cols = ("group", "note", "status", "game", "rejoin", "uptime")
        self.tree = ttk.Treeview(left, columns=cols, selectmode="extended", show="tree headings")
        self.tree.heading("#0", text="Account")
        self.tree.heading("group", text="Group")
        self.tree.heading("note", text="Note")
        self.tree.heading("status", text="Status")
        self.tree.heading("game", text="Last game")
        self.tree.heading("rejoin", text="Auto-rejoin")
        self.tree.heading("uptime", text="Uptime")
        self.tree.column("#0", width=130, minwidth=90)
        self.tree.column("group", width=90, minwidth=60)
        self.tree.column("note", width=150, minwidth=60)
        self.tree.column("status", width=130, minwidth=90)
        self.tree.column("game", width=160, minwidth=80)
        self.tree.column("rejoin", width=75, minwidth=60, anchor="center")
        self.tree.column("uptime", width=65, minwidth=55, anchor="e")
        for tag, color in (("ok", "#0a7d2c"), ("busy", "#b36b00"), ("bad", "#b00020"), ("idle", "#777777")):
            self.tree.tag_configure(tag, foreground=color)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<Control-a>", lambda e: (self.tree.selection_set(self.tree.get_children()), "break")[1])

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="Launch", command=lambda: self.launch_accounts(self.selected_accounts()))
        self.menu.add_command(label="Stop", command=self.stop_selected)
        self.menu.add_command(label="Resume", command=self.resume_selected)
        self.menu.add_command(label="Toggle auto-rejoin", command=self.toggle_selected_rejoin)
        self.menu.add_separator()
        self.menu.add_command(label="Set group...", command=self.set_group_selected)
        self.menu.add_command(label="Edit note...", command=self.edit_note_selected)
        self.menu.add_command(label="Use game box as group default", command=self.set_group_default)
        self.menu.add_separator()
        self.menu.add_command(label="Remove account", command=self.remove_selected)

        # controls (right)
        right = ttk.Frame(body, padding=(10, 0, 0, 0))
        right.grid(row=0, column=1, sticky="ns")

        game = ttk.LabelFrame(right, text="Game", padding=8)
        game.pack(fill="x")
        self.place_var = tk.StringVar()
        self.place_cb = ttk.Combobox(game, textvariable=self.place_var, width=30)
        self.place_cb.pack(fill="x")
        ttk.Label(game, text="Pick a saved game, or paste a place ID / link.",
                  foreground="#777777", wraplength=240).pack(anchor="w", pady=(2, 6))
        row = ttk.Frame(game)
        row.pack(fill="x")
        ttk.Button(row, text="Save game...", command=self.add_place).pack(side="left")
        ttk.Button(row, text="Delete", command=self.remove_place).pack(side="left", padx=4)
        ttk.Label(game, text="Server (job) ID - optional:").pack(anchor="w", pady=(10, 0))
        self.job_var = tk.StringVar()
        ttk.Entry(game, textvariable=self.job_var).pack(fill="x")
        ttk.Button(game, text="Find server...", command=self.open_join_dialog).pack(anchor="w", pady=(6, 0))
        self.armed_row = ttk.Frame(game)  # shown only while a private server is armed
        ttk.Label(self.armed_row, text="Private server armed", foreground="#a15c00").pack(side="left")
        ttk.Button(self.armed_row, text="Clear", width=6, command=self.clear_private).pack(side="left", padx=6)
        self.place_var.trace_add("write", self._game_box_edited)
        self.job_var.trace_add("write", self._game_box_edited)
        self.reload_places()

        act = ttk.LabelFrame(right, text="Actions", padding=8)
        act.pack(fill="x", pady=(10, 0))
        for text, cmd in (
            ("Launch selected", lambda: self.launch_accounts(self.selected_accounts())),
            ("Join all accounts", lambda: self.launch_accounts(list(self.store.accounts))),
            ("Stop selected", self.stop_selected),
            ("Resume selected", self.resume_selected),
            ("Toggle auto-rejoin (selected)", self.toggle_selected_rejoin),
        ):
            ttk.Button(act, text=text, command=cmd).pack(fill="x", pady=2)
        self.group_btn = ttk.Button(act, text="Launch group", command=self.launch_group, state="disabled")
        self.group_btn.pack(fill="x", pady=2)
        ttk.Separator(act).pack(fill="x", pady=4)
        ttk.Button(act, text="Tile windows", command=self.tile_windows).pack(fill="x", pady=2)
        wrow = ttk.Frame(act)
        wrow.pack(fill="x", pady=2)
        ttk.Button(wrow, text="Minimize all", command=self.minimize_windows).pack(side="left", expand=True, fill="x")
        ttk.Button(wrow, text="Restore all", command=self.restore_windows).pack(side="left", expand=True, fill="x", padx=(4, 0))

        acc = ttk.LabelFrame(right, text="Manage", padding=8)
        acc.pack(fill="x", pady=(10, 0))
        ttk.Button(acc, text="Add account...", command=self.add_account).pack(fill="x", pady=2)
        ttk.Button(acc, text="Remove selected", command=self.remove_selected).pack(fill="x", pady=2)

        # log + status bar
        logf = ttk.LabelFrame(root, text="Log", padding=6)
        logf.pack(fill="both", padx=10, pady=(8, 0))
        self.log_text = ScrolledText(logf, height=9, state="disabled", wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        for tag, color in (("bad", "#b00020"), ("ok", "#0a7d2c"), ("rejoin", "#1a56b0")):
            self.log_text.tag_configure(tag, foreground=color)

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(root, textvariable=self.status_var, anchor="w", padding=(10, 4)).pack(fill="x")

    # ---- periodic work --------------------------------------------------
    def drain(self) -> None:
        try:
            while True:
                try:
                    self.ui_q.get_nowait()()
                except queue.Empty:
                    break
                except Exception as exc:
                    mi_log(f"[!] UI task error: {exc!r}")
            lines = []
            try:
                while True:
                    lines.append(self.log_q.get_nowait())
            except queue.Empty:
                pass
            if lines:
                self._append_log(lines)
        finally:
            if not self._closing:
                self.root.after(100, self.drain)

    def _append_log(self, lines: list[str]) -> None:
        t = self.log_text
        t.configure(state="normal")
        for line in lines:
            low = line.lower()
            tag = "bad" if "[!]" in line or "error" in low else "ok" if "[ok]" in line \
                else "rejoin" if "[rejoin]" in line else ""
            t.insert("end", line + "\n", tag)
        extra = int(t.index("end-1c").split(".")[0]) - LOG_MAX_LINES
        if extra > 0:
            t.delete("1.0", f"{extra + 1}.0")
        t.see("end")
        t.configure(state="disabled")

    def refresh(self) -> None:
        if self._closing:
            return
        self._render_rows()
        if self._restored:
            self.sessions.save(self.manager.session_records(), clean=False)
        self.root.after(1000, self.refresh)

    def _sync_group_filter(self) -> None:
        """Keeps the Group box in step with the groups that exist."""
        values = (groups.ALL, *groups.names(self.store.accounts))
        if values != self._group_values:
            self._group_values = values
            self.group_cb.configure(values=list(values))
        current = self.group_filter.get()
        if current != groups.ALL and not any(v.lower() == current.lower() for v in values[1:]):
            self.group_filter.set(groups.ALL)  # the last member left that group
        self.group_btn.configure(state="disabled" if self.group_filter.get() == groups.ALL else "normal")

    def _render_rows(self) -> None:
        """Redraws the account table. No timer here: refresh() schedules itself."""
        self._sync_group_filter()
        accounts = groups.filter_accounts(self.store.accounts, self.group_filter.get())
        existing = set(self.tree.get_children())
        for iid in existing - {a.id for a in accounts}:
            self.tree.delete(iid)
        snap = self.client_snapshot()
        now = self.manager.clock()
        for idx, a in enumerate(accounts):
            state, pid, started, attempts = snap.get(a.id, (None, None, 0.0, 0))
            if a.id in self.launching:
                label, tag = self.launching[a.id], "busy"
            elif state:
                label, tag = STATE_LABELS.get(state, (state, "idle"))
            else:
                label, tag = "Not running", "idle"
            uptime = ""
            if state == "running" and started:
                secs = int(now - started)
                uptime = f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
            values = (a.group, a.note, label, a.lastGameName or (a.savedPlaceId or ""),
                      "ON" if a.autoRejoinEnabled else "off", uptime)
            if a.id in existing:
                self.tree.item(a.id, text=a.username, values=values, tags=(tag,))
            else:
                self.tree.insert("", idx, iid=a.id, text=a.username, values=values, tags=(tag,))
        self.update_header()

    def update_header(self) -> None:
        on = self.holder.active
        self.mi_btn.configure(text=f"Multi-Instance: {'ON' if on else 'OFF'}",
                              style="On.TButton" if on else "Off.TButton")
        total = len(self.store.accounts)
        n = sum(1 for a in self.store.accounts if a.autoRejoinEnabled)
        self.ar_btn.configure(text=f"Auto-Rejoin: {n}/{total}",
                              style="On.TButton" if total and n == total else
                              "Off.TButton" if n == 0 else "TButton")

    # ---- startup / shutdown --------------------------------------------
    def startup(self) -> None:
        mi_log("[ok] Account manager started.")
        ours = {r.get("pid") for r in self._boot_records if r.get("pid")}
        after = lambda: self.ui(self._restore)  # multi-instance first, THEN recover/adopt sessions
        if self.settings["autoEnableMultiInstance"]:
            self.enable_multi(known_pids=ours, then=after)
        else:
            after()

    def _restore(self) -> None:
        if self._boot_records:
            c = restore_sessions(self.manager, self.store, self._boot_records, self._boot_clean,
                                 rejoin_after_crash=self.settings["rejoinAfterCrash"], log=mi_log)
            mi_log(f"[ok] Sessions: {c['adopted']} re-attached, {c['recovered']} relaunching, "
                   f"{c['dropped']} skipped" + ("" if self._boot_clean else " (last run did not exit cleanly)."))
        self._boot_records = []
        self._restored = True
        self._scheduler_tick()  # once now (catches runs missed while closed), then every 30 s

    def _scheduler_tick(self) -> None:
        if self._closing:
            return
        try:
            self.scheduler.tick()
        except Exception as exc:
            mi_log(f"[!] Schedule: tick failed: {exc!r}")
        self.root.after(30000, self._scheduler_tick)

    def open_schedules(self) -> None:
        from schedule_window import ScheduleWindow  # imported on demand (PyInstaller: see the spec file)
        ScheduleWindow(self)

    def on_close(self) -> None:
        if self.tray and self.settings["closeToTray"]:
            self.root.withdraw()
            return
        self.quit(confirm=True)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self, confirm: bool = False) -> None:
        if confirm:
            watching = [a for a in self.store.accounts
                        if a.autoRejoinEnabled and a.id in self.client_snapshot()]
            if watching and not messagebox.askyesno(
                    "Quit?",
                    "Auto-rejoin only works while this program is running.\n\n"
                    "Open Roblox clients stay open, but they will no longer be rejoined, "
                    "and multi-instance will be released.\n\nQuit anyway?"):
                return
        self._closing = True
        if self._restored:
            self.sessions.save(self.manager.session_records(), clean=True)  # a deliberate quit
        self.manager.stop()
        self.scheduler.stop()
        self.notifier.stop()
        self.watcher.stop()
        self.holder.disable()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    # ---- tray (optional) ------------------------------------------------
    def _setup_tray(self) -> None:
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            return
        try:
            img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle((4, 4, 60, 60), radius=12, fill=(35, 41, 70, 255))
            d.polygon([(25, 18), (25, 46), (47, 32)], fill=(255, 255, 255, 255))
            menu = pystray.Menu(
                pystray.MenuItem("Show window", lambda: self.ui(self.show_window), default=True),
                pystray.MenuItem("Quit", lambda: self.ui(self.quit)),
            )
            self.tray = pystray.Icon("RobloxAccountManager", img, APP_TITLE, menu)
            self.tray.run_detached()
        except Exception as exc:
            self.tray = None
            mi_log(f"[!] Tray icon unavailable: {exc!r}")

    # ---- multi-instance -------------------------------------------------
    def toggle_multi(self) -> None:
        if self.holder.active:
            self.holder.disable()
            self.update_header()
        else:
            self.enable_multi()

    def enable_multi(self, known_pids=frozenset(), then=None) -> None:
        """`known_pids`: clients saved by an earlier run of this tool. Clients we launched
        already share the singleton objects, so they do not need to be closed."""
        ours = set(known_pids) | {v[1] for v in self.client_snapshot().values() if v[1]}
        running = find_roblox_processes()
        strangers = [(pid, name) for pid, name in running if pid not in ours]
        if strangers:
            if not messagebox.askyesno(
                    "Close Roblox?",
                    f"{len(strangers)} Roblox client(s) are running that this tool did not start. A client "
                    "like that owns the singleton objects, so ALL running clients must be closed before "
                    "multi-instance can work.\n\nAny open game will be disconnected. Close them now?"):
                mi_log("[!] Multi-instance NOT enabled (Roblox was left running).")
                if then:
                    then()
                return
            self.holder.force = True  # skips the console prompt in SingletonHolder
        trust = bool(running) and not strangers

        def work() -> None:
            try:
                self.holder.enable(trust_running=trust)
            finally:
                self.holder.force = False

                def done() -> None:
                    self.update_header()
                    if then:
                        then()
                self.ui(done)
        self.run_bg(work)

    # ---- launching ------------------------------------------------------
    def resolve_place(self, accounts: list) -> int | None:
        raw = self.place_var.get().strip()
        if not raw:
            for a in accounts:
                if a.savedPlaceId:
                    return a.savedPlaceId
            messagebox.showinfo("Pick a game", "Choose a saved game, or paste a place ID / roblox.com link.")
            return None
        saved = self.places.find(raw)
        if saved:
            return saved.placeId
        place_id, _ = parse_place_input(raw)
        if place_id is None:
            messagebox.showwarning("Unknown game", "That is not a saved game, a place ID, or a roblox.com game link.")
        return place_id

    # ---- private server (experimental) ----------------------------------
    def arm_private(self, extra: dict, place_id: int) -> None:
        """Called by the Find server dialog. The next launch from the Game box joins this private server."""
        self._arming = True
        try:
            self.place_var.set(str(place_id))
            self.job_var.set("")
        finally:
            self._arming = False
        self.launch_extra, self.launch_extra_place = extra, place_id
        self.armed_row.pack(anchor="w", pady=(6, 0))
        mi_log(f"[ok] Private server armed for place {place_id}. Launch an account to join it.")

    def clear_private(self) -> None:
        if self.launch_extra is None:
            return
        self.launch_extra, self.launch_extra_place = None, None
        self.armed_row.pack_forget()
        mi_log("[ok] Private server cleared; launches go to public servers again.")

    def _game_box_edited(self, *_args) -> None:
        if not self._arming and self.launch_extra is not None:
            self.clear_private()  # changing the game or server means "not that private server any more"

    def launch_accounts(self, accounts: list, place_id: int | None = None,
                        job_id: str | None = None) -> None:
        """`place_id` / `job_id` override the Game box (Launch group passes a saved default)."""
        if not accounts:
            messagebox.showinfo("Launch", "Select one or more accounts first.")
            return
        extra = None
        if place_id is None:
            place_id = self.resolve_place(accounts)
            if place_id is None:
                return
            job_id = self.job_var.get().strip() or None
            if self.launch_extra is not None and place_id == self.launch_extra_place:
                extra, job_id = self.launch_extra, None  # armed private server: never mixed with a job ID
        if job_id and len(job_id) < 8:
            mi_log("[!] That doesn't look like a job ID; ignoring it.")
            job_id = None

        snap = self.client_snapshot()
        todo = []
        for a in accounts:
            if a.id in self.launching or snap.get(a.id, ("",))[0] in ("starting", "running", "rejoining"):
                mi_log(f"[!] {a.username} is already running - skipped (use Stop first).")
            else:
                todo.append(a)
        if not todo:
            return

        running = len(find_roblox_processes())
        if not self.holder.active and (running or len(todo) > 1) and not messagebox.askyesno(
                "Multi-instance is OFF",
                "A new client will replace the one that is already running (or each other).\n\n"
                "Launch anyway?"):
            return
        if running + len(todo) > PERF_WARN_CLIENTS and not messagebox.askokcancel(
                "Heavy load",
                f"This would run about {running + len(todo)} Roblox clients at once. "
                "Each one uses a lot of CPU, RAM and GPU.\n\nContinue?"):
            return

        for a in todo:
            self.launching[a.id] = "Queued..."
        self.run_bg(self._launch_worker, todo, place_id, job_id, extra)

    def _launch_worker(self, accounts: list, place_id: int, job_id: str | None, extra: dict | None = None) -> None:
        total = len(accounts)
        for n, a in enumerate(accounts, 1):
            self.launching[a.id] = "Launching..."
            self.set_status(f"Launching {a.username} ({n}/{total})...")
            try:
                self.manager.launch(a, place_id, job_id, extra=extra)  # waits for the client + settle pause
                mi_log(f"[ok] {a.username} is running.")
            except LaunchError as exc:
                mi_log(f"[!] {a.username}: {exc}")
            except Exception as exc:
                mi_log(f"[!] {a.username}: unexpected error {exc!r}")
            finally:
                self.launching.pop(a.id, None)
        self.set_status("Ready")

    def stop_selected(self) -> None:
        accounts = self.selected_accounts()
        if not accounts:
            return

        def work() -> None:
            for a in accounts:
                if not self.manager.stop_account(a):
                    mi_log(f"[!] {a.username} has no running client.")
        self.run_bg(work)

    def resume_selected(self) -> None:
        for a in self.selected_accounts():
            if self.manager.resume(a):
                mi_log(f"[ok] {a.username}: trying again.")
            else:
                mi_log(f"[!] {a.username}: nothing to resume.")

    # ---- groups and notes -----------------------------------------------
    def launch_group(self) -> None:
        group = self.group_filter.get()
        members = groups.filter_accounts(self.store.accounts, group)
        if group == groups.ALL or not members:
            messagebox.showinfo("Launch group", "Pick a group in the Group box above the account list first.")
            return
        if self.place_var.get().strip():  # a game picked or typed in the Game box wins
            self.launch_accounts(members)
            return
        target = groups.resolve_target(group, None, None, self.settings["groupGames"], members)
        if target is None:
            messagebox.showinfo(
                "Launch group",
                "No game is set for this group. Pick a game in the Game box, or right-click an account "
                "in the group and choose 'Use game box as group default'.")
            return
        place_id, job_id = target
        self.launch_accounts(members, place_id=place_id, job_id=job_id)

    def set_group_selected(self) -> None:
        accounts = self.selected_accounts()
        if not accounts:
            return
        current = accounts[0].group if len({a.group for a in accounts}) == 1 else ""
        value = simpledialog.askstring(
            "Set group", "Group name (leave empty to clear):", initialvalue=current, parent=self.root)
        if value is None:
            return
        try:
            for a in accounts:
                self.store.set_group(a, value)
        except ValueError as exc:
            messagebox.showwarning("Set group", str(exc), parent=self.root)
            return
        names = ", ".join(a.username for a in accounts)
        mi_log(f"[ok] Group {accounts[0].group!r} set for: {names}" if accounts[0].group
               else f"[ok] Group cleared for: {names}")
        self._render_rows()

    def edit_note_selected(self) -> None:
        accounts = self.selected_accounts()
        if not accounts:
            return
        current = accounts[0].note if len({a.note for a in accounts}) == 1 else ""
        value = simpledialog.askstring(
            "Edit note", "Note (leave empty to clear):", initialvalue=current, parent=self.root)
        if value is None:
            return
        for a in accounts:
            self.store.set_note(a, value)
        self._render_rows()

    def set_group_default(self) -> None:
        """Saves the game in the Game box as the default for the selected account's group."""
        accounts = self.selected_accounts()
        group = accounts[0].group if accounts else ""
        if not group:
            messagebox.showinfo("Group default", "Right-click an account that is in a group "
                                                 "(use 'Set group...' first).")
            return
        raw = self.place_var.get().strip()
        if not raw:
            messagebox.showinfo("Group default", "Pick a game in the Game box first.")
            return
        saved = self.places.find(raw)
        place_id = saved.placeId if saved else parse_place_input(raw)[0]
        if place_id is None:
            messagebox.showwarning("Group default", "That is not a saved game, a place ID, or a roblox.com game link.")
            return
        job_id = self.job_var.get().strip() or None
        if job_id and len(job_id) < 8:
            job_id = None
        games = {k: v for k, v in self.settings["groupGames"].items() if k.strip().lower() != group.lower()}
        games[group] = {"placeId": place_id, "jobId": job_id}
        self.settings["groupGames"] = games
        try:
            self.settings.save()
        except OSError as exc:
            messagebox.showwarning("Group default", f"Applied, but could not save: {exc}")
        mi_log(f"[ok] Group {group!r} will launch place {place_id} when no game is picked.")

    # ---- join tools -----------------------------------------------------
    def open_join_dialog(self) -> None:
        """Server finder window. Imported here, on demand, so the main window stays light."""
        from join_dialog import JoinDialog
        dlg = getattr(self, "_join_dlg", None)
        try:
            if dlg is not None and dlg.winfo_exists():
                dlg.lift()
                return
        except tk.TclError:
            pass
        self._join_dlg = JoinDialog(self)

    # ---- auto-rejoin ----------------------------------------------------
    def toggle_selected_rejoin(self) -> None:
        accounts = self.selected_accounts()
        if not accounts:
            return
        enable = not all(a.autoRejoinEnabled for a in accounts)
        for a in accounts:
            self.manager.set_enabled(a, enable)
        self.update_header()

    def toggle_all_rejoin(self) -> None:
        accounts = self.store.accounts
        if not accounts:
            return
        enable = not all(a.autoRejoinEnabled for a in accounts)
        for a in accounts:
            self.manager.set_enabled(a, enable)
        self.update_header()

    # ---- accounts -------------------------------------------------------
    def add_account(self) -> None:
        raw = simpledialog.askstring(
            "Add account", "Paste the .ROBLOSECURITY cookie (hidden):", show="*", parent=self.root)
        if not raw:
            return
        self.set_status("Verifying cookie with Roblox...")

        def work() -> None:
            try:
                account, updated = self.store.add_from_cookie(raw)
                verb = "Updated cookie for" if updated else "Added"
                mi_log(f"[ok] {verb} {account.username} (userId {account.userId}). Cookie stored encrypted.")
            except CookieError as exc:
                self.ui(lambda: messagebox.showerror("Could not add account", str(exc)))
            except OSError as exc:
                self.ui(lambda: messagebox.showerror("Could not encrypt/save", str(exc)))
            finally:
                self.set_status("Ready")
        self.run_bg(work)

    def remove_selected(self) -> None:
        accounts = self.selected_accounts()
        if not accounts:
            return
        names = ", ".join(a.username for a in accounts)
        if not messagebox.askyesno("Remove", f"Remove from this tool: {names}?\n(Their Roblox accounts are not affected.)"):
            return
        for a in accounts:
            self.manager.stop_account(a)  # also stops auto-rejoin from reviving it
            self.store.remove(a)
        mi_log(f"[ok] Removed: {names}")

    # ---- places ---------------------------------------------------------
    def reload_places(self, select=None) -> None:
        self.place_cb["values"] = [p.name for p in self.places.places]
        if select:
            self.place_var.set(select.name)

    def add_place(self) -> None:
        raw = simpledialog.askstring(
            "Save game", "Paste the game's roblox.com link or place ID:", parent=self.root)
        if not raw:
            return
        place_id, slug_name = parse_place_input(raw)
        if place_id is None:
            messagebox.showwarning("Save game", "Couldn't find a place ID in that.")
            return
        self.set_status("Looking up the game name...")

        def work() -> None:
            name = fetch_place_name(place_id) or slug_name
            self.ui(lambda: self._finish_add_place(place_id, name))
        self.run_bg(work)

    def _finish_add_place(self, place_id: int, name: str | None) -> None:
        self.status_var.set("Ready")
        name = simpledialog.askstring(
            "Save game", "Name for this game:", initialvalue=name or f"Place {place_id}", parent=self.root)
        if name is None:
            return
        place, updated = self.places.add(place_id, name.strip() or f"Place {place_id}")
        self.reload_places(select=place)
        mi_log(f"[ok] {'Updated' if updated else 'Saved'} game: {place.name} (place {place.placeId})")

    def remove_place(self) -> None:
        place = self.places.find(self.place_var.get())
        if not place:
            messagebox.showinfo("Delete game", "Pick one of your saved games in the box first.")
            return
        if messagebox.askyesno("Delete game", f"Delete '{place.name}' from your saved games?"):
            self.places.remove(place)
            self.place_var.set("")
            self.reload_places()

    # ---- tree events ----------------------------------------------------
    def on_double_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.launch_accounts(self.selected_accounts())

    def on_right_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        if self.tree.selection():
            self.menu.tk_popup(event.x_root, event.y_root)

    # ---- diagnostics ----------------------------------------------------
    def open_diagnostics(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Diagnostics")
        win.geometry("780x540")
        text = ScrolledText(win, font=("Consolas", 9), wrap="word", state="disabled")
        text.pack(fill="both", expand=True, padx=8, pady=(8, 4))
        report = {"text": ""}

        def show(content: str) -> None:
            try:
                text.configure(state="normal")
                text.delete("1.0", "end")
                text.insert("end", content)
                text.configure(state="disabled")
            except tk.TclError:  # window was closed while a check was running
                pass

        def run() -> None:
            show("Running checks...")

            def work() -> None:
                try:
                    results = diagnostics.run_all(
                        store=self.store, holder=self.holder, include_network=True,
                        extras={"settings": self.settings, "notifier": self.notifier,
                                "winapi": self.winapi(), "last_tile": self.last_tile,
                                "scheduler": self.scheduler},
                        progress=lambda m: self.ui(lambda m=m: show(m)))
                    report["text"] = diagnostics.format_report(results)
                except Exception as exc:
                    report["text"] = f"Diagnostics failed: {exc!r}"
                self.ui(lambda: show(report["text"]))
            self.run_bg(work)

        def copy() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(report["text"])
            self.set_status("Report copied to the clipboard.")

        def save() -> None:
            path = DATA_DIR / "diagnostics_report.txt"
            try:
                path.write_text(report["text"], encoding="utf-8")
                messagebox.showinfo("Saved", f"Saved to:\n{path}", parent=win)
            except OSError as exc:
                messagebox.showerror("Save failed", str(exc), parent=win)

        bar = ttk.Frame(win, padding=(8, 0, 8, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="Run again", command=run).pack(side="left")
        ttk.Button(bar, text="Copy report", command=copy).pack(side="left", padx=6)
        ttk.Button(bar, text="Save as file", command=save).pack(side="left")
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="right")
        run()

    # ---- settings dialog ------------------------------------------------
    def open_settings(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=14)
        frm.pack()

        spins = (
            ("rejoinCooldown", "Wait before rejoining (seconds)", 1, 120),
            ("maxAttempts", "Max rejoin attempts", 1, 20),
            ("attemptWindowMinutes", "...within this many minutes", 1, 120),
            ("presencePollSeconds", "Check Roblox presence every (seconds)", 15, 300),
            ("launchPauseSeconds", "Pause after each launch (seconds)", 2, 60),
        )
        vars_: dict[str, tk.Variable] = {}
        row = 0
        for key, label, lo, hi in spins:
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
            v = tk.IntVar(value=self.settings[key])
            vars_[key] = v
            ttk.Spinbox(frm, from_=lo, to=hi, textvariable=v, width=6).grid(row=row, column=1, padx=(12, 0))
            row += 1

        checks = (
            ("autoEnableMultiInstance", "Turn multi-instance on when the app starts"),
            ("closeToTray", "Closing the window keeps running in the tray"),
            ("restoreSessions", "On startup, re-attach to Roblox clients that are still running"),
            ("rejoinAfterCrash", "After a crash, relaunch auto-rejoin accounts that were in a game"),
        )
        bools: dict[str, tk.BooleanVar] = {}
        for i, (key, label) in enumerate(checks):
            bools[key] = tk.BooleanVar(value=self.settings[key])
            ttk.Checkbutton(frm, text=label, variable=bools[key]).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(10 if i == 0 else 0, 0))
            row += 1
        if not self.tray:
            ttk.Label(frm, foreground="#777777", text="(Tray needs: pip install pystray pillow)"
                      ).grid(row=row, column=0, columnspan=2, sticky="w")
            row += 1

        ttk.Separator(frm).grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1
        ttk.Label(frm, text="Advanced - Roblox object names (comma separated). Change only if a Roblox "
                            "update breaks multi-instance; Diagnostics shows the names it finds. "
                            "Switch multi-instance off and on afterwards.",
                  foreground="#777777", wraplength=420, justify="left").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        names: dict[str, tk.StringVar] = {}
        for key, label in (("mutexNames", "Mutex name(s)"), ("eventNames", "Event name(s)")):
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
            names[key] = tk.StringVar(value=self.settings[key])
            ttk.Entry(frm, textvariable=names[key], width=34).grid(row=row, column=1, padx=(12, 0))
            row += 1

        # ---- notifications
        ttk.Separator(frm).grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1
        ttk.Label(frm, text="Notifications", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        event_vars: dict[str, tk.BooleanVar] = {}
        for event, label in EVENT_LABELS.items():
            event_vars[event] = tk.BooleanVar(value=event in self.settings["notifyEvents"])
            ttk.Checkbutton(frm, text=label, variable=event_vars[event]).grid(
                row=row, column=0, columnspan=2, sticky="w")
            row += 1
        notify_bools = {
            "notifyTray": tk.BooleanVar(value=self.settings["notifyTray"]),
            "notifyDiscord": tk.BooleanVar(value=self.settings["notifyDiscord"]),
        }
        ttk.Checkbutton(frm, text="Show a notice in the system tray" + ("" if self.tray else " (needs the tray: "
                        "pip install pystray pillow)"), variable=notify_bools["notifyTray"]).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        row += 1
        ttk.Checkbutton(frm, text="Send a Discord message", variable=notify_bools["notifyDiscord"]).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        cooldown_var = tk.IntVar(value=self.settings["notifyCooldownSeconds"])
        ttk.Label(frm, text="Same event, same account at most every (seconds)").grid(
            row=row, column=0, sticky="w", pady=3)
        ttk.Spinbox(frm, from_=1, to=3600, textvariable=cooldown_var, width=6).grid(row=row, column=1, padx=(12, 0))
        row += 1
        have_webhook = secrets_store.has(notify_discord.WEBHOOK_SLOT)
        webhook_var = tk.StringVar(value="(saved)" if have_webhook else "")
        webhook_original = webhook_var.get()
        ttk.Label(frm, text="Discord webhook URL").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=webhook_var, show="*", width=34).grid(row=row, column=1, padx=(12, 0))
        row += 1
        ttk.Label(frm, foreground="#777777", wraplength=420, justify="left",
                  text="Stored encrypted, never logged. A row of stars means one is saved: clear it to remove "
                       "the webhook, or paste a new URL to replace it. Create one in Discord under Server "
                       "Settings > Integrations > Webhooks.").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        test_label = ttk.Label(frm, text="", foreground="#777777")
        test_label.grid(row=row, column=1, sticky="w", padx=(12, 0))

        def commit_notify() -> bool:
            """Validates and saves the notification part of this dialog. False = invalid, nothing saved."""
            try:
                cooldown = int(cooldown_var.get())
                if not 1 <= cooldown <= 3600:
                    raise ValueError
            except (ValueError, tk.TclError):
                messagebox.showwarning("Settings", "Notification cooldown: use 1-3600 seconds.", parent=win)
                return False
            url = webhook_var.get().strip()
            changed = url != webhook_original
            if changed and url and not notify_discord.is_discord_webhook(url):
                messagebox.showwarning("Settings", "That is not a Discord webhook URL (it looks like "
                                       "https://discord.com/api/webhooks/<number>/<token>).", parent=win)
                return False
            if changed:
                try:
                    if url:
                        secrets_store.set(notify_discord.WEBHOOK_SLOT, url)
                    else:
                        secrets_store.delete(notify_discord.WEBHOOK_SLOT)
                except OSError as exc:
                    messagebox.showerror("Settings", f"Could not save the webhook: {exc}", parent=win)
                    return False
            self.settings["notifyEvents"] = [e for e, v in event_vars.items() if v.get()]
            for key, var in notify_bools.items():
                self.settings[key] = bool(var.get())
            self.settings["notifyCooldownSeconds"] = cooldown
            self.apply_notify_settings()
            return True

        def send_test() -> None:
            if not commit_notify():
                return
            try:
                self.settings.save()
            except OSError:
                pass
            self.notifier.notify("test", "test", "test message", force=True)
            test_label.configure(text="Test sent. Check the log, Discord and the tray.")

        ttk.Button(frm, text="Save and send test", command=send_test).grid(row=row, column=0, sticky="w")
        row += 1

        # ---- layout
        ttk.Separator(frm).grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1
        ttk.Label(frm, text="Window layout", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        api = self.winapi()
        try:
            monitors = api.get_monitors() if api else []
        except Exception:
            monitors = []
        monitor_labels = [f"{m.index + 1}: {m.work.w}x{m.work.h}" for m in monitors] or ["1"]
        ttk.Label(frm, text="Tile onto monitor").grid(row=row, column=0, sticky="w", pady=3)
        saved_monitor = self.settings["layoutMonitor"]
        monitor_var = tk.StringVar(value=monitor_labels[saved_monitor if 0 <= saved_monitor < len(monitor_labels) else 0])
        ttk.Combobox(frm, textvariable=monitor_var, values=monitor_labels, state="readonly", width=16).grid(
            row=row, column=1, padx=(12, 0), sticky="w")
        row += 1
        layout_vars: dict[str, tk.Variable] = {}
        for key, label, lo, hi in (("layoutColumns", "Columns", 1, 6), ("layoutGap", "Gap between windows (px)", 0, 32)):
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
            layout_vars[key] = tk.IntVar(value=self.settings[key])
            ttk.Spinbox(frm, from_=lo, to=hi, textvariable=layout_vars[key], width=6).grid(
                row=row, column=1, padx=(12, 0), sticky="w")
            row += 1
        auto_tile_var = tk.BooleanVar(value=self.settings["layoutAutoTile"])
        ttk.Checkbutton(frm, text="Tile automatically when a client starts or rejoins",
                        variable=auto_tile_var).grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        # ---- scheduler
        ttk.Separator(frm).grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1
        ttk.Label(frm, text="Scheduler", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        sched_on_var = tk.BooleanVar(value=self.settings["schedulerEnabled"])
        ttk.Checkbutton(frm, text="Run schedules (Schedules... button)", variable=sched_on_var).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        missed_var = tk.IntVar(value=self.settings["scheduleMissedWindowMinutes"])
        ttk.Label(frm, text="Still run a missed schedule for (minutes)").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Spinbox(frm, from_=1, to=1440, textvariable=missed_var, width=6).grid(row=row, column=1, padx=(12, 0))
        row += 1

        # ---- performance (resource saver)
        ttk.Separator(frm).grid(row=row, column=0, columnspan=2, sticky="ew", pady=10)
        row += 1
        ttk.Label(frm, text="Performance", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        profile_var = tk.StringVar(value=self.settings["performanceProfile"]
                                   if self.settings["performanceProfile"] in resourcesaver.PROFILES else "Normal")
        profile_row = ttk.Frame(frm)
        profile_row.grid(row=row, column=0, columnspan=2, sticky="w")
        for name in resourcesaver.PROFILE_ORDER:
            ttk.Radiobutton(profile_row, text=name, value=name, variable=profile_var).pack(side="left", padx=(0, 12))
        row += 1
        apply_before_var = tk.BooleanVar(value=self.settings["applyProfileBeforeLaunch"])
        ttk.Checkbutton(frm, text="Apply the profile before every launch", variable=apply_before_var).grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        perf_status = ttk.Label(frm, text="", foreground="#777777", wraplength=420, justify="left")
        perf_status.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        def refresh_perf_status(prefix: str = "") -> None:
            perf_status.configure(text=prefix + resourcesaver.describe(resourcesaver.status()))

        def apply_perf_now() -> None:
            path = _perf_path()
            if path is None:
                refresh_perf_status("Roblox install not found. ")
                return
            result = resourcesaver.apply_profile(
                profile_var.get(), settings_path=path, state_path=resourcesaver.STATE_FILE, log_fn=mi_log)
            refresh_perf_status(f"{result.status}: {result.detail}. " if result.detail else "")

        def restore_perf() -> None:
            path = _perf_path()
            did = bool(path) and resourcesaver.restore(
                settings_path=path, state_path=resourcesaver.STATE_FILE, log_fn=mi_log)
            profile_var.set("Normal")
            refresh_perf_status("Restored. " if did else "Nothing to restore. ")

        def _perf_path():
            vdir = resourcesaver.current_version_dir()
            return resourcesaver.settings_path(vdir) if vdir else None

        perf_btns = ttk.Frame(frm)
        perf_btns.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(perf_btns, text="Apply now", command=apply_perf_now).pack(side="left")
        ttk.Button(perf_btns, text="Restore original", command=restore_perf).pack(side="left", padx=6)
        ttk.Label(frm, foreground="#777777", wraplength=420, justify="left",
                  text="Roblox reads these flags when a client starts, so running clients are not affected. "
                       "Only ClientSettings/ClientAppSettings.json in the newest Roblox version folder is "
                       "written, and the original is backed up and put back on Restore."
                  ).grid(row=row + 1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        row += 2
        refresh_perf_status()

        def save() -> None:
            if not commit_notify():
                return
            try:
                for key, label, lo, hi in spins:
                    value = int(vars_[key].get())
                    if not lo <= value <= hi:
                        raise ValueError(f"{label}: use {lo}-{hi}")
                    self.settings[key] = value
            except (ValueError, tk.TclError) as exc:
                messagebox.showwarning("Settings", f"Invalid value. {exc}", parent=win)
                return
            for key, var in bools.items():
                self.settings[key] = bool(var.get())
            try:
                columns, gap = int(layout_vars["layoutColumns"].get()), int(layout_vars["layoutGap"].get())
                if not (1 <= columns <= 6 and 0 <= gap <= 32):
                    raise ValueError("Columns: use 1-6, gap: use 0-32")
            except (ValueError, tk.TclError) as exc:
                messagebox.showwarning("Settings", f"Invalid layout value. {exc}", parent=win)
                return
            self.settings["layoutColumns"], self.settings["layoutGap"] = columns, gap
            self.settings["layoutMonitor"] = max(0, monitor_labels.index(monitor_var.get())
                                                 if monitor_var.get() in monitor_labels else 0)
            self.settings["layoutAutoTile"] = bool(auto_tile_var.get())
            try:
                missed = int(missed_var.get())
                if not 1 <= missed <= 1440:
                    raise ValueError
            except (ValueError, tk.TclError):
                messagebox.showwarning("Settings", "Missed schedule window: use 1-1440 minutes.", parent=win)
                return
            self.settings["schedulerEnabled"] = bool(sched_on_var.get())
            self.settings["scheduleMissedWindowMinutes"] = missed
            self.settings["performanceProfile"] = profile_var.get()
            self.settings["applyProfileBeforeLaunch"] = bool(apply_before_var.get())
            for key, var in names.items():
                self.settings[key] = var.get().strip() or DEFAULTS[key]
            self.settings.apply()
            try:
                self.settings.save()
            except OSError as exc:
                messagebox.showwarning("Settings", f"Applied, but could not save: {exc}", parent=win)
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=save).pack(side="right", padx=6)
        win.grab_set()


_APP_LOCK = None  # keeps the single-copy mutex handle alive for the life of the process


def _acquire_app_lock() -> bool:
    """False if another copy of this app is already running (two copies would fight over
    the singleton mutex and rejoin the same accounts twice)."""
    global _APP_LOCK
    _APP_LOCK = multi_instance.kernel32.CreateMutexW(None, False, "RobloxAccountManager.GUI.singleton")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS

def main() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except Exception:
        pass
    root = tk.Tk()
    if not _acquire_app_lock():
        root.withdraw()
        messagebox.showerror(APP_TITLE, "The account manager is already running (look in the system tray "
                                        "or the taskbar).")
        root.destroy()
        return
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()