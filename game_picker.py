"""
The game picker: ONE widget used by the main window's Game box and by the Private server tab, so
both behave the same.

    [ editable box  v ] [Search]

  * the box takes a saved game's name, a place ID or a roblox.com game link;
  * the drop-down lists the saved games;
  * plain text that is none of those (press Enter or Search) is searched for online, and the
    results are listed in a window with the game's picture, players, rating and creator.

Choosing a result puts it in the box. With "Save to my games" ticked (the default) it is also added
to the saved games, so it shows up in the drop-down of every picker. A game that is already saved
keeps the name you gave it.

All network work runs through app.run_bg and comes back through app.ui. Imports nothing from
gui.py; the app only needs: root, places (PlaceStore), run_bg, ui.
"""

from __future__ import annotations

import base64
import tkinter as tk
from tkinter import ttk

import game_search as gs

ROW_HEIGHT = 56          # tall enough for a 50 px icon
STYLE_NAME = "GameResults.Treeview"


def _tall_rows_style(widget) -> None:
    """A Treeview style with rows tall enough for the pictures. A derived ttk style does not reliably
    inherit a theme's colours (the app's dark theme restyles "Treeview" itself, and the list came up
    blank), so every colour, the state map and the layout are copied from "Treeview" explicitly."""
    style = ttk.Style(widget)
    options = {"rowheight": ROW_HEIGHT, "borderwidth": 0, "relief": "flat"}
    for key in ("background", "fieldbackground", "foreground"):
        value = style.lookup("Treeview", key)
        if value:
            options[key] = value
    style.configure(STYLE_NAME, **options)
    for key in ("background", "foreground"):
        states = style.map("Treeview", key)
        if states:
            style.map(STYLE_NAME, **{key: states})
    layout = style.layout("Treeview")
    if layout:
        style.layout(STYLE_NAME, layout)
    heading = {}
    for key in ("background", "foreground", "relief", "borderwidth", "padding", "font"):
        value = style.lookup("Treeview.Heading", key)
        if value not in ("", None):
            heading[key] = value
    if heading:
        style.configure(STYLE_NAME + ".Heading", **heading)
    states = style.map("Treeview.Heading", "background")
    if states:
        style.map(STYLE_NAME + ".Heading", background=states)


class GamePicker(ttk.Frame):
    def __init__(self, parent, app, textvariable: tk.StringVar | None = None, *, style: str = "TFrame",
                 on_saved=None, on_chosen=None) -> None:
        super().__init__(parent, style=style)
        self.app = app
        self.var = textvariable if textvariable is not None else tk.StringVar()
        self.on_saved = on_saved          # called after a search result was added to the saved games
        self.on_chosen = on_chosen        # called after the user picked a search result
        self.combobox = ttk.Combobox(self, textvariable=self.var, postcommand=self.refresh_values)
        self.combobox.pack(side="left", fill="x", expand=True)
        self.search_btn = ttk.Button(self, text="Search", width=8, command=self.search)
        self.search_btn.pack(side="left", padx=(6, 0))
        self.combobox.bind("<Return>", lambda e: self.search())
        self._dialog: "GameSearchDialog | None" = None
        self.refresh_values()

    # ---- the box ---------------------------------------------------------
    def refresh_values(self) -> None:
        self.combobox["values"] = [p.name for p in self.app.places.places]

    def is_resolved(self) -> bool:
        """True when the box already names a game: a saved game, a place ID or a roblox.com link."""
        raw = self.var.get().strip()
        return bool(raw) and (self.app.places.find(raw) is not None or gs.looks_like_id(raw))

    def search(self) -> None:
        """Opens the online search for the text in the box (or just focuses the existing window).
        Nothing is searched when the box already holds a saved game, a place ID or a link."""
        raw = self.var.get().strip()
        if self.is_resolved():
            return
        try:
            if self._dialog is not None and self._dialog.winfo_exists():
                self._dialog.lift()
                if raw:
                    self._dialog.search_for(raw)
                return
        except tk.TclError:
            pass
        self._dialog = GameSearchDialog(self, raw)

    # ---- a result was chosen ---------------------------------------------------
    def choose(self, game: gs.GameResult, save: bool) -> str:
        """Puts the game in the box; returns what was done, for the status line."""
        places = self.app.places
        existing = next((p for p in places.places if p.placeId == game.place_id), None)
        if existing is not None:                 # keep the name the user gave it
            self.var.set(existing.name)
            return f"Using your saved game {existing.name}."
        if save:
            try:
                place, _ = places.add(game.place_id, game.name)
            except OSError as exc:               # the box still works with a plain ID
                self.var.set(str(game.place_id))
                return f"Could not save the game ({exc.__class__.__name__}); using its place ID."
            self.var.set(place.name)
            self.refresh_values()
            if self.on_saved:
                self.on_saved(place)
            return f"Saved {place.name} and picked it."
        self.var.set(str(game.place_id))
        return f"Using place {game.place_id} (not saved)."


