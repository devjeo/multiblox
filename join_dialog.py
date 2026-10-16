"""
Join tools window. Tabs: "Server finder" (Feature C1), "Join friend" (Feature C2) and
"Private server" (Feature C3, experimental).

Imported only when the "Find server..." button is pressed, so gui.py stays light.
All network work runs through app.run_bg and results come back through app.ui.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

import friends
import private_links
import server_finder as sf
from places import parse_place_link


class JoinDialog(tk.Toplevel):
    def __init__(self, app) -> None:
        super().__init__(app.root)
        self.app = app
        self.title("Join tools")
        self.geometry("620x440")
        self.minsize(520, 360)
        self.transient(app.root)
        self._busy = False
        self._place_id: int | None = None
        self._servers: dict[str, sf.Server] = {}

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        finder = ttk.Frame(self.nb, padding=10)
        self.nb.add(finder, text="Server finder")
        self._build_finder(finder)

        self._friend_busy = False
        self._friend_user: friends.User | None = None
        friend = ttk.Frame(self.nb, padding=10)
        self.nb.add(friend, text="Join friend")
        self._build_friend(friend)

        self._parsed = None  # the last parsed private-server link (memory only)
        private = ttk.Frame(self.nb, padding=10)
        self.nb.add(private, text="Private server")
        self._build_private(private)

    # ---- UI -------------------------------------------------------------
    def _build_finder(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Looks at the game in the main window's Game box (or the selected account's "
                               "last game) and lists its public servers.",
                  style="Muted.TLabel", wraplength=560, justify="left").pack(anchor="w")
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Pick:").pack(side="left")
        self.mode_var = tk.StringVar(value=sf.MODE_LABELS["best"])
        self.mode_cb = ttk.Combobox(row, textvariable=self.mode_var, state="readonly", width=38,
                                    values=[sf.MODE_LABELS[m] for m in sf.MODES])
        self.mode_cb.pack(side="left", padx=(6, 12))
        ttk.Label(row, text="At least").pack(side="left")
        self.min_var = tk.IntVar(value=0)
        ttk.Spinbox(row, from_=0, to=99, textvariable=self.min_var, width=4).pack(side="left", padx=(6, 4))
        ttk.Label(row, text="players").pack(side="left")
        self.find_btn = ttk.Button(row, text="Find", command=self.find)
        self.find_btn.pack(side="right")

        cols = ("players", "ping", "job")
        self.tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        for col, text, width, anchor in (("players", "Players", 80, "center"), ("ping", "Ping", 60, "center"),
                                         ("job", "Server (job) ID", 380, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor)
        sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        body = ttk.Frame(parent)
        body.pack(fill="both", expand=True, pady=8)
        self.tree.pack(in_=body, side="left", fill="both", expand=True)
        sb.pack(in_=body, side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.use_selected())

        bottom = ttk.Frame(parent)
        bottom.pack(fill="x")
        self.status_var = tk.StringVar(value="Press Find.")
        ttk.Label(bottom, textvariable=self.status_var, wraplength=420, justify="left").pack(side="left")
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side="right")
        self.use_btn = ttk.Button(bottom, text="Use this server", command=self.use_selected, state="disabled")
        self.use_btn.pack(side="right", padx=6)
        self.tree.bind("<<TreeviewSelect>>",
                       lambda e: self.use_btn.configure(state="normal" if self.tree.selection() else "disabled"))

    def _build_friend(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Finds the server a friend is playing in and launches the selected account(s) "
                               "into it. Uses the first selected account to ask Roblox, so select an account in "
                               "the main window first. Roblox only shows a server to accounts the friend "
                               "allows to join them.",
                  style="Muted.TLabel", wraplength=560, justify="left").pack(anchor="w")
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="Username or ID:").pack(side="left")
        self.friend_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.friend_var, width=30)
        entry.pack(side="left", padx=(6, 8))
        entry.bind("<Return>", lambda e: self.lookup_friend())
        self.lookup_btn = ttk.Button(row, text="Look up", command=self.lookup_friend)
        self.lookup_btn.pack(side="left")

        self.friend_name_var = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.friend_name_var, style="Heading.TLabel").pack(anchor="w", pady=(14, 0))
        self.friend_status_var = tk.StringVar(value="Type a username (or a user ID / profile link) and press Look up.")
        ttk.Label(parent, textvariable=self.friend_status_var, wraplength=560, justify="left").pack(anchor="w", pady=(4, 0))

        bottom = ttk.Frame(parent)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side="right")
        self.join_btn = ttk.Button(bottom, text="Join", command=self.join_friend, state="disabled")
        self.join_btn.pack(side="right", padx=6)

    def _build_private(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="EXPERIMENTAL. Join your private server once in your browser (the game's "
                               "Private Servers list, then Join), copy the roblox-player:... text that "
                               "appears, and paste it here. The app keeps only the place and the server code "
                               "(in memory, never saved) and discards the login ticket in it.",
                  style="Muted.TLabel", wraplength=560, justify="left").pack(anchor="w")
        self.private_var = tk.StringVar()
        entry = ttk.Entry(parent, textvariable=self.private_var)
        entry.pack(fill="x", pady=(10, 0))
        entry.bind("<Return>", lambda e: self.parse_private())
        ttk.Button(parent, text="Parse", command=self.parse_private).pack(anchor="w", pady=(8, 0))
        self.private_status_var = tk.StringVar(value="Paste the text above, then press Parse.")
        ttk.Label(parent, textvariable=self.private_status_var, wraplength=560, justify="left").pack(anchor="w", pady=(10, 0))
        ttk.Label(parent, text="While armed, Launch selected joins this private server and auto-rejoin returns "
                               "to it (never to a public server). Editing the Game or Server box un-arms it.",
                  style="Muted.TLabel", wraplength=560, justify="left").pack(anchor="w", pady=(10, 0))
        bottom = ttk.Frame(parent)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side="right")
        self.arm_btn = ttk.Button(bottom, text="Arm", command=self.arm_private, state="disabled")
        self.arm_btn.pack(side="right", padx=6)

    def _mode(self) -> str:
        label = self.mode_var.get()
        return next((m for m, text in sf.MODE_LABELS.items() if text == label), "best")

    def _set_status(self, text: str) -> None:
        try:
            self.status_var.set(text)
        except tk.TclError:  # the window was closed while a search was running
            pass

    # ---- actions --------------------------------------------------------
    def find(self) -> None:
        if self._busy:
            return
        place_id = self.app.resolve_place(self.app.selected_accounts())
        if place_id is None:
            return
        try:
            min_players = max(0, int(self.min_var.get()))
        except (ValueError, tk.TclError):
            messagebox.showwarning("Find a server", "'At least ... players' must be a number.", parent=self)
            return
        mode = self._mode()
        self._busy = True
        self.find_btn.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.use_btn.configure(state="disabled")
        self._set_status(f"Looking up servers for place {place_id}...")

        def work() -> None:
            try:
                found = sf.find_servers(place_id)
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
                                         s.job_id))
            note = " The list may be incomplete (Roblox stopped answering)." if found.partial else ""
            if ranked:
                self.tree.selection_set(ranked[0].job_id)
                self.tree.focus(ranked[0].job_id)
                self._set_status(f"{len(ranked)} of {len(found.servers)} servers match; the best is selected. "
                                 f"Servers fill up fast, so join soon.{note}")
            elif found.servers:
                self._set_status(f"{len(found.servers)} servers found, but none match "
                                 f"(at least {min_players} players, with room).{note}")
            else:
                self._set_status("This game has no public servers right now." + note)
        except tk.TclError:
            pass

    def use_selected(self) -> None:
        sel = self.tree.selection()
        if not sel or self._place_id is None:
            return
        job_id = sel[0]
        if not self.app.place_var.get().strip():
            self.app.place_var.set(str(self._place_id))  # keep the server and the game together
        self.app.job_var.set(job_id)
        self.app.set_status("Server chosen. Launch an account to join it.")
        self.destroy()

    # ---- join friend ----------------------------------------------------
    def _set_friend(self, name: str, status: str, joinable: bool) -> None:
        try:
            self.friend_name_var.set(name)
            self.friend_status_var.set(status)
            self.join_btn.configure(state="normal" if joinable else "disabled")
        except tk.TclError:  # the window was closed while a lookup was running
            pass

    def _friend_done(self) -> None:
        self._friend_busy = False
        try:
            self.lookup_btn.configure(state="normal")
        except tk.TclError:
            pass

    def _friend_presence(self, account, user: friends.User):
        """Runs in a worker. The cookie lives only inside this call."""
        cookie = self.app.store.get_cookie(account)
        try:
            return friends.get_presence(cookie, user.id)
        finally:
            del cookie

    def lookup_friend(self) -> None:
        if self._friend_busy:
            return
        accounts = self.app.selected_accounts()
        if not accounts:
            messagebox.showinfo("Join friend", "Select an account in the main window first. It is used to ask "
                                               "Roblox where your friend is.", parent=self)
            return
        query = self.friend_var.get().strip()
        if not query:
            return
        asker = accounts[0]
        self._friend_busy = True
        self.lookup_btn.configure(state="disabled")
        self._friend_user = None
        self._set_friend("", "Looking up...", False)

        def work() -> None:
            user = None
            try:
                user = friends.find_user(query)
                if user is None:
                    friends.record_result(None, "no such user")
                    self.app.ui(lambda: (self._friend_done(), self._set_friend(
                        "", "No Roblox user found for that name or ID.", False)))
                    return
                presence = self._friend_presence(asker, user)
                target = friends.resolve_join_target(presence)
                friends.record_result(user.id, "joinable" if target else "unavailable")
                text = friends.describe_presence(presence)
                title = user.name if not user.display_name or user.display_name == user.name \
                    else f"{user.display_name} (@{user.name})"
                self.app.ui(lambda: (self._friend_done(), self._remember(user), self._set_friend(
                    title, text, target is not None)))
            except Exception as exc:
                text = friends.describe_error(exc)
                friends.record_result(user.id if user else None, "error", error=text)
                self.app.ui(lambda: (self._friend_done(), self._set_friend("", text, False)))
        self.app.run_bg(work)

    def _remember(self, user: friends.User) -> None:
        self._friend_user = user

    def join_friend(self) -> None:
        """Asks Roblox again (the friend may have moved), then launches the selected accounts there."""
        user = self._friend_user
        accounts = self.app.selected_accounts()
        if user is None or self._friend_busy:
            return
        if not accounts:
            messagebox.showinfo("Join friend", "Select the account(s) to launch in the main window first.", parent=self)
            return
        asker = accounts[0]
        self._friend_busy = True
        self.lookup_btn.configure(state="disabled")
        self.join_btn.configure(state="disabled")
        self.friend_status_var.set("Checking where they are now...")

        def work() -> None:
            try:
                presence = self._friend_presence(asker, user)
                target = friends.resolve_join_target(presence)
                if target is None:
                    friends.record_result(user.id, "unavailable")
                    text = friends.describe_presence(presence)
                    self.app.ui(lambda: (self._friend_done(), self._set_friend(
                        self.friend_name_var.get(), "They are no longer joinable. " + text, False)))
                    return
                friends.record_result(user.id, "joined")
                place_id, job_id = target
                self.app.ui(lambda: self._launch(accounts, place_id, job_id))
            except Exception as exc:
                text = friends.describe_error(exc)
                friends.record_result(user.id, "error", error=text)
                self.app.ui(lambda: (self._friend_done(), self._set_friend(self.friend_name_var.get(), text, False)))
        self.app.run_bg(work)

    def _launch(self, accounts: list, place_id: int, job_id: str) -> None:
        self._friend_done()
        self.app.launch_accounts(accounts, place_id, job_id)  # the existing job-ID launch path
        self.app.set_status("Joining your friend's server...")
        self.destroy()

    # ---- private server -------------------------------------------------
    def parse_private(self) -> None:
        text = self.private_var.get()
        parsed = parse_place_link(text)
        can_arm, message = private_links.assess(parsed)
        self._parsed = parsed if can_arm else None
        self.private_var.set("")  # the pasted text holds a login ticket: do not leave it in the box
        self.private_status_var.set(message)
        self.arm_btn.configure(state="normal" if can_arm else "disabled")

    def arm_private(self) -> None:
        extra = private_links.extra_from(self._parsed)
        if extra is None or self._parsed is None or self._parsed.place_id is None:
            return
        place_id = self._parsed.place_id
        self.app.arm_private(extra, place_id)
        private_links.record_result("armed", place_id)
        self.app.set_status("Private server armed. Launch an account to join it.")
        self.destroy()