"""
Join tools, as tabs of the main window's left panel (no separate window any more):

  Player   (join_panel.py)           look up / pick a player and join their server
  Finder   (this file)               list a game's public servers; click one to join it
  Private  (this file)               your own and your friends' private servers (experimental)
  Hopper   (this file)               move accounts to a new server now or every N minutes
  History  (server_history_panel.py) every server any account got into

This file builds the Finder, Private and Hopper tabs into the notebook it is given. Joining a friend
or any user lives in join_panel.py. All network work runs through app.run_bg and results come back
through app.ui. The tabs scroll when the window is short, so nothing is ever cut off; the window and
its panels have a fixed size.
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import messagebox, ttk

import ui_style
from game_picker import GamePicker
import private_links
import private_servers as ps
import server_finder as sf
import server_hopper as sh
from places import parse_place_input


def short_id(job_id: str) -> str:
    return job_id if len(job_id) <= 13 else job_id[:8] + "…" + job_id[-4:]


class JoinTools:
    def __init__(self, app, nb: ttk.Notebook, wrap: int) -> None:
        self.app = app
        self.nb = nb            # the left panel's notebook; the Player and History tabs are added by the main window
        self.wrap = wrap        # width, in px, that long texts wrap to
        self._busy = False
        self._joining = False
        self._place_id: int | None = None
        self._servers: dict[str, sf.Server] = {}
        self._ps_busy = False
        self._ps_place_id: int | None = None   # the game the list on screen belongs to
        self._ps_found: list = []              # PrivateServer objects (codes live in memory only)
        self._ps_rows: dict[str, ps.PrivateServer] = {}
        self._hop_job = None

        self._build_finder(app.make_join_tab("Finder"))
        self._build_private(app.make_join_tab("Private"))
        self._build_hopper(app.make_join_tab("Hopper"))
        nb.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed(), add="+")
        self._hop_job = nb.after(500, self._hop_tick)   # not at once: the main window is still being built

    # ---- small helpers ----------------------------------------------------
    def _note(self, parent, text: str) -> None:
        lab = ttk.Label(parent, text=text, style="CardMuted.TLabel", wraplength=self.wrap, justify="left")
        lab.pack(anchor="w")
        ui_style.autowrap(lab, parent)

    def _line(self, parent, textvariable, pady=(6, 6)) -> ttk.Label:
        lab = ttk.Label(parent, textvariable=textvariable, style="Card.TLabel", wraplength=self.wrap, justify="left")
        lab.pack(anchor="w", fill="x", pady=pady)
        ui_style.autowrap(lab, parent)
        return lab

    def _table(self, parent, columns, height: int) -> ttk.Treeview:
        """A table with a scrollbar and fixed column widths (headings cannot be dragged).
        columns: (id, heading, width, anchor, stretch)."""
        box = ttk.Frame(parent, style="Surface.TFrame")
        box.pack(fill="x", pady=(8, 0))
        tree = ttk.Treeview(box, columns=[c[0] for c in columns], show="headings", selectmode="browse", height=height)
        for col, text, width, anchor, stretch in columns:
            tree.heading(col, text=text, anchor="w" if anchor == "w" else "center")
            tree.column(col, width=width, minwidth=24, anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="x", expand=True)
        sb.pack(side="left", fill="y")
        ui_style.lock_columns(tree)
        return tree

    # ---- Finder tab ---------------------------------------------------------
    def _build_finder(self, parent: ttk.Frame) -> None:
        self._note(parent, "Lists the public servers of the game in the Game box (or the selected account's last "
                           "game). Click a server to join it right away with the selected accounts. Servers a hop "
                           "or a join already used are marked \"visited\".")
        row = ttk.Frame(parent, style="Surface.TFrame")
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Pick", style="Card.TLabel").pack(side="left")
        self.mode_var = tk.StringVar(value=sf.MODE_LABELS["best"])
        self.mode_cb = ttk.Combobox(row, textvariable=self.mode_var, state="readonly",
                                    values=[sf.MODE_LABELS[m] for m in sf.MODES])
        self.mode_cb.pack(side="left", padx=(6, 0), fill="x", expand=True)
        row2 = ttk.Frame(parent, style="Surface.TFrame")
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="At least", style="Card.TLabel").pack(side="left")
        self.min_var = tk.IntVar(value=0)
        ttk.Spinbox(row2, from_=0, to=99, textvariable=self.min_var, width=4).pack(side="left", padx=(6, 4))
        ttk.Label(row2, text="players", style="Card.TLabel").pack(side="left")
        self.find_btn = ttk.Button(row2, text="Find", command=self.find)
        self.find_btn.pack(side="right")
        self.refresh_btn = ttk.Button(row2, text="Refresh", command=lambda: self.find(force=True))
        self.refresh_btn.pack(side="right", padx=(0, 6))

        self.tree = self._table(parent, (("players", "Players", 64, "center", False), ("ping", "Ping", 56, "center", False),
                                         ("seen", "History", 60, "center", False), ("job", "Server ID", 110, "w", True)), 8)
        self.tree.bind("<ButtonRelease-1>", self._row_clicked)   # a real click only: the auto-selected best row does not join
        self.status_var = tk.StringVar(value="Press Find, then click a server to join it.")
        self._line(parent, self.status_var)
        self.use_btn = ttk.Button(parent, text="Only fill Server box", command=self.use_selected, state="disabled")
        self.use_btn.pack(anchor="w")
        self.tree.bind("<<TreeviewSelect>>",
                       lambda e: self.use_btn.configure(state="normal" if self.tree.selection() else "disabled"))

    # ---- Private tab ----------------------------------------------------------
    def _build_private(self, parent: ttk.Frame) -> None:
        self._note(parent, "Pick a game and press Load to see the private servers of the selected account and of its "
                           "friends. Select one and press Arm, then launch an account. While armed, Launch selected "
                           "joins that private server and auto-rejoin returns to it. Editing the Game or Server box "
                           "un-arms it.")
        row = ttk.Frame(parent, style="Surface.TFrame")
        row.pack(fill="x", pady=(8, 0))
        self.ps_game_var = tk.StringVar(value="")   # filled from the Game box when this tab is first shown
        # the same picker as the main window's Game box: saved games, place ID / link, or a name to search
        self.ps_picker = GamePicker(row, self.app, self.ps_game_var, style="Surface.TFrame", on_chosen=self.ps_load)
        self.ps_picker.pack(side="left", fill="x", expand=True)
        self.ps_picker.combobox.bind("<Return>", self._ps_enter)
        self.ps_load_btn = ttk.Button(row, text="Load", command=self.ps_load)
        self.ps_load_btn.pack(side="left", padx=(6, 0))

        row2 = ttk.Frame(parent, style="Surface.TFrame")
        row2.pack(fill="x", pady=(8, 0))
        self.ps_account_var = tk.StringVar(value="Account: none selected")
        ttk.Label(row2, textvariable=self.ps_account_var, style="CardHeading.TLabel").pack(side="left")
        self.ps_filter_var = tk.StringVar(value=ps.FILTER_LABELS["all"])
        self.ps_filter_cb = ttk.Combobox(row2, textvariable=self.ps_filter_var, state="readonly", width=14,
                                         values=[ps.FILTER_LABELS[f] for f in ps.FILTERS])
        self.ps_filter_cb.pack(side="right")
        self.ps_filter_cb.bind("<<ComboboxSelected>>", lambda e: self._ps_render())
        ttk.Label(row2, text="Show", style="Card.TLabel").pack(side="right", padx=(0, 6))

        self.ps_tree = self._table(parent, (("name", "Server", 90, "w", True), ("from", "From", 52, "center", False),
                                            ("owner", "Owner", 70, "w", True), ("players", "Players", 62, "center", False),
                                            ("joinable", "Joinable", 68, "center", False)), 7)
        self.ps_tree.bind("<<TreeviewSelect>>", lambda e: self._ps_picked())
        self.ps_tree.bind("<Double-1>", lambda e: self.ps_arm() if self.ps_arm_btn["state"] == "normal" else None)
        self.ps_status_var = tk.StringVar(value="Press Load.")
        self._line(parent, self.ps_status_var)
        self.ps_arm_btn = ttk.Button(parent, text="Arm selected server", command=self.ps_arm, state="disabled")
        self.ps_arm_btn.pack(anchor="w")

    # ---- Hopper tab ------------------------------------------------------------
    def _build_hopper(self, parent: ttk.Frame) -> None:
        self._hop_hist_sig = None
        self._note(parent, "Hop now moves the selected accounts into a different public server, picked from the "
                           "Finder's list and never one in the history below. The old game is fully closed first; "
                           "if no server has room the account stays put. Auto-hop is off unless you switch it on. "
                           "Pressing Stop on an account ends its hopping; private-server accounts are skipped.")
        settings = self.app.settings
        row = ttk.Frame(parent, style="Surface.TFrame")
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Pick", style="Card.TLabel").pack(side="left")
        mode = settings["hopMode"] if settings["hopMode"] in sf.MODES else "best"
        self.hop_mode_var = tk.StringVar(value=sf.MODE_LABELS[mode])
        ttk.Combobox(row, textvariable=self.hop_mode_var, state="readonly",
                     values=[sf.MODE_LABELS[m] for m in sf.MODES]).pack(side="left", padx=(6, 0), fill="x", expand=True)
        row1 = ttk.Frame(parent, style="Surface.TFrame")
        row1.pack(fill="x", pady=(6, 0))
        ttk.Label(row1, text="At least", style="Card.TLabel").pack(side="left")
        self.hop_min_var = tk.IntVar(value=settings["hopMinPlayers"])
        ttk.Spinbox(row1, from_=0, to=99, textvariable=self.hop_min_var, width=4).pack(side="left", padx=(6, 4))
        ttk.Label(row1, text="players", style="Card.TLabel").pack(side="left")
        self.hop_account_var = tk.StringVar(value="")
        ttk.Label(row1, textvariable=self.hop_account_var, style="CardMuted.TLabel").pack(side="right")

        row2 = ttk.Frame(parent, style="Surface.TFrame")
        row2.pack(fill="x", pady=(6, 0))
        self.hop_auto_var = tk.BooleanVar(value=False)   # off by default; hopping never outlives the app
        ttk.Checkbutton(row2, text="Auto-hop every", variable=self.hop_auto_var, style="Card.TCheckbutton",
                        command=self.hop_auto_toggled).pack(side="left")
        self.hop_every_var = tk.IntVar(value=settings["hopIntervalMinutes"])
        ttk.Spinbox(row2, from_=sh.MIN_INTERVAL_MINUTES, to=sh.MAX_INTERVAL_MINUTES,
                    textvariable=self.hop_every_var, width=5).pack(side="left", padx=(6, 4))
        ttk.Label(row2, text="min", style="Card.TLabel").pack(side="left")

        btns = ttk.Frame(parent, style="Surface.TFrame")
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Hop now", command=self.hop_now).pack(side="left")
        ttk.Button(btns, text="Stop hopping", command=self.hop_stop).pack(side="left", padx=6)
        ttk.Button(btns, text="Clear", style="Ghost.TButton", command=self.hop_clear_history).pack(side="left")
        self.hop_status_var = tk.StringVar(value="Select accounts in the list, then press Hop now.")
        self._line(parent, self.hop_status_var)

        ttk.Label(parent, text="Hopping accounts", style="CardHeading.TLabel").pack(anchor="w", pady=(6, 0))
        self.hop_tree = self._table(parent, (("account", "Account", 90, "w", True), ("next", "Next", 52, "center", False),
                                             ("hops", "Hops", 40, "center", False), ("last", "Last result", 120, "w", True)), 3)
        hist_lab = ttk.Label(parent, text="History (newest first; a hop never goes back into these)",
                             style="CardHeading.TLabel", wraplength=self.wrap, justify="left")
        hist_lab.pack(anchor="w", pady=(10, 0))
        ui_style.autowrap(hist_lab, parent)
        self.hop_hist = self._table(parent, (("time", "Time", 64, "center", False), ("account", "Account", 80, "w", True),
                                             ("game", "Game", 70, "center", False), ("server", "Server ID", 100, "w", True)), 5)

    # ---- actions ---------------------------------------------------------------
    def _mode(self) -> str:
        label = self.mode_var.get()
        return next((m for m, text in sf.MODE_LABELS.items() if text == label), "best")

    def _set_status(self, text: str) -> None:
        try:
            self.status_var.set(text)
        except tk.TclError:  # the window was closed while a search was running
            pass

    # ---- actions --------------------------------------------------------
    def find(self, force: bool = False) -> None:
        """Uses the session cache (see server_finder.ServerCache); `force` asks Roblox again."""
        if self._busy:
            return
        place_id = self.app.resolve_place(self.app.selected_accounts())
        if place_id is None:
            return
        try:
            min_players = max(0, int(self.min_var.get()))
        except (ValueError, tk.TclError):
            messagebox.showwarning("Find a server", "'At least ... players' must be a number.", parent=self.nb)
            return
        mode = self._mode()
        self._busy = True
        self.find_btn.configure(state="disabled")
        self.refresh_btn.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.use_btn.configure(state="disabled")
        self._set_status(f"Looking up servers for place {place_id}...")

        def work() -> None:
            try:
                found = sf.find_servers_cached(place_id, force=force)
                ranked = sf.rank(found.servers, mode, min_players=min_players)
                sf.record_result(place_id, mode, len(found.servers), ranked[0].job_id if ranked else None)
                self.app.ui(lambda: self._show(place_id, found, ranked, min_players))
            except Exception as exc:
                text = sf.describe_error(exc)
                sf.record_result(place_id, mode, 0, None, error=text)
                self.app.ui(lambda: self._failed(text))
        self.app.run_bg(work)

    def _finish(self) -> None:
        self._busy = False
        try:
            self.find_btn.configure(state="normal")
            self.refresh_btn.configure(state="normal")
        except tk.TclError:
            pass

    def _failed(self, text: str) -> None:
        self._finish()
        self._set_status(text)

    def _show(self, place_id: int, found: sf.Found, ranked: list, min_players: int) -> None:
        self._finish()
        try:
            self._place_id = place_id
            self._servers = {s.job_id: s for s in ranked}
            for s in ranked:
                self.tree.insert("", "end", iid=s.job_id,
                                 values=(f"{s.playing}/{s.max_players}", "" if s.ping is None else f"{s.ping} ms",
                                         "visited" if self.app.hopper.was_visited(place_id, s.job_id) else "",
                                         short_id(s.job_id)))
            note = " The list may be incomplete (Roblox stopped answering)." if found.partial else ""
            if found.stale:
                note += f" Roblox is not answering or is rate-limiting, so this is an older list ({int(found.age)}s old)."
            elif found.cached:
                note += f" From cache ({int(found.age)}s old); press Refresh for a new list."
            if ranked:
                self.tree.selection_set(ranked[0].job_id)
                self.tree.focus(ranked[0].job_id)
                self._set_status(f"{len(ranked)} of {len(found.servers)} servers match. Click one to join it "
                                 f"now; servers fill up fast.{note}")
            elif found.servers:
                self._set_status(f"{len(found.servers)} servers found, but none match "
                                 f"(at least {min_players} players, with room).{note}")
            else:
                self._set_status("This game has no public servers right now." + note)
        except tk.TclError:
            pass

    def _row_clicked(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return   # the column headings are not servers
        iid = self.tree.identify_row(event.y)
        if iid:
            self.join_server(iid)

    def join_server(self, job_id: str) -> None:
        """Joins this exact server right now with the accounts selected in the main window. `strict`:
        an account that does not get into THIS server is closed, never sent somewhere else."""
        server, place_id = self._servers.get(job_id), self._place_id
        if server is None or place_id is None or self._joining:
            return
        accounts = self.app.selected_accounts()
        if not accounts:
            messagebox.showinfo("Join server", "Select one or more accounts in the main window first. "
                                               "They are the accounts that join.", parent=self.nb)
            return
        if not server.has_room:
            self._set_status("That server is full. Pick another one.")
            return
        self._joining = True
        try:
            # move: an account that is already playing is closed completely, then rejoins in THIS server
            self.app.launch_accounts(accounts, place_id, job_id, strict=True, move=True)
            started = [a for a in accounts if a.id in self.app.launching]
        finally:
            self._joining = False
        if not started:
            self._set_status("Nothing was launched (the account is already in that server, or the "
                             "launch was cancelled).")
            return
        self.app.hopper.record_visit(place_id, job_id, ", ".join(a.username for a in started[:3]))
        self.app.set_status(f"Joining server {job_id[:8]}... with {len(started)} account(s)...")

    def use_selected(self) -> None:
        sel = self.tree.selection()
        if not sel or self._place_id is None:
            return
        job_id = sel[0]
        if not self.app.place_var.get().strip():
            self.app.place_var.set(str(self._place_id))  # keep the server and the game together
        self.app.job_var.set(job_id)
        self.app.set_status("Server chosen. Launch an account to join it.")

    # ---- tabs -----------------------------------------------------------
    def _tab_changed(self) -> None:
        try:
            current = self.nb.tab(self.nb.select(), "text")
        except tk.TclError:
            return
        if current == "Private":
            if not self.ps_game_var.get().strip():
                self.ps_game_var.set(self.app.place_var.get())
            self._ps_refresh_account()
        if current == "Hopper":
            self._hop_refresh_account()


    def destroy(self) -> None:
        """Stops the 1-second refresh. Hopping itself keeps running: it belongs to the app."""
        if self._hop_job is not None:
            try:
                self.nb.after_cancel(self._hop_job)
            except tk.TclError:
                pass
            self._hop_job = None

    # ---- server hopper ----------------------------------------------------
    def _hop_mode(self) -> str:
        label = self.hop_mode_var.get()
        return next((m for m, text in sf.MODE_LABELS.items() if text == label), "best")

    def _hop_refresh_account(self) -> None:
        try:
            accounts = self.app.selected_accounts()
            self.hop_account_var.set(f"{len(accounts)} selected")
        except tk.TclError:
            pass

    def _hop_params(self, need_interval: bool = True) -> tuple[float, int] | None:
        """(minutes, min_players). Hop now has no schedule, so it does not care about the interval box."""
        try:
            min_players = max(0, int(self.hop_min_var.get()))
            minutes = float(self.hop_every_var.get())
        except (ValueError, tk.TclError):
            if need_interval:
                messagebox.showwarning("Server hopper", "'Auto-hop every' and 'At least' must be numbers.",
                                       parent=self.nb)
                return None
            try:
                min_players, minutes = max(0, int(self.hop_min_var.get())), float(sh.MIN_INTERVAL_MINUTES)
            except (ValueError, tk.TclError):
                messagebox.showwarning("Server hopper", "'At least' must be a number.", parent=self.nb)
                return None
        if need_interval and minutes < sh.MIN_INTERVAL_MINUTES:
            messagebox.showwarning("Server hopper", f"Auto-hop at most every {sh.MIN_INTERVAL_MINUTES} minutes; "
                                   "faster only causes failed launches and rate limits.", parent=self.nb)
            return None
        return max(minutes, float(sh.MIN_INTERVAL_MINUTES)), min_players

    def _hop_place_for(self, accounts: list) -> tuple[bool, int | None]:
        """(ok, place). A place is needed only for accounts that are not running yet; running ones
        hop within the game they are in."""
        snap = self.app.client_snapshot()
        idle = [a for a in accounts if a.id not in snap]
        if not idle:
            return True, None
        place_id = self.app.resolve_place(idle)
        return place_id is not None, place_id

    def hop_auto_toggled(self) -> None:
        """The Auto-hop checkbox. On: schedule the selected accounts. Off: cancel every schedule."""
        if self.hop_auto_var.get():
            if not self.hop_start():
                self.hop_auto_var.set(False)   # cancelled or invalid: stay off
        else:
            self.app.hopper.disable_all()
            self.hop_status_var.set("Auto-hop is off. Their games keep running.")
            self._hop_render()

    def hop_start(self) -> bool:
        """Turns on the timer for the selected accounts. Returns False if nothing was started."""
        accounts = self.app.selected_accounts()
        if not accounts:
            messagebox.showinfo("Server hopper", "Select one or more accounts in the main window first.", parent=self.nb)
            return False
        params = self._hop_params()
        if params is None:
            return False
        minutes, min_players = params
        ok, place_id = self._hop_place_for(accounts)
        if not ok:
            return False
        if not self.app.confirm(
                "Start auto-hop?",
                f"Every {minutes:g} minutes each of these {len(accounts)} account(s) will be closed and started "
                "again in a different server:\n\n" + ", ".join(a.username for a in accounts[:8])
                + (" ..." if len(accounts) > 8 else ""), parent=self.nb):
            return False
        mode = self._hop_mode()
        settings = self.app.settings
        settings["hopIntervalMinutes"], settings["hopMode"], settings["hopMinPlayers"] = int(minutes), mode, min_players
        try:
            settings.save()
        except OSError:
            pass
        snap = self.app.client_snapshot()
        for n, a in enumerate(accounts):   # staggered, so they do not all hop in the same minute
            running = a.id in snap
            self.app.hopper.enable(a, interval_minutes=minutes, mode=mode, min_players=min_players,
                                   place_id=place_id, first_in=(minutes * 60 + n * 30) if running else n * 20)
        self.hop_status_var.set(f"Auto-hop on: {len(accounts)} account(s) every {minutes:g} min.")
        self._hop_render()
        return True

    def hop_now(self) -> None:
        accounts = self.app.selected_accounts()
        if not accounts:
            messagebox.showinfo("Server hopper", "Select one or more accounts in the main window first.", parent=self.nb)
            return
        params = self._hop_params(need_interval=False)
        if params is None:
            return
        minutes, min_players = params
        todo = [a for a in accounts if not self.app.hopper.is_enabled(a)]
        place_id = None
        if todo:
            ok, place_id = self._hop_place_for(todo)
            if not ok:
                return
        if not self.app.confirm("Hop now?", f"Close and restart {len(accounts)} account(s) in a different server now?",
                                parent=self.nb):
            return
        for n, a in enumerate(todo):   # no plan yet: a single hop that removes itself
            self.app.hopper.enable(a, interval_minutes=minutes, mode=self._hop_mode(), min_players=min_players,
                                   place_id=place_id, first_in=n * 20, repeat=False)
        for a in accounts:
            self.app.hopper.hop_now(a)
        self.hop_status_var.set(f"Hopping {len(accounts)} account(s) now.")
        self._hop_render()

    def hop_stop(self) -> None:
        accounts = self.app.selected_accounts()
        n = sum(1 for a in accounts if self.app.hopper.disable(a, "stopped by you"))
        self.hop_status_var.set(f"Stopped hopping for {n} account(s). Their games keep running.")
        self._hop_render()

    def hop_clear_history(self) -> None:
        self.app.hopper.clear_history()
        self.hop_status_var.set("History cleared: hops may go back into any server again.")
        self._hop_render()

    def _hop_render(self) -> None:
        try:
            rows = self.app.hopper.snapshot()
            seen = set()
            for r in rows:
                seen.add(r["id"])
                if r["repeat"]:
                    mins, secs = divmod(int(r["next_in"]), 60)
                    nxt = f"{mins}:{secs:02d}" if r["next_in"] else "now"
                else:
                    nxt = "now"   # a one-off hop has no timer
                values = (r["username"], nxt, r["hops"], r["last"])
                if self.hop_tree.exists(r["id"]):
                    self.hop_tree.item(r["id"], values=values)
                else:
                    self.hop_tree.insert("", "end", iid=r["id"], values=values)
            for iid in self.hop_tree.get_children():
                if iid not in seen:
                    self.hop_tree.delete(iid)
            if self.hop_auto_var.get() and not any(r["repeat"] for r in rows):
                self.hop_auto_var.set(False)   # every timer ended (Stop in the main window, failures)
            self._hop_render_history()
        except tk.TclError:
            pass

    def _hop_render_history(self) -> None:
        rows = self.app.hopper.history()
        sig = (len(rows), rows[0]["seq"] if rows else 0)
        if sig == self._hop_hist_sig:
            return   # nothing new: leave the list (and the user's scroll position) alone
        self._hop_hist_sig = sig
        self.hop_hist.delete(*self.hop_hist.get_children())
        for r in rows:
            self.hop_hist.insert("", "end", values=(time.strftime("%H:%M:%S", time.localtime(r["at"])),
                                                    r["who"], r["place_id"], short_id(r["job_id"])))

    def _hop_tick(self) -> None:
        self._hop_refresh_account()
        self._hop_render()
        try:
            self._hop_job = self.nb.after(1000, self._hop_tick)
        except tk.TclError:
            self._hop_job = None

    # ---- private server list ---------------------------------------------
    def _ps_refresh_account(self) -> None:
        accounts = self.app.selected_accounts()
        try:
            self.ps_account_var.set(f"Account: {accounts[0].username}" if accounts else "Account: none selected")
        except tk.TclError:
            pass

    def _ps_enter(self, _event=None) -> None:
        """Enter in the game box: load a game that is already named, otherwise search for the text."""
        if self.ps_picker.is_resolved():
            self.ps_load()
        else:
            self.ps_picker.search()

    def _ps_place(self) -> int | None:
        """The game typed or picked in this tab (a saved game's name, a place ID or a roblox.com link).
        Plain text opens the online search instead; picking a result loads the list."""
        raw = self.ps_game_var.get().strip()
        if not raw:
            messagebox.showinfo("Private servers", "Pick a saved game, paste a place ID / roblox.com link, or "
                                                   "type a name and press Search.", parent=self.nb)
            return None
        saved = self.app.places.find(raw)
        if saved:
            return saved.placeId
        place_id, _ = parse_place_input(raw)
        if place_id is None:
            self.ps_picker.search()
        return place_id

    def ps_load(self) -> None:
        if self._ps_busy:
            return
        accounts = self.app.selected_accounts()
        if not accounts:
            messagebox.showinfo("Private servers", "Select an account in the main window first. Its own private "
                                                   "servers and its friends' are listed.", parent=self.nb)
            return
        place_id = self._ps_place()
        if place_id is None:
            return
        asker = accounts[0]
        self._ps_refresh_account()
        self._ps_busy = True
        self.ps_load_btn.configure(state="disabled")
        self.ps_arm_btn.configure(state="disabled")
        self.ps_tree.delete(*self.ps_tree.get_children())
        self._ps_rows, self._ps_found = {}, []
        self.ps_status_var.set(f"Loading private servers for place {place_id}...")

        def work() -> None:
            try:
                cookie = self.app.store.get_cookie(asker)
                try:
                    found = ps.find_private_servers(place_id, cookie, asker.userId)
                finally:
                    del cookie
                ps.record_result(place_id, len(found.servers))
                self.app.ui(lambda: self._ps_show(place_id, found))
            except Exception as exc:
                text = ps.describe_error(exc)
                ps.record_result(place_id, 0, error=text)
                self.app.ui(lambda: self._ps_failed(text))
        self.app.run_bg(work)

    def _ps_finish(self) -> None:
        self._ps_busy = False
        try:
            self.ps_load_btn.configure(state="normal")
        except tk.TclError:
            pass

    def _ps_failed(self, text: str) -> None:
        self._ps_finish()
        try:
            self.ps_status_var.set(text)
        except tk.TclError:
            pass

    def _ps_show(self, place_id: int, found: ps.Found) -> None:
        self._ps_finish()
        self._ps_place_id = place_id
        self._ps_found = list(found.servers)
        self._ps_partial = found.partial
        self._ps_render()

    def _ps_filter(self) -> str:
        label = self.ps_filter_var.get()
        return next((f for f, text in ps.FILTER_LABELS.items() if text == label), "all")

    def _ps_render(self) -> None:
        """Fills the list from the loaded servers, applying the Show filter."""
        try:
            self.ps_tree.delete(*self.ps_tree.get_children())
            self.ps_arm_btn.configure(state="disabled")
            shown = ps.apply_filter(self._ps_found, self._ps_filter())
            self._ps_rows = {}
            for i, server in enumerate(shown):
                iid = str(i)
                self._ps_rows[iid] = server
                self.ps_tree.insert("", "end", iid=iid, values=(
                    server.name, ps.KIND_LABELS[server.kind], server.owner_name,
                    f"{server.playing}/{server.max_players}", "Yes" if server.joinable else "No"))
            if not self._ps_found:
                if self._ps_place_id is None:
                    return
                self.ps_status_var.set("No private servers found for this account or its friends in that game."
                                       + (" The list may be incomplete." if getattr(self, "_ps_partial", False) else ""))
                return
            mine = sum(1 for s in self._ps_found if s.kind == ps.KIND_MINE)
            note = " The list may be incomplete (Roblox stopped answering)." if getattr(self, "_ps_partial", False) else ""
            self.ps_status_var.set(f"{len(shown)} shown: {mine} yours, {len(self._ps_found) - mine} from friends. "
                                   f"Select one and press Arm.{note}")
        except tk.TclError:
            pass

    def _ps_picked(self) -> None:
        sel = self.ps_tree.selection()
        server = self._ps_rows.get(sel[0]) if sel else None
        if server is None:
            self.ps_arm_btn.configure(state="disabled")
            return
        self.ps_arm_btn.configure(state="normal" if server.joinable else "disabled")
        if not server.joinable:
            self.ps_status_var.set("Roblox did not give a join code for that server, so it cannot be joined from here.")

    def ps_arm(self) -> None:
        sel = self.ps_tree.selection()
        server = self._ps_rows.get(sel[0]) if sel else None
        place_id = self._ps_place_id
        extra = ps.extra_for(server) if server is not None else None
        if extra is None or place_id is None:
            return
        self.app.arm_private(extra, place_id)
        private_links.record_result("armed", place_id)
        self.app.set_status("Private server armed. Launch an account to join it.")