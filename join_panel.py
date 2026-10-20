"""
Join panel: the "Player" tab of the main window's left panel.

How it works
  1. Pick a player: type a username / ID / profile link and press Look up, click someone in the
     History list (every player you actually JOINED before; looking someone up does not add them),
     or switch to Online friends. Picking only SELECTS the player as the join target; nothing launches.
  2. Press the normal "Launch selected" button (it is renamed "Join <player>" while a target is
     set; Enter, double-click and the right-click Launch do the same). Every selected account that
     Roblox lets into that player's server joins it. An account that is already playing is closed
     completely first and then rejoins in that exact server (RejoinManager.hop, by job ID).
  3. The target is dropped by itself once a join has started, so the next Launch selected is a normal
     launch from the Game box. To drop it earlier press "Clear player (launch normally)", press Esc,
     or click the picked row again.

Privacy: Roblox only shows a user's server to accounts allowed to see it. When it is hidden the
player is shown as unavailable and nothing is launched. Network work runs through app.run_bg and
results come back through app.ui. Cookies live only inside the worker call that needs them.
"""

from __future__ import annotations

import time
import tkinter as tk
from tkinter import messagebox, ttk

import friends
import theme
import ui_style
from join_history import Entry, JoinHistory
from multi_instance import log as mi_log


def ago(then: float, now: float | None = None) -> str:
    """'just now', '5 min ago', '3 h ago', '2 d ago' for the History list."""
    secs = max(0, int((now if now is not None else time.time()) - then))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


