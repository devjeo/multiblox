"""
Join tools window. Tabs: "Server finder" (Feature C1), "Join friend" and "Join user" (Feature C2) and
"Private server" (Feature C3: pick a game and see your own and your friends' private servers).

Imported only when the "Find server..." button is pressed, so gui.py stays light.
All network work runs through app.run_bg and results come back through app.ui.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

import friends
from game_picker import GamePicker
from multi_instance import log as mi_log
import private_links
import private_servers as ps
import server_finder as sf
from places import parse_place_input


class UserTab:
    """The two 'join a person' tabs share this code.

    mode "friend": the account's ONLINE FRIENDS are listed; ONE account joins. Choosing this tab
                   reduces the main window's selection to its top account and locks it, so the
                   account whose friends you see is always the account that joins.
    mode "public": look up ANY user by username / ID and see whether they can be joined. Every
                   selected account that Roblox actually lets into that server joins.
    """

    def __init__(self, dlg: "JoinDialog", parent: ttk.Frame, mode: str) -> None:
        self.dlg, self.app, self.mode = dlg, dlg.app, mode
        self.busy = False
        self.user: friends.User | None = None
        self.asker = None            # friend mode: the locked account
        self.entries: dict[str, friends.FriendEntry] = {}
        self.loaded = False
        self.parent = parent
        self._build(parent)

    # ---- UI -------------------------------------------------------------
    def _build(self, parent: ttk.Frame) -> None:
        if self.mode == "friend":
            text = ("Pick one of your online friends and press Join. Only ONE account joins: the top selected "
                    "account in the main window, which is locked while this tab is open. Roblox only shows a "
                    "server if the friend's privacy settings allow it.")
        else:
            text = ("Look up any Roblox user, friend or not, to see if you can join them. Every account selected "
                    "in the main window that Roblox lets into their server joins; the first selected account "
                    "is used to ask Roblox. Roblox only shows a server if the user's privacy settings allow it.")
        ttk.Label(parent, text=text, style="Muted.TLabel", wraplength=560, justify="left").pack(anchor="w")

        if self.mode == "friend":
            acct = ttk.Frame(parent)
            acct.pack(fill="x", pady=(8, 0))
            self.asker_var = tk.StringVar(value="Account: none selected")
            ttk.Label(acct, textvariable=self.asker_var, style="Heading.TLabel").pack(side="left")
            self.change_btn = ttk.Button(acct, text="Change account", command=self.change_account)
            self.change_btn.pack(side="left", padx=10)
            top = ttk.Frame(parent)
            top.pack(fill="x", pady=(8, 0))
            self.refresh_btn = ttk.Button(top, text="Refresh online friends", command=self.load_friends)
            self.refresh_btn.pack(side="left")
            self.count_var = tk.StringVar(value="")
            ttk.Label(top, textvariable=self.count_var, style="Muted.TLabel").pack(side="left", padx=10)
            self.tree = ttk.Treeview(parent, columns=("name", "status"), show="headings",
                                     selectmode="browse", height=8)
            for col, label, width in (("name", "Online friend", 260), ("status", "Status", 260)):
                self.tree.heading(col, text=label)
                self.tree.column(col, width=width, anchor="w")
            sb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
            self.tree.configure(yscrollcommand=sb.set)
            body = ttk.Frame(parent)
            body.pack(fill="both", expand=True, pady=6)
            self.tree.pack(in_=body, side="left", fill="both", expand=True)
            sb.pack(in_=body, side="left", fill="y")
            self.tree.lift()   # created before `body`, so it would sit under it and be invisible
            sb.lift()
            self.tree.bind("<<TreeviewSelect>>", lambda e: self._picked())
            self.tree.bind("<Double-1>", lambda e: self.join() if self.join_btn["state"] == "normal" else None)
        else:
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=(10, 0))
            ttk.Label(row, text="Username or ID:").pack(side="left")
            self.query_var = tk.StringVar()
            entry = ttk.Entry(row, textvariable=self.query_var, width=30)
            entry.pack(side="left", padx=(6, 8))
            entry.bind("<Return>", lambda e: self.lookup())
            self.lookup_btn = ttk.Button(row, text="Look up", command=self.lookup)
            self.lookup_btn.pack(side="left")

        self.name_var = tk.StringVar(value="")
        ttk.Label(parent, textvariable=self.name_var, style="Heading.TLabel").pack(anchor="w", pady=(10, 0))
        self.status_var = tk.StringVar(value=(
            "Select an account in the main window, then press Refresh online friends." if self.mode == "friend"
            else "Type a username (or a user ID / profile link) and press Look up."))
        ttk.Label(parent, textvariable=self.status_var, wraplength=560, justify="left").pack(anchor="w", pady=(2, 0))

        bottom = ttk.Frame(parent)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Close", command=self.dlg.destroy).pack(side="right")
        self.join_btn = ttk.Button(bottom, text="Join", command=self.join, state="disabled")
        self.join_btn.pack(side="right", padx=6)

    # ---- small helpers --------------------------------------------------
    def _set(self, name: str, status: str, joinable: bool) -> None:
        try:
            self.name_var.set(name)
            self.status_var.set(status)
            self.join_btn.configure(state="normal" if joinable else "disabled")
        except tk.TclError:  # the window was closed while a lookup was running
            pass

    def _done(self) -> None:
        self.busy = False
        for name in ("lookup_btn", "refresh_btn"):
            btn = getattr(self, name, None)
            if btn is not None:
                try:
                    btn.configure(state="normal")
                except tk.TclError:
                    pass

    def _busy_on(self) -> None:
        self.busy = True
        for name in ("lookup_btn", "refresh_btn"):
            btn = getattr(self, name, None)
            if btn is not None:
                btn.configure(state="disabled")

    def _presence(self, account, user: friends.User):
        """Runs in a worker. The cookie lives only inside this call."""
        cookie = self.app.store.get_cookie(account)
        try:
            return friends.get_presence(cookie, user.id)
        finally:
            del cookie

    # ---- friend mode: lock the account, list online friends ----------------
    def activate(self) -> None:
        """The Join friend tab was opened."""
        if self.asker is None and self.app.selected_accounts():
            self.load_friends()

    def deactivate(self) -> None:
        """Left the tab or closed the window: give the main list back."""
        if self.mode != "friend":
            return
        self.asker = None
        self.app.unlock_selection()
        try:
            self.asker_var.set("Account: none selected")
        except (tk.TclError, AttributeError):
            pass

    def _lock_top_account(self):
        """Reduce the selection to its top account and lock the list to it."""
        if self.asker is not None:
            return self.asker
        accounts = self.app.selected_accounts()
        if not accounts:
            messagebox.showinfo("Join friend", "Select an account in the main window first. It is the account "
                                               "that joins, and whose friends are listed.", parent=self.dlg)
            return None
        self.asker = accounts[0]
        self.app.lock_selection(accounts[0])   # also unselects the others
        self.asker_var.set(f"Account: {accounts[0].username}")
        return accounts[0]

    def change_account(self) -> None:
        if self.busy:
            return
        self.deactivate()
        self.loaded = False
        self.entries = {}
        self.user = None
        try:
            self.tree.delete(*self.tree.get_children())
        except tk.TclError:
            pass
        self.count_var.set("")
        self._set("", "Select another account in the main window, then press Refresh online friends.", False)

    def load_friends(self) -> None:
        if self.busy:
            return
        asker = self._lock_top_account()
        if asker is None:
            return
        self._busy_on()
        self.count_var.set("Loading...")

        def work() -> None:
            try:
                cookie = self.app.store.get_cookie(asker)
                try:
                    entries = friends.online_friends(cookie, asker.userId)
                finally:
                    del cookie
                friends.record_result(None, f"listed {len(entries)} online")
                self.app.ui(lambda: self._show_friends(asker, entries))
            except Exception as exc:
                text = friends.describe_error(exc)
                friends.record_result(None, "error", error=text)
                self.app.ui(lambda: self._friends_failed(text))
        self.app.run_bg(work)

    def _friends_failed(self, text: str) -> None:
        self._done()
        try:
            self.count_var.set("")
            self.status_var.set(text)
        except tk.TclError:
            pass

    def _show_friends(self, asker, entries: list) -> None:
        self._done()
        try:
            self.tree.delete(*self.tree.get_children())
        except tk.TclError:
            return
        self.loaded = True
        self.entries = {str(e.user.id): e for e in entries}
        for e in entries:
            self.tree.insert("", "end", iid=str(e.user.id), values=(e.title, e.status))
        joinable = sum(1 for e in entries if e.joinable)
        self.count_var.set(f"{asker.username}: {len(entries)} online, {joinable} joinable")
        self._set("", "Pick a friend from the list." if entries else
                  "None of this account's friends are online right now.", False)

    def _picked(self) -> None:
        sel = self.tree.selection()
        entry = self.entries.get(sel[0]) if sel else None
        if entry is None:
            return
        self.user = entry.user
        self._set(entry.title, friends.describe_presence(entry.presence), entry.joinable)

    # ---- public mode: look anyone up ---------------------------------------
    def lookup(self) -> None:
        if self.busy:
            return
        accounts = self.app.selected_accounts()
        if not accounts:
            messagebox.showinfo("Join user", "Select an account in the main window first. It is used to ask "
                                             "Roblox where the user is.", parent=self.dlg)
            return
        query = self.query_var.get().strip()
        if not query:
            return
        asker = accounts[0]
        self._busy_on()
        self.user = None
        self._set("", "Looking up...", False)

        def work() -> None:
            user = None
            try:
                user = friends.find_user(query)
                if user is None:
                    friends.record_result(None, "no such user")
                    self.app.ui(lambda: (self._done(), self._set("", "No Roblox user found for that name or ID.", False)))
                    return
                presence = self._presence(asker, user)
                target = friends.resolve_join_target(presence)
                friends.record_result(user.id, "joinable" if target else "unavailable")
                text = friends.describe_presence(presence)
                title = user.name if not user.display_name or user.display_name == user.name \
                    else f"{user.display_name} (@{user.name})"

                def show() -> None:
                    self._done()
                    self.user = user
                    self._set(title, text, target is not None)
                self.app.ui(show)
            except Exception as exc:
                text = friends.describe_error(exc)
                friends.record_result(user.id if user else None, "error", error=text)
                self.app.ui(lambda: (self._done(), self._set("", text, False)))
        self.app.run_bg(work)

    # ---- join ---------------------------------------------------------------
    def join(self) -> None:
        """Asks Roblox again (the user may have moved), then launches there."""
        user = self.user
        if user is None or self.busy:
            return
        if self.mode == "friend":
            accounts = [self.asker] if self.asker is not None else []
        else:
            accounts = self.app.selected_accounts()
        if not accounts:
            messagebox.showinfo("Join", "Select the account to launch in the main window first.", parent=self.dlg)
            return
        asker = accounts[0]
        self._busy_on()
        self.join_btn.configure(state="disabled")
        self.status_var.set("Checking where they are now...")

        def work() -> None:
            try:
                presence = self._presence(asker, user)
                target = friends.resolve_join_target(presence)
                if target is None:
                    friends.record_result(user.id, "unavailable")
                    text = friends.describe_presence(presence)
                    self.app.ui(lambda: (self._done(), self._set(
                        self.name_var.get(), "They are no longer joinable. " + text, False)))
                    return
                place_id, job_id = target
                allowed, skipped = [asker], []
                for other in accounts[1:]:      # public mode only: each extra account must be let in too
                    try:
                        p = self._presence(other, user)
                        ok = friends.resolve_join_target(p) == target
                        why = "" if ok else friends.describe_presence(p)
                    except Exception as exc:
                        ok, why = False, friends.describe_error(exc)
                    if ok:
                        allowed.append(other)
                    else:
                        skipped.append(other)
                        mi_log(f"[!] {other.username}: skipped - cannot join {user.name}'s server. {why}")
                friends.record_result(user.id, "joined")
                self.app.ui(lambda: self._launch(allowed, place_id, job_id, len(skipped)))
            except Exception as exc:
                text = friends.describe_error(exc)
                friends.record_result(user.id, "error", error=text)
                self.app.ui(lambda: (self._done(), self._set(self.name_var.get(), text, False)))
        self.app.run_bg(work)

    def _launch(self, accounts: list, place_id: int, job_id: str, skipped: int) -> None:
        self._done()
        # strict: an account that does not get into this exact server within ~75 s is closed,
        # never retried into another server.
        self.app.launch_accounts(accounts, place_id, job_id, strict=True)
        note = f" ({skipped} account(s) skipped, see the log)" if skipped else ""
        self.app.set_status(f"Joining that user's server with {len(accounts)} account(s)...{note}")
        self.dlg.destroy()


class JoinDialog(tk.Toplevel):
    def __init__(self, app) -> None:
        super().__init__(app.root)
        self.app = app
        self.title("Join tools")
        self.geometry("700x680")
        self.minsize(560, 560)
        self.transient(app.root)
        self._busy = False
        self._place_id: int | None = None
        self._servers: dict[str, sf.Server] = {}

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        finder = ttk.Frame(self.nb, padding=10)
        self.nb.add(finder, text="Server finder")
        self._build_finder(finder)

        friend = ttk.Frame(self.nb, padding=10)
        self.nb.add(friend, text="Join friend")
        self.friend_tab = UserTab(self, friend, "friend")

        public = ttk.Frame(self.nb, padding=10)
        self.nb.add(public, text="Join user")
        self.public_tab = UserTab(self, public, "public")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self._tab_changed())

        self._ps_busy = False
        self._ps_place_id: int | None = None   # the game the list on screen belongs to
        self._ps_found: list = []              # PrivateServer objects (codes live in memory only)
        self._ps_rows: dict[str, ps.PrivateServer] = {}
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
        self.tree.lift()   # created before `body`, so it would sit under it and be invisible
        sb.lift()
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

    def _build_private(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Pick a game and press Load to see the private servers of the selected account "
                               "and of its friends. Select one and press Arm, then launch an account. While armed, "
                               "Launch selected joins that private server and auto-rejoin returns to it (never to a "
                               "public server). Editing the Game or Server box in the main window un-arms it.",
                  style="Muted.TLabel", wraplength=640, justify="left").pack(anchor="w")

        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text="Game:").pack(side="left")
        self.ps_game_var = tk.StringVar(value=self.app.place_var.get())
        # the same picker as the main window's Game box: saved games, place ID / link, or a name to search
        self.ps_picker = GamePicker(row, self.app, self.ps_game_var, on_chosen=self.ps_load)
        self.ps_picker.pack(side="left", padx=(6, 8), fill="x", expand=True)
        self.ps_picker.combobox.bind("<Return>", self._ps_enter)
        self.ps_load_btn = ttk.Button(row, text="Load", command=self.ps_load)
        self.ps_load_btn.pack(side="right")

        row2 = ttk.Frame(parent)
        row2.pack(fill="x", pady=(8, 0))
        self.ps_account_var = tk.StringVar(value="Account: none selected")
        ttk.Label(row2, textvariable=self.ps_account_var, style="Heading.TLabel").pack(side="left")
        self.ps_filter_var = tk.StringVar(value=ps.FILTER_LABELS["all"])
        self.ps_filter_cb = ttk.Combobox(row2, textvariable=self.ps_filter_var, state="readonly", width=16,
                                         values=[ps.FILTER_LABELS[f] for f in ps.FILTERS])
        self.ps_filter_cb.pack(side="right")
        self.ps_filter_cb.bind("<<ComboboxSelected>>", lambda e: self._ps_render())
        ttk.Label(row2, text="Show:").pack(side="right", padx=(0, 6))

        # bottom widgets are packed before the list so the list gets whatever space is left
        bottom = ttk.Frame(parent)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side="right")
        self.ps_arm_btn = ttk.Button(bottom, text="Arm selected server", command=self.ps_arm, state="disabled")
        self.ps_arm_btn.pack(side="right", padx=6)
        self.ps_status_var = tk.StringVar(value="Press Load.")
        ttk.Label(parent, textvariable=self.ps_status_var, wraplength=640, justify="left").pack(
            side="bottom", fill="x", pady=(6, 6))   # packed after `bottom`, so it sits just above the buttons

        body = ttk.Frame(parent)      # the list is a child of this frame (see the note in game_picker.py)
        body.pack(fill="both", expand=True, pady=8)
        cols = ("name", "from", "owner", "players", "joinable")
        self.ps_tree = ttk.Treeview(body, columns=cols, show="headings", selectmode="browse")
        for col, text, width, anchor in (("name", "Server", 220, "w"), ("from", "From", 70, "center"),
                                         ("owner", "Owner", 150, "w"), ("players", "Players", 70, "center"),
                                         ("joinable", "Joinable", 70, "center")):
            self.ps_tree.heading(col, text=text)
            self.ps_tree.column(col, width=width, anchor=anchor)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.ps_tree.yview)
        self.ps_tree.configure(yscrollcommand=sb.set)
        self.ps_tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.ps_tree.bind("<<TreeviewSelect>>", lambda e: self._ps_picked())
        self.ps_tree.bind("<Double-1>", lambda e: self.ps_arm() if self.ps_arm_btn["state"] == "normal" else None)

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

    # ---- tabs -----------------------------------------------------------
    def _tab_changed(self) -> None:
        try:
            current = self.nb.tab(self.nb.select(), "text")
        except tk.TclError:
            return
        if current == "Join friend":
            self.friend_tab.activate()
        else:
            self.friend_tab.deactivate()
        if current == "Private server":
            self._ps_refresh_account()

    def destroy(self) -> None:
        self.friend_tab.deactivate()   # releases the account lock on the main list
        super().destroy()

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
                                                   "type a name and press Search.", parent=self)
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
                                                   "servers and its friends' are listed.", parent=self)
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
        self.destroy()