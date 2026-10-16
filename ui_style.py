"""
Applies theme.py's tokens to tkinter / ttk. Imported by gui.py (and the dialogs); it needs Tk but not
Windows. All numbers (padding, row height, font sizes) come from theme.py.

    pal = ui_style.apply(root, "dark")      # returns the palette dict in use
    ui_style.style_menu(menu, pal)
    ui_style.Tooltip(widget, "text")
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import theme


def apply(root: tk.Misc, mode: str = "dark") -> dict:
    pal = theme.palette(mode)
    s = ttk.Style(root)
    try:
        s.theme_use("clam")  # the only built-in theme whose colours can all be set
    except tk.TclError:
        pass
    sp = theme.space
    base = theme.font()
    root.configure(background=pal["bg"])
    root.option_add("*Font", base)
    root.option_add("*Toplevel.background", pal["bg"])
    root.option_add("*TCombobox*Listbox.background", pal["raised"])
    root.option_add("*TCombobox*Listbox.foreground", pal["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", pal["selection"])
    root.option_add("*TCombobox*Listbox.selectForeground", pal["selectionText"])
    root.option_add("*TCombobox*Listbox.font", base)

    s.configure(".", background=pal["bg"], foreground=pal["text"], font=base, bordercolor=pal["border"],
                lightcolor=pal["border"], darkcolor=pal["border"], troughcolor=pal["surface"],
                focuscolor=pal["accent"], insertcolor=pal["text"], selectbackground=pal["selection"],
                selectforeground=pal["selectionText"])
    s.map(".", foreground=[("disabled", pal["muted"])])

    # ---- containers and text
    s.configure("TFrame", background=pal["bg"])
    s.configure("Card.TFrame", background=pal["surface"], bordercolor=pal["border"], relief="solid",
                borderwidth=1)
    s.configure("Surface.TFrame", background=pal["surface"])
    s.configure("Header.TFrame", background=pal["surface"])
    s.configure("TLabel", background=pal["bg"], foreground=pal["text"])
    s.configure("Card.TLabel", background=pal["surface"], foreground=pal["text"])
    s.configure("Header.TLabel", background=pal["surface"], foreground=pal["text"])
    s.configure("Title.TLabel", background=pal["surface"], font=theme.font(2, "bold"))
    s.configure("Heading.TLabel", font=theme.font(1, "bold"))
    s.configure("CardHeading.TLabel", background=pal["surface"], font=theme.font(1, "bold"))
    s.configure("Muted.TLabel", foreground=pal["muted"], font=theme.font(-1))
    s.configure("CardMuted.TLabel", background=pal["surface"], foreground=pal["muted"], font=theme.font(-1))
    s.configure("Status.TLabel", background=pal["surface"], foreground=pal["muted"], font=theme.font(-1))
    s.configure("Warn.TLabel", background=pal["surface"], foreground=pal["st_busy"], font=theme.font(-1))
    s.configure("TLabelframe", background=pal["bg"], bordercolor=pal["border"], relief="solid", borderwidth=1)
    s.configure("TLabelframe.Label", background=pal["bg"], foreground=pal["muted"], font=theme.font(-1, "bold"))
    s.configure("TSeparator", background=pal["border"])

    # ---- buttons
    pad = (sp(3), sp(1.5))
    s.configure("TButton", background=pal["raised"], foreground=pal["text"], padding=pad, relief="flat",
                borderwidth=1, anchor="center")
    s.map("TButton", background=[("disabled", pal["surface"]), ("pressed", pal["border"]),
                                 ("active", pal["hoverButton"])],
          bordercolor=[("focus", pal["accent"])], foreground=[("disabled", pal["muted"])])
    s.configure("Accent.TButton", background=pal["accent"], foreground=pal["accentText"],
                bordercolor=pal["accent"], lightcolor=pal["accent"], darkcolor=pal["accent"], padding=pad,
                font=theme.font(0, "bold"))
    s.map("Accent.TButton", background=[("disabled", pal["border"]), ("pressed", pal["hoverAccent"]),
                                        ("active", pal["hoverAccent"])],
          foreground=[("disabled", pal["muted"])], bordercolor=[("disabled", pal["border"])])
    s.configure("Ghost.TButton", background=pal["surface"], foreground=pal["text"], bordercolor=pal["surface"],
                lightcolor=pal["surface"], darkcolor=pal["surface"], padding=(sp(2), sp(1.5)))
    s.map("Ghost.TButton", background=[("active", pal["hoverButton"]), ("pressed", pal["border"])],
          bordercolor=[("active", pal["hoverButton"])])
    # destructive actions: always red, never mistaken for a normal button
    s.configure("Danger.TButton", background=pal["tint_bad"], foreground=pal["st_bad"],
                bordercolor=pal["st_bad"], lightcolor=pal["tint_bad"], darkcolor=pal["tint_bad"], padding=pad)
    s.map("Danger.TButton", background=[("disabled", pal["surface"]), ("pressed", pal["hoverDanger"]),
                                        ("active", pal["hoverDanger"])],
          foreground=[("disabled", pal["muted"])], bordercolor=[("disabled", pal["border"])])
    # the two header switches (Multi-Instance / Auto-Rejoin): a soft green or red pill
    for name, key in (("On", "ok"), ("Off", "bad")):
        s.configure(f"{name}.TButton", background=pal["tint_" + key], foreground=pal["st_" + key],
                    bordercolor=pal["st_" + key], lightcolor=pal["tint_" + key], darkcolor=pal["tint_" + key],
                    padding=pad, font=theme.font(0, "bold"))
        s.map(f"{name}.TButton", background=[("active", theme.mix(pal["tint_" + key], pal["st_" + key], .12))])

    # ---- inputs
    field = dict(fieldbackground=pal["raised"], foreground=pal["text"], bordercolor=pal["border"],
                 lightcolor=pal["border"], darkcolor=pal["border"], insertcolor=pal["text"], padding=sp(1.5))
    s.configure("TEntry", **field)
    s.configure("TCombobox", **field, arrowcolor=pal["muted"], background=pal["raised"])
    s.configure("TSpinbox", **field, arrowcolor=pal["muted"], background=pal["raised"])
    for w in ("TEntry", "TCombobox", "TSpinbox"):
        s.map(w, bordercolor=[("focus", pal["accent"])], lightcolor=[("focus", pal["accent"])],
              darkcolor=[("focus", pal["accent"])], fieldbackground=[("readonly", pal["raised"])],
              foreground=[("disabled", pal["muted"])])
    for w in ("TCheckbutton", "TRadiobutton"):
        s.configure(w, background=pal["bg"], foreground=pal["text"], indicatorbackground=pal["raised"],
                    indicatorforeground=pal["accent"], indicatormargin=sp(1))
        s.map(w, background=[("active", pal["bg"])],
              indicatorbackground=[("selected", pal["accent"]), ("active", pal["hoverButton"])])
    s.configure("Card.TCheckbutton", background=pal["surface"])
    s.map("Card.TCheckbutton", background=[("active", pal["surface"])])

    # ---- table
    s.configure("Treeview", background=pal["surface"], fieldbackground=pal["surface"], foreground=pal["text"],
                rowheight=theme.row_height(), borderwidth=0, relief="flat")
    s.map("Treeview", background=[("selected", pal["selection"])], foreground=[("selected", pal["selectionText"])])
    s.configure("Treeview.Heading", background=pal["surface"], foreground=pal["muted"], relief="flat",
                borderwidth=0, padding=(sp(2), sp(2)), font=theme.font(-1, "bold"))
    s.map("Treeview.Heading", background=[("active", pal["raised"])])
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # no inner border

    # ---- scrollbars, tabs
    for orient in ("Vertical", "Horizontal"):
        s.configure(f"{orient}.TScrollbar", background=pal["border"], troughcolor=pal["surface"],
                    bordercolor=pal["surface"], lightcolor=pal["border"], darkcolor=pal["border"],
                    arrowcolor=pal["muted"], relief="flat", gripcount=0)
        s.map(f"{orient}.TScrollbar", background=[("active", pal["muted"])])
    s.configure("TNotebook", background=pal["bg"], bordercolor=pal["border"], tabmargins=(0, sp(1), 0, 0))
    s.configure("TNotebook.Tab", background=pal["bg"], foreground=pal["muted"], padding=(sp(4), sp(2)),
                bordercolor=pal["border"], lightcolor=pal["bg"], darkcolor=pal["bg"])
    s.map("TNotebook.Tab", background=[("selected", pal["bg"])], foreground=[("selected", pal["text"])],
          lightcolor=[("selected", pal["bg"])])
    s.configure("Horizontal.TProgressbar", background=pal["accent"], troughcolor=pal["raised"],
                bordercolor=pal["raised"], lightcolor=pal["accent"], darkcolor=pal["accent"])
    return pal


def style_menu(menu: tk.Menu, pal: dict) -> None:
    menu.configure(background=pal["raised"], foreground=pal["text"], activebackground=pal["selection"],
                   activeforeground=pal["selectionText"], relief="flat", borderwidth=1,
                   disabledforeground=pal["muted"], font=theme.font())


def style_text(widget: tk.Text, pal: dict) -> None:
    widget.configure(background=pal["surface"], foreground=pal["text"], insertbackground=pal["text"],
                     selectbackground=pal["selection"], selectforeground=pal["selectionText"], relief="flat",
                     borderwidth=0, highlightthickness=0, padx=theme.space(3), pady=theme.space(2))


class Tooltip:
    """A small hover label. Appears after 450 ms, hides on leave or click."""

    def __init__(self, widget: tk.Widget, text: str, pal: dict | None = None) -> None:
        self.widget, self.text, self.pal = widget, text, pal or theme.palette("dark")
        self._tip: tk.Toplevel | None = None
        self._job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self._job = self.widget.after(450, self._show)

    def _cancel(self) -> None:
        if self._job:
            self.widget.after_cancel(self._job)
            self._job = None

    def _show(self) -> None:
        if self._tip or not self.text:
            return
        x = self.widget.winfo_rootx() + theme.space(2)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + theme.space(1)
        self._tip = tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(tip, text=self.text, background=self.pal["raised"], foreground=self.pal["text"],
                 relief="solid", borderwidth=1, padx=theme.space(2), pady=theme.space(1),
                 font=theme.font(-1)).pack()

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self._tip:
            self._tip.destroy()
            self._tip = None


class ScrollFrame(ttk.Frame):
    """A vertically scrollable area. Put widgets in `.inner`.

    The scrollbar only shows when the content is taller than the view, the mouse wheel works while the
    pointer is over it, and the inner frame always matches the view's width. With `max_height` the view
    asks for the content's height (up to that cap), so a dialog sizes itself to the content but never
    past the screen; without it the view just fills whatever space it is given (a sidebar)."""

    def __init__(self, parent, pal: dict, *, padding=0, max_height: int | None = None, width: int | None = None):
        super().__init__(parent)
        self.pal, self.max_height = pal, max_height
        self.canvas = tk.Canvas(self, background=pal["bg"], highlightthickness=0, borderwidth=0,
                                width=width or 1, height=1, yscrollincrement=theme.space(5))
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas, padding=padding)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", self._enter, add="+")
            w.bind("<Leave>", self._leave, add="+")

    # ---- geometry
    def _on_inner(self, _e=None) -> None:
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()))
        if self.max_height:
            self.canvas.configure(height=min(self.inner.winfo_reqheight(), self.max_height))
        self._update_bar()

    def _on_canvas(self, e) -> None:
        self.canvas.itemconfigure(self._win, width=e.width)
        self._update_bar()

    def _on_scroll(self, lo, hi) -> None:
        self.bar.set(lo, hi)

    def overflows(self) -> bool:
        return self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1

    def _update_bar(self) -> None:
        if self.overflows():
            if not self.bar.winfo_ismapped():
                self.bar.pack(side="right", fill="y")
        else:
            self.bar.pack_forget()
            self.canvas.yview_moveto(0)

    # ---- mouse wheel, only while the pointer is over this frame
    def _inside(self, widget) -> bool:
        while widget is not None:
            if widget is self:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _enter(self, _e=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _leave(self, e) -> None:
        try:
            under = self.winfo_containing(e.x_root, e.y_root)
        except (KeyError, tk.TclError):
            under = None
        if not self._inside(under):
            self.canvas.unbind_all("<MouseWheel>")

    def _wheel(self, e) -> None:
        if self.overflows():
            notches = int(-e.delta / 120) or (-1 if e.delta > 0 else 1)
            self.canvas.yview_scroll(notches * 3, "units")