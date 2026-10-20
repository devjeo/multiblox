"""
One account's server history window (right-click an account > Server history...).

Lists every server that account got into, newest first, with the date and time. Pick a row and press
"Join with selected accounts": the accounts selected in the main list go to that exact server (same
path as the Server history card: a running account is closed first, a full server is waited for).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from account_history import Visit
from server_history_panel import short_id, when


class AccountHistoryDialog(tk.Toplevel):
    def __init__(self, app, account) -> None:
        super().__init__(app.root)
        self.app = app
        self.account = account
        self.title(f"Server history: {account.username}")
        self.transient(app.root)
        self.geometry("620x360")
        self._visits: dict[str, Visit] = {}

        ttk.Label(self, text="Pick a server, select the accounts to send in the main window, then press Join.",
                  wraplength=580).pack(anchor="w", padx=10, pady=(10, 4))
        box = ttk.Frame(self)
        box.pack(fill="both", expand=True, padx=10)
        cols = ("when", "game", "server")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", selectmode="browse")
        for col, text, width, stretch in (("when", "When", 110, False), ("game", "Game", 260, True),
                                          ("server", "Server ID", 180, False)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.join())

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=10)
        ttk.Button(bar, text="Join with selected accounts", command=self.join).pack(side="left")
        ttk.Button(bar, text="Clear this account's history", command=self.clear).pack(side="left", padx=8)
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        self.refresh()

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._visits = {}
        for i, v in enumerate(self.app.account_history.list(self.account.userId)):
            iid = str(i)
            self._visits[iid] = v
            self.tree.insert("", "end", iid=iid, values=(
                when(v.at), v.game or f"Place {v.place_id}", short_id(v.job_id)))

    def join(self) -> None:
        sel = self.tree.selection()
        visit = self._visits.get(sel[0]) if sel else None
        if visit is None:
            return
        accounts = self.app.selected_accounts()
        self.app.launch_accounts(accounts, visit.place_id, visit.job_id, strict=True, move=True,
                                 patient=self.app.join_wait.get())

    def _current_keys(self) -> set:
        """Servers this account is in right now: they are never cleared."""
        try:
            live = self.app.manager.live_servers()
        except Exception:
            return set()
        return {k for k, v in live.items() if self.account.username in v["names"]}

    def clear(self) -> None:
        keep = self._current_keys()
        note = " The server it is in right now is kept." if keep else ""
        if self.app.confirm("Clear history", f"Forget the saved servers of {self.account.username}?{note}",
                            parent=self):
            self.app.account_history.clear(self.account.userId, keep=keep)
            self.refresh()
