"""
Join tools window. Today it holds one tab, "Server finder" (Feature C1); the friend
and private-server tabs are added to the same Notebook by later phases.

Imported only when the "Find server..." button is pressed, so gui.py stays light.
All network work runs through app.run_bg and results come back through app.ui.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

import server_finder as sf


class JoinDialog(tk.Toplevel):
    def __init__(self, app) -> None:
        super().__init__(app.root)
        self.app = app
        self.title("Find a server")
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

    # ---- UI -------------------------------------------------------------
    def _build_finder(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Looks at the game in the main window's Game box (or the selected account's "
                               "last game) and lists its public servers.",
                  foreground="#777777", wraplength=560, justify="left").pack(anchor="w")
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