class JoinPanel:
    def __init__(self, app, card: ttk.Frame, history: JoinHistory | None = None, wrap: int | None = None) -> None:
        self.app = app
        self.wrap = wrap
        self.history = history if history is not None else JoinHistory()
        self.target: friends.User | None = None
        self.joinable: bool | None = None      # None = not checked yet
        self.busy = False
        self.friend_entries: dict[str, friends.FriendEntry] = {}
        self.friends_loaded_for: str | None = None   # account id the Online friends list belongs to
        self._build(card)
        self._render()

    # ---- UI -------------------------------------------------------------
    def _build(self, card: ttk.Frame) -> None:
        sp = theme.space
        wrap = self.wrap or sp(62)
        intro = ttk.Label(card, text="Pick a player, then press the Join button in Actions. Every selected account "
                                     "that Roblox lets into their server joins; a running account is closed first. "
                                     "Players you join are saved in History.",
                          style="CardMuted.TLabel", wraplength=wrap, justify="left")
        intro.pack(anchor="w")
        ui_style.autowrap(intro, card)

        row = ttk.Frame(card, style="Surface.TFrame")
        row.pack(fill="x", pady=(sp(2), 0))
        self.query_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.query_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.lookup())
        self.lookup_btn = ttk.Button(row, text="Look up", command=self.lookup)
        self.lookup_btn.pack(side="left", padx=(sp(1), 0))
        ttk.Label(card, text="Username, user ID or profile link", style="CardMuted.TLabel").pack(anchor="w")

        tabs = ttk.Frame(card, style="Surface.TFrame")
        tabs.pack(fill="x", pady=(sp(3), sp(1)))
        self.source = tk.StringVar(value="history")
        for value, text in (("history", "History"), ("friends", "Online friends")):
            ttk.Radiobutton(tabs, text=text, value=value, variable=self.source, style="Toolbutton",
                            command=self._source_changed).pack(side="left", padx=(0, sp(1)))

        box = ttk.Frame(card, style="Surface.TFrame")
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=("name", "info"), show="headings", selectmode="browse", height=8)
        self.tree.heading("name", text="Player", anchor="w")
        self.tree.heading("info", text="", anchor="w")
        self.tree.column("name", width=sp(34), minwidth=sp(20), stretch=True, anchor="w")
        self.tree.column("info", width=sp(22), minwidth=sp(12), stretch=False, anchor="w")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        ui_style.lock_columns(self.tree)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._picked())
        self.tree.bind("<ButtonPress-1>", self._press, add="+")
        self.tree.bind("<ButtonRelease-1>", self._release, add="+")
        self.tree.bind("<Escape>", lambda e: self.clear_target())
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Button-3>", self._right_click)
        self.menu = tk.Menu(self.tree, tearoff=0)
        self.menu.add_command(label="Remove from history", command=self.remove_selected)

        tools = ttk.Frame(card, style="Surface.TFrame")
        tools.pack(fill="x", pady=(sp(1), 0))
        self.refresh_btn = ttk.Button(tools, text="Refresh friends", command=self.load_friends)
        self.clear_hist_btn = ttk.Button(tools, text="Clear history", style="Ghost.TButton",
                                         command=self.clear_history)
        self.count_var = tk.StringVar()
        ttk.Label(tools, textvariable=self.count_var, style="CardMuted.TLabel").pack(side="right")

        ttk.Separator(card).pack(fill="x", pady=(sp(2), sp(2)))
        self.name_var = tk.StringVar(value="No player picked")
        name_lab = ttk.Label(card, textvariable=self.name_var, style="CardHeading.TLabel", wraplength=wrap,
                             justify="left")
        name_lab.pack(anchor="w")
        ui_style.autowrap(name_lab, card)
        self.status_var = tk.StringVar(value="Pick someone from the list, or look them up.")
        status_lab = ttk.Label(card, textvariable=self.status_var, style="CardMuted.TLabel", wraplength=wrap,
                               justify="left")
        status_lab.pack(anchor="w", pady=(sp(0.5), 0))
        ui_style.autowrap(status_lab, card)
        wait = ttk.Checkbutton(card, text="Stay and wait if the server is full", variable=self.app.join_wait)
        wait.pack(anchor="w", pady=(sp(1.5), 0))
        ui_style.Tooltip(wait, "The Roblox client is left open and never closed by this app while it waits to "
                               "get into that server, for as long as it takes, until you press Stop. Unticked, an "
                               "account that cannot get in is closed after about 75 s.", self.app.pal)
        self.clear_btn = ttk.Button(card, text="Clear player (launch normally)", style="Ghost.TButton",
                                    command=self.clear_target)
        self.clear_btn.pack(anchor="w", pady=(sp(1), 0))
        self.clear_btn.configure(state="disabled")

    # ---- list -------------------------------------------------------------
    def _alive(self) -> bool:
        try:
            return bool(self.tree.winfo_exists())
        except tk.TclError:
            return False

    def _source_changed(self) -> None:
        self._render()
        if self.source.get() == "friends" and self.friends_loaded_for is None and self.app.selected_accounts():
            self.load_friends()

    def _render(self) -> None:
        if not self._alive():
            return
        self.tree.delete(*self.tree.get_children())
        if self.source.get() == "history":
            self.refresh_btn.pack_forget()
            self.clear_hist_btn.pack(side="left")
            self.tree.heading("info", text="Last used")
            for e in self.history.entries:
                self.tree.insert("", "end", iid=str(e.user_id), values=(e.title, ago(e.last_used)))
            self.count_var.set(f"{len(self.history.entries)} saved")
            if not self.history.entries and self.target is None:
                self.status_var.set("History is empty. Players you join appear here.")
        else:
            self.clear_hist_btn.pack_forget()
            self.refresh_btn.pack(side="left")
            self.tree.heading("info", text="Status")
            for uid, e in self.friend_entries.items():
                self.tree.insert("", "end", iid=uid, values=(e.title, e.status))
            self.count_var.set(f"{len(self.friend_entries)} online" if self.friends_loaded_for else "")
        if self.target is not None and self.tree.exists(str(self.target.id)):
            self.tree.selection_set(str(self.target.id))

    def _right_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid and self.source.get() == "history":
            self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def remove_selected(self) -> None:
        if self.source.get() != "history":
            return
        for iid in self.tree.selection():
            self.history.remove(int(iid))
        self._render()

    def clear_history(self) -> None:
        if self.history.entries and self.app.confirm("Clear history?", "Forget every saved player?"):
            self.history.clear()
            self._render()

    def _press(self, event) -> None:
        """Remembers whether the click lands on the row that is already the target."""
        iid = self.tree.identify_row(event.y)
        self._press_on_target = bool(self.target is not None and iid == str(self.target.id))

    def _release(self, event) -> None:
        """Clicking the picked player again un-picks them (so Launch selected is a normal launch)."""
        iid = self.tree.identify_row(event.y)
        if getattr(self, "_press_on_target", False) and self.target is not None and iid == str(self.target.id):
            self._press_on_target = False
            self.clear_target()

    def _picked(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        if self.source.get() == "friends":
            entry = self.friend_entries.get(iid)
            if entry is None:
                return
            self.set_target(entry.user, friends.describe_presence(entry.presence), entry.joinable)
        else:
            entry = self.history.get(int(iid))
            if entry is None:
                return
            user = friends.User(entry.user_id, entry.name, entry.display_name)
            if self.target is not None and self.target.id == user.id:
                return   # already the target (the list was redrawn): do not ask Roblox again
            self.set_target(user, "Checking where they are...", None)
            self.check_status(user)

    # ---- target ------------------------------------------------------------
    def set_target(self, user: friends.User, status: str, joinable: bool | None) -> None:
        self.target, self.joinable = user, joinable
        self.name_var.set(_title(user))
        self.status_var.set(status)
        self.clear_btn.configure(state="normal")
        self.app.on_join_target_changed(user)

    def clear_target(self, notify: bool = True) -> None:
        self.target, self.joinable = None, None
        if self._alive():
            self.name_var.set("No player picked")
            self.status_var.set("Pick someone from the list, or look them up.")
            self.clear_btn.configure(state="disabled")
            self.tree.selection_remove(*self.tree.selection())
        if notify:
            self.app.on_join_target_changed(None)

    def _presence(self, account, user: friends.User):
        """Runs in a worker. The cookie lives only inside this call."""
        cookie = self.app.store.get_cookie(account)
        try:
            return friends.get_presence(cookie, user.id)
        finally:
            del cookie

    def _set_status(self, user: friends.User, text: str, joinable: bool | None) -> None:
        """Applies a late result, but only if that player is still the target."""
        if self.target is None or self.target.id != user.id:
            return
        self.joinable = joinable
        try:
            self.status_var.set(text)
        except tk.TclError:
            pass

    def check_status(self, user: friends.User) -> None:
        """Advisory: asks Roblox where the player is now. Joining asks again anyway."""
        accounts = self.app.selected_accounts()
        if not accounts:
            self._set_status(user, "Select an account in the list to check where they are.", None)
            return
        asker = accounts[0]

        def work() -> None:
            try:
                presence = self._presence(asker, user)
                target = friends.resolve_join_target(presence)
                text = friends.describe_presence(presence)
                self.app.ui(lambda: self._set_status(user, text, target is not None))
            except Exception as exc:
                text = friends.describe_error(exc)
                self.app.ui(lambda: self._set_status(user, text, None))
        self.app.run_bg(work)

    # ---- look up -------------------------------------------------------------
    def lookup(self) -> None:
        query = self.query_var.get().strip()
        if not query or self.busy:
            return
        self.busy = True
        self.lookup_btn.configure(state="disabled")
        self.status_var.set("Looking up...")
        accounts = self.app.selected_accounts()
        asker = accounts[0] if accounts else None

        def work() -> None:
            user = None
            try:
                user = friends.find_user(query)
                if user is None:
                    friends.record_result(None, "no such user")
                    self.app.ui(lambda: self._lookup_done(None, "No Roblox user found for that name or ID."))
                    return
                if asker is None:
                    self.app.ui(lambda: self._lookup_done(user, "Select an account in the list to check where they are.", None))
                    return
                presence = self._presence(asker, user)
                target = friends.resolve_join_target(presence)
                friends.record_result(user.id, "joinable" if target else "unavailable")
                text = friends.describe_presence(presence)
                self.app.ui(lambda: self._lookup_done(user, text, target is not None))
            except Exception as exc:
                text = friends.describe_error(exc)
                friends.record_result(user.id if user else None, "error", error=text)
                self.app.ui(lambda: self._lookup_done(user, text, None))
        self.app.run_bg(work)

    def _lookup_done(self, user: friends.User | None, text: str, joinable: bool | None = None) -> None:
        self.busy = False
        if not self._alive():
            return
        self.lookup_btn.configure(state="normal")
        if user is None:
            self.status_var.set(text)
            return
        # not saved to History here: only a player you actually join is remembered (see _launch)
        self.query_var.set("")
        self.source.set("history")
        self.set_target(user, text, joinable)
        self._render()

    # ---- online friends --------------------------------------------------------
    def load_friends(self) -> None:
        if self.busy:
            return
        accounts = self.app.selected_accounts()
        if not accounts:
            messagebox.showinfo("Online friends", "Select an account in the list first. Its friends are listed.",
                                parent=self.app.root)
            return
        asker = accounts[0]
        self.busy = True
        self.refresh_btn.configure(state="disabled")
        self.count_var.set("Loading...")

        def work() -> None:
            try:
                cookie = self.app.store.get_cookie(asker)
                try:
                    entries = friends.online_friends(cookie, asker.userId)
                finally:
                    del cookie
                friends.record_result(None, f"listed {len(entries)} online")
                self.app.ui(lambda: self._friends_done(asker, entries, None))
            except Exception as exc:
                text = friends.describe_error(exc)
                friends.record_result(None, "error", error=text)
                self.app.ui(lambda: self._friends_done(asker, [], text))
        self.app.run_bg(work)

    def _friends_done(self, asker, entries: list, error: str | None) -> None:
        self.busy = False
        if not self._alive():
            return
        self.refresh_btn.configure(state="normal")
        if error:
            self.count_var.set("")
            self.status_var.set(error)
            return
        self.friends_loaded_for = asker.id
        self.friend_entries = {str(e.user.id): e for e in entries}
        if self.source.get() == "friends":
            self._render()
        joinable = sum(1 for e in entries if e.joinable)
        self.count_var.set(f"{asker.username}: {len(entries)} online, {joinable} joinable")
        if not entries:
            self.status_var.set("None of this account's friends are online right now.")

    # ---- join (called by the main window's Launch selected) -----------------------
    def join(self, accounts: list) -> None:
        """Asks Roblox again (the player may have moved), then launches there. Accounts that are
        already running are closed completely and rejoin in that exact server."""
        user = self.target
        if user is None or self.busy:
            return
        if not accounts:
            messagebox.showinfo("Join", "Select one or more accounts in the list first. They are the "
                                        "accounts that join.", parent=self.app.root)
            return
        asker = accounts[0]
        self.busy = True
        self.status_var.set("Checking where they are now...")

        def work() -> None:
            try:
                presence = self._presence(asker, user)
                target = friends.resolve_join_target(presence)
                if target is None:
                    friends.record_result(user.id, "unavailable")
                    text = "They are not joinable right now. " + friends.describe_presence(presence)
                    self.app.ui(lambda: self._join_blocked(user, text))
                    return
                place_id, job_id = target
                allowed, skipped = [asker], 0
                for other in accounts[1:]:      # every extra account must be let into the same server
                    try:
                        p = self._presence(other, user)
                        ok = friends.resolve_join_target(p) == target
                        why = "" if ok else friends.describe_presence(p)
                    except Exception as exc:
                        ok, why = False, friends.describe_error(exc)
                    if ok:
                        allowed.append(other)
                    else:
                        skipped += 1
                        mi_log(f"[!] {other.username}: skipped - cannot join {user.name}'s server. {why}")
                friends.record_result(user.id, "joined")
                self.app.ui(lambda: self._launch(user, allowed, place_id, job_id, skipped))
            except Exception as exc:
                text = friends.describe_error(exc)
                friends.record_result(user.id, "error", error=text)
                self.app.ui(lambda: self._join_blocked(user, text))
        self.app.run_bg(work)

    def _join_blocked(self, user: friends.User, text: str) -> None:
        self.busy = False
        self._set_status(user, text, False)

    def _launch(self, user: friends.User, accounts: list, place_id: int, job_id: str, skipped: int) -> None:
        self.busy = False
        self.history.add(user.id, user.name, user.display_name)
        if self.source.get() == "history":
            self._render()
        note = f" ({skipped} account(s) skipped, see the log)" if skipped else ""
        self._set_status(user, f"Joining with {len(accounts)} account(s){note}.", True)
        # strict: an account is never sent to another server. patient (checkbox): the client is left open
        # and waits as long as it takes (no timeout, nothing closed) until it gets in or Stop; unchecked,
        # it is closed after ~75 s.
        # move: a running account is closed completely first, then rejoins.
        self.app.launch_accounts(accounts, place_id, job_id, strict=True, move=True,
                                 patient=self.app.join_wait.get())
        self.app.set_status(f"Joining {user.name}'s server with {len(accounts)} account(s)...{note}")
        self.clear_target()   # done: the next Launch selected is a normal launch, not another join


def _title(user: friends.User) -> str:
    return Entry(user.id, user.name, user.display_name).title
