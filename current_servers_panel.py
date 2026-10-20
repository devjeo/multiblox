"""
Current servers: the "Current" tab of the main window's left panel. Only the servers where one of
your accounts is in right now (confirmed by Roblox's presence, see RejoinManager.live_servers), all
games together. Columns: Game | Server ID | Accounts.

They are also listed in the History tab (in green, at the top). This tab is the short version: no
old servers, nothing to scroll past. A server listed here can not be cleared from the History tab.

Click a row to pick it as the join target, exactly like the History tab: select accounts in the
accounts list and press the Join button. Click the picked row again to un-pick it. The list updates
by itself when an account joins, leaves or teleports.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import theme
import ui_style
from server_history_panel import iid_of, short_id
from server_history import Row


class CurrentServersPanel:
    HINT = "Servers your accounts are in right now. Click one, select accounts, then press the Join button."
    EMPTY = "None of your accounts is in a server right now."

    def __init__(self, app, card: ttk.Frame, wrap: int = 320) -> None:
        self.app = app
        self.wrap = wrap
        self.selected: Row | None = None
        self._live: dict = {}          # {(place, job): {"game", "names"}} as last drawn
        self._build(card)
        self.refresh()

    def _build(self, card: ttk.Frame) -> None:
        sp = theme.space
        self.hint_var = tk.StringVar(value=self.EMPTY)
        hint = ttk.Label(card, textvariable=self.hint_var, style="CardMuted.TLabel", wraplength=self.wrap - sp(24),
                         justify="left")
        hint.pack(fill="x")
        ui_style.autowrap(hint, card, margin=sp(8))

        box = ttk.Frame(card, style="Surface.TFrame")
        box.pack(fill="both", expand=True, pady=(sp(1), 0))
        cols = ("game", "server", "accounts")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", selectmode="browse", height=9)
        for col, text, width, stretch in (("game", "Game", sp(22), True), ("server", "Server ID", sp(29), False),
                                          ("accounts", "Accounts", sp(34), True)):
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, minwidth=sp(14), stretch=stretch, anchor="w")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        ui_style.lock_columns(self.tree)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._picked())
        self.tree.bind("<ButtonPress-1>", self._press, add="+")
        self.tree.bind("<ButtonRelease-1>", self._release, add="+")
        self.tree.bind("<Escape>", lambda e: self.app.set_server_target(None))
        self.tree.bind("<Button-3>", self._right_click)
        self.menu = tk.Menu(self.tree, tearoff=0)
        self.menu.add_command(label="Copy server ID", command=self.copy_id)

    # ---- state --------------------------------------------------------------
    def _alive(self) -> bool:
        try:
            return bool(self.tree.winfo_exists())
        except tk.TclError:
            return False

    def _read_live(self) -> dict:
        try:
            return self.app.manager.live_servers()
        except Exception:   # a convenience view: never break the tab
            return {}

    def retag(self) -> None:
        """Colours; called again when the theme changes."""
        try:
            self.tree.tag_configure("online", foreground=self.app.pal["st_ok"])
        except (tk.TclError, AttributeError, KeyError):
            pass

    def tick(self) -> None:
        """Called once a second by the main window: redraws only when somebody joined or left a server."""
        if self._alive() and self._read_live() != self._live:
            self.refresh()

    def _rows(self) -> list[Row]:
        rows = [Row(k[0], k[1], v["game"], list(v["names"]), 0.0, 0.0) for k, v in self._live.items()]
        rows.sort(key=lambda r: ((r.game or "").lower(), r.job_id))
        return rows

    def refresh(self) -> None:
        if not self._alive():
            return
        self._live = self._read_live()
        rows = self._rows()
        top = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            self.tree.insert("", "end", iid=iid_of(r), tags=("online",), values=(
                r.game or f"Place {r.place_id}", short_id(r.job_id), ", ".join(r.accounts)))
        self.tree.yview_moveto(top)
        if self.selected is not None:
            iid = iid_of(self.selected)
            if self.tree.exists(iid):
                self.tree.selection_set(iid)
            else:
                self.selected = None
        if not rows:
            self.hint_var.set(self.EMPTY)
        elif self.selected is None:
            self.hint_var.set(self._idle_hint())

    def _idle_hint(self) -> str:
        people = sum(len(v["names"]) for v in self._live.values())
        servers = len(self._live)
        return (f"{people} account{'s' if people != 1 else ''} in {servers} server{'s' if servers != 1 else ''} "
                f"right now. " + self.HINT)

    # ---- picking ----------------------------------------------------------------
    def _row_for(self, iid: str) -> Row | None:
        try:
            place, job = iid.split("|", 1)
            key = (int(place), job)
        except ValueError:
            return None
        v = self._live.get(key)
        return Row(key[0], key[1], v["game"], list(v["names"]), 0.0, 0.0) if v else None

    def _press(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        self._press_on_pick = bool(self.selected is not None and iid == iid_of(self.selected))

    def _release(self, event) -> None:
        """Clicking the picked server again un-picks it (so Launch selected is a normal launch)."""
        iid = self.tree.identify_row(event.y)
        if getattr(self, "_press_on_pick", False) and self.selected is not None and iid == iid_of(self.selected):
            self._press_on_pick = False
            self.app.set_server_target(None)

    def _picked(self) -> None:
        sel = self.tree.selection()
        row = self._row_for(sel[0]) if sel else None
        if row is None or (self.selected is not None and self.selected.key == row.key):
            return
        self.selected = row
        self.hint_var.set(f"Picked: {row.game or row.place_id}, server {row.job_id}")
        self.app.set_server_target({"place_id": row.place_id, "job_id": row.job_id, "game": row.game})

    def deselect(self) -> None:
        """Another join target was picked (a player, a private server, a typed game, a History row)."""
        self.selected = None
        if self._alive():
            self.tree.selection_remove(*self.tree.selection())
            self.hint_var.set(self._idle_hint() if self._live else self.EMPTY)

    def _right_click(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def copy_id(self) -> None:
        sel = self.tree.selection()
        row = self._row_for(sel[0]) if sel else None
        if row is not None:
            self.app.root.clipboard_clear()
            self.app.root.clipboard_append(row.job_id)
            self.app.set_status("Server ID copied.")