class GameSearchDialog(tk.Toplevel):
    def __init__(self, picker: GamePicker, query: str) -> None:
        super().__init__(picker.winfo_toplevel())
        self.picker, self.app = picker, picker.app
        self.title("Search games")
        self.geometry("640x520")
        self.minsize(500, 380)
        self.transient(picker.winfo_toplevel())
        self._busy = False
        self._token: str | None = None
        self._query = ""
        self._session = gs.new_session_id()
        self._games: dict[str, gs.GameResult] = {}
        self._images: list = []              # PhotoImage objects must be kept alive
        self._generation = 0                 # a new search makes older icon downloads stop
        self._blank = tk.PhotoImage(width=50, height=50)   # keeps rows without a picture aligned

        _tall_rows_style(self)

        top = ttk.Frame(self, padding=(10, 10, 10, 0))
        top.pack(fill="x")
        self.query_var = tk.StringVar(value=query)
        entry = ttk.Entry(top, textvariable=self.query_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self.search_for(self.query_var.get()))
        self.go_btn = ttk.Button(top, text="Search", command=lambda: self.search_for(self.query_var.get()))
        self.go_btn.pack(side="left", padx=(6, 0))

        bottom = ttk.Frame(self, padding=10)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side="right")
        self.use_btn = ttk.Button(bottom, text="Use this game", command=self.use_selected, state="disabled")
        self.use_btn.pack(side="right", padx=6)
        self.more_btn = ttk.Button(bottom, text="More results", command=self.more, state="disabled")
        self.more_btn.pack(side="right")
        self.save_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom, text="Save to my games", variable=self.save_var).pack(side="left")
        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, wraplength=600, justify="left",
                  padding=(10, 6, 10, 0)).pack(side="bottom", fill="x")

        # the holder frame is made first and the table is its child: a table created before the frame
        # and packed into it with in_= ends up UNDER the frame in Tk's stacking order and is invisible
        body = ttk.Frame(self, padding=(10, 8, 10, 0))
        body.pack(fill="both", expand=True)
        cols = ("players", "rating", "creator", "age")
        self.tree = ttk.Treeview(body, columns=cols, show="tree headings", selectmode="browse", style=STYLE_NAME)
        self.tree.heading("#0", text="Game")
        self.tree.column("#0", width=250, anchor="w")
        for col, text, width, anchor in (("players", "Playing", 70, "e"), ("rating", "Rating", 60, "center"),
                                         ("creator", "Creator", 150, "w"), ("age", "Age", 70, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor)
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>",
                       lambda e: self.use_btn.configure(state="normal" if self.tree.selection() else "disabled"))
        self.tree.bind("<Double-1>", lambda e: self.use_selected())
        entry.focus_set()
        if query.strip():
            self.search_for(query)

    # ---- searching -----------------------------------------------------------
    def search_for(self, text: str) -> None:
        text = (text or "").strip()
        self.query_var.set(text)
        if self._busy:
            return
        if not text:
            self._set_status("Type a game name to search for.")
            return
        if gs.looks_like_id(text):
            # an ID or a link needs no search: hand it back to the picker as it is
            self.picker.var.set(text)
            self.destroy()
            return
        self._generation += 1
        self._query, self._token = text, None
        self.tree.delete(*self.tree.get_children())
        self._games, self._images = {}, []
        self.use_btn.configure(state="disabled")
        self._fetch_page("")

    def more(self) -> None:
        if self._token and not self._busy:
            self._fetch_page(self._token)

    def _fetch_page(self, token: str) -> None:
        self._busy = True
        generation, query = self._generation, self._query
        self.go_btn.configure(state="disabled")
        self.more_btn.configure(state="disabled")
        self._set_status(f"Searching for '{query}'...")

        def work() -> None:
            try:
                games, next_token = gs.search_games(query, page_token=token, session_id=self._session)
            except Exception as exc:
                text = gs.describe_error(exc)
                self.app.ui(lambda: self._failed(text))
                return
            self.app.ui(lambda: self._show(games, next_token, generation, first=not token))
            self._load_icons(games, generation)
        self.app.run_bg(work)

    def _finish(self) -> None:
        self._busy = False
        try:
            self.go_btn.configure(state="normal")
        except tk.TclError:
            pass

    def _failed(self, text: str) -> None:
        self._finish()
        self._set_status(text)

    def _show(self, games: list, next_token: str | None, generation: int, first: bool) -> None:
        self._finish()
        if generation != self._generation:
            return
        try:
            for g in games:
                iid = str(g.place_id)
                if iid in self._games:
                    continue
                self._games[iid] = g
                rating = "" if g.rating is None else f"{g.rating}%"
                self.tree.insert("", "end", iid=iid, text=" " + g.name, image=self._blank,
                                 values=(f"{g.players:,}", rating, g.creator, g.age_label))
            self._token = next_token
            self.more_btn.configure(state="normal" if next_token else "disabled")
            if not self._games:
                self._set_status(f"No games found for '{self._query}'.")
            else:
                self._set_status(f"{len(self._games)} games for '{self._query}'. Pick one and press Use this game."
                                 + (" More results are available." if next_token else ""))
                if first and self.tree.get_children():
                    first_id = self.tree.get_children()[0]
                    self.tree.selection_set(first_id)
                    self.tree.focus(first_id)
        except tk.TclError:
            pass

    # ---- pictures (best effort; a game without a picture just shows no icon) ------------
    def _load_icons(self, games: list, generation: int) -> None:
        """Runs in a worker thread."""
        try:
            urls = gs.icon_urls([g.universe_id for g in games])
        except Exception:
            return
        for g in games:
            if generation != self._generation:
                return
            url = urls.get(g.universe_id)
            if not url:
                continue
            try:
                data = gs.download(url)
            except Exception:
                continue
            if data:
                self.app.ui(lambda g=g, data=data: self._set_icon(g, data, generation))

    def _set_icon(self, game: gs.GameResult, data: bytes, generation: int) -> None:
        if generation != self._generation:
            return
        try:
            image = tk.PhotoImage(data=base64.b64encode(data).decode("ascii"))
            iid = str(game.place_id)
            if self.tree.exists(iid):
                self._images.append(image)
                self.tree.item(iid, image=image)
        except tk.TclError:
            pass

    # ---- choosing ---------------------------------------------------------------
    def use_selected(self) -> None:
        sel = self.tree.selection()
        game = self._games.get(sel[0]) if sel else None
        if game is None:
            return
        note = self.picker.choose(game, self.save_var.get())
        self.app.set_status(note)
        picker = self.picker
        self.destroy()
        if picker.on_chosen:
            picker.after(0, picker.on_chosen)

    def _set_status(self, text: str) -> None:
        try:
            self.status_var.set(text)
        except tk.TclError:
            pass