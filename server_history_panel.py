"""
Server history: the "History" tab of the main window's left panel. Every server any account got into,
all games together, newest first. Columns: Game | Server ID | When | Accounts.

Click a row to pick that server as the join target (nothing launches). Select accounts in the
accounts list and press the normal Launch selected button, which now reads "Join <game>": every selected
account goes to that exact server; one that is already playing is closed completely first.
If the server is full, the account's client is left open and waits as long as it takes (the "Stay and
wait if the server is full" box in the Join panel; unticked, an account that cannot get in is closed
after about 75 s). Press Stop to end the wait. A server that no longer exists waits until Stop too.
Once the join has started the pick is dropped by itself; click the picked row again to drop it earlier.
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import ttk

import theme
import ui_style
from server_history import Row, ServerHistory

ONLINE_DOT = "\u25CF"


def short_id(job_id: str) -> str:
    return job_id if len(job_id) <= 13 else job_id[:8] + "…" + job_id[-4:]


def when(ts: float) -> str:
    """Local time like 'Oct 19 13:28'."""
    try:
        return datetime.fromtimestamp(ts).strftime("%b %d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def iid_of(row: Row) -> str:
    return f"{row.place_id}|{row.job_id}"


class ServerHistoryPanel:
    HINT = "Click a server, select accounts in the list, then press the Join button. Click it again to un-pick."

    def __init__(self, app, card: ttk.Frame, history: ServerHistory, wrap: int = 320) -> None:
        self.app = app
        self.wrap = wrap
        self.history = history
        self.selected: Row | None = None
        self._live: dict = {}         # {(place, job): {"game", "names"}} as last drawn
        self._extra: dict = {}        # online servers that are not in the saved list (history was cleared)
        self._build(card)
        self.retag()
        self.refresh()

    def _build(self, card: ttk.Frame) -> None:
        sp = theme.space
        top = ttk.Frame(card, style="Surface.TFrame")
        top.pack(fill="x")
        self.hint_var = tk.StringVar(value=self.HINT)
        self.clear_btn = ttk.Button(top, text="Clear all", style="Ghost.TButton", width=9, command=self.clear)
        self.clear_btn.pack(side="right", anchor="n")
        hint = ttk.Label(top, textvariable=self.hint_var, style="CardMuted.TLabel", wraplength=self.wrap - sp(24),
                         justify="left")
        hint.pack(side="left")
        ui_style.autowrap(hint, top, margin=sp(32))   # leaves room for the Clear all button

        box = ttk.Frame(card, style="Surface.TFrame")
        box.pack(fill="both", expand=True, pady=(sp(1), 0))
        cols = ("game", "server", "when", "accounts")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", selectmode="browse", height=9)
        for col, text, width, stretch in (("game", "Game", sp(18), True), ("server", "Server ID", sp(29), False),
                                          ("when", "When", sp(23), False), ("accounts", "Accounts", sp(30), True)):
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
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Button-3>", self._right_click)
        self.menu = tk.Menu(self.tree, tearoff=0)
        self.menu.add_command(label="Copy server ID", command=self.copy_id)
        self.menu.add_command(label="Remove from history", command=self.remove_selected)   # not for online servers

    # ---- live state: which accounts are in which server right now ---------------
    def _idle_hint(self) -> str:
        servers = len(self._live)
        if not servers:
            return self.HINT
        people = sum(len(v["names"]) for v in self._live.values())
        return (f"{ONLINE_DOT} {people} account{'s' if people != 1 else ''} online now in {servers} "
                f"server{'s' if servers != 1 else ''} (green, at the top). " + self.HINT)

    def _read_live(self) -> dict:
        try:
            return self.app.manager.live_servers()
        except Exception:   # a convenience view: never break the tab
            return {}

    def retag(self) -> None:
        """Colours for the online rows; called again when the theme changes."""
        try:
            self.tree.tag_configure("online", foreground=self.app.pal["st_ok"])
        except (tk.TclError, AttributeError, KeyError):
            pass

    def tick(self) -> None:
        """Called once a second by the main window: redraws only when somebody joined or left a server."""
        if self._alive() and self._read_live() != self._live:
            self.refresh()

    # ---- list -------------------------------------------------------------
    def _alive(self) -> bool:
        try:
            return bool(self.tree.winfo_exists())
        except tk.TclError:
            return False

    def refresh(self) -> None:
        """Redraws from the store; keeps the picked row selected if it is still there."""
        if not self._alive():
            return
        rows = self.history.list()
        live = self._live = self._read_live()
        known = {r.key for r in rows}
        # an online server the saved list does not have (it was removed or cleared while the account is
        # still in it) is still shown, so the tab never hides where an account is
        self._extra = {k: Row(k[0], k[1], v["game"], list(v["names"]), 0.0, 0.0)
                       for k, v in live.items() if k not in known}
        rows = list(self._extra.values()) + rows
        rows.sort(key=lambda r: r.key not in live)   # online servers first; otherwise newest first (stable)
        top = self.tree.yview()[0]
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            here = live.get(r.key)
            if here:
                others = len([a for a in r.accounts if a not in here["names"]])
                who = ", ".join(here["names"]) + (f"  (+{others} earlier)" if others else "")
                values = (r.game or f"Place {r.place_id}", short_id(r.job_id), f"{ONLINE_DOT} Online now", who)
                tags = ("online",)
            else:
                values = (r.game or f"Place {r.place_id}", short_id(r.job_id), when(r.last_at),
                          ", ".join(r.accounts))
                tags = ()
            self.tree.insert("", "end", iid=iid_of(r), values=values, tags=tags)
        self.tree.yview_moveto(top)
        if self.selected is not None:
            iid = iid_of(self.selected)
            if self.tree.exists(iid):
                self.tree.selection_set(iid)
            else:
                self.selected = None
        self.clear_btn.configure(state="normal" if any(r.key not in live for r in self.history.list())
                                 else "disabled")
        if not rows:
            self.hint_var.set("Nothing yet. Servers your accounts join are listed here, for every game.")
        elif self.selected is None:
            self.hint_var.set(self._idle_hint())

    def _row_for(self, iid: str) -> Row | None:
        try:
            place, job = iid.split("|", 1)
            return self.history.get(int(place), job) or self._extra.get((int(place), job))
        except ValueError:
            return None

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
        """Another join target was picked (a player, a private server, a typed game)."""
        self.selected = None
        if self._alive():
            self.tree.selection_remove(*self.tree.selection())
            if self.history.list() or self._extra:
                self.hint_var.set(self._idle_hint())

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

    def remove_selected(self) -> None:
        sel = self.tree.selection()
        row = self._row_for(sel[0]) if sel else None
        if row is None:
            return
        live = self._read_live()
        if row.key in live:
            self.app.set_status("Can't remove it: one of your accounts is in that server right now.")
            return
        if self.selected is not None and self.selected.key == row.key:
            self.selected = None
            self.app.set_server_target(None)
        self.history.remove(row.place_id, row.job_id, keep=set(live))
        self.refresh()

    def clear(self) -> None:
        """Clears the saved servers, except the ones where your accounts are right now (they stay)."""
        live = set(self._read_live())
        if not any(r.key not in live for r in self.history.list()):
            return
        kept = sum(1 for r in self.history.list() if r.key in live)
        note = (f" The {kept} server{'s' if kept != 1 else ''} where your accounts are right now "
                f"will be kept.") if kept else ""
        if self.app.confirm("Clear server history?", "Forget every saved server?" + note):
            if self.selected is not None and self.selected.key not in live:
                self.selected = None
                self.app.set_server_target(None)
            self.history.clear(keep=live)
            self.refresh()
