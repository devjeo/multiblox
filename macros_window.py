"""
The Macros window (Tk). Imported on demand by gui.py.

All the logic (validation, hotkeys, running) is in macros.py and macro_engine.py; this file is only
the forms. Changes go through app.macro_engine (add / replace / remove / set_enabled), which saves
macros.json and re-registers the hotkeys. The table refreshes by itself twice a second so the
Status column shows "Running (n)" while a macro clicks.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

import macro_engine
import macros as mc

REFRESH_MS = 500
CAPTURE_SECONDS = 3
# Tk key-event state bits on Windows
STATE_SHIFT, STATE_CONTROL, STATE_ALT = 0x1, 0x4, 0x20000


class MacrosWindow(tk.Toplevel):
    _instance: "MacrosWindow | None" = None

    @classmethod
    def show(cls, app) -> None:
        """Opens the window, or brings the open one to the front (there is only ever one)."""
        inst = cls._instance
        if inst is not None and inst.winfo_exists():
            inst.deiconify()
            inst.lift()
            inst.focus_force()
            return
        cls._instance = cls(app)

    def __init__(self, app) -> None:
        super().__init__(app.root)
        self.app, self.engine = app, app.macro_engine
        self.title("Macros")
        self.transient(app.root)
        self.geometry("900x380")
        self._job = None

        top = ttk.Frame(self, padding=(10, 10, 10, 0))
        top.pack(fill="x")
        self.on_var = tk.BooleanVar(value=app.settings["macrosEnabled"])
        ttk.Checkbutton(top, text="Macros on", variable=self.on_var, command=self._master).pack(side="left")
        ttk.Label(top, style="Muted.TLabel",
                  text=f"   Stop every macro at any time: {mc.format_hotkey(mc.PANIC_HOTKEY)}").pack(side="left")

        cols = ("enabled", "name", "hotkey", "type", "position", "repeat", "interval", "runs", "status")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for col, text, width in (("enabled", "On", 36), ("name", "Name", 150), ("hotkey", "Hotkey", 110),
                                 ("type", "Type", 120), ("position", "Target", 130), ("repeat", "Repeat", 80),
                                 ("interval", "Interval", 70), ("runs", "Runs", 45), ("status", "Status", 200)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(8, 4))
        self.tree.bind("<Double-1>", lambda e: self.edit())

        bar = ttk.Frame(self, padding=(10, 4, 10, 10))
        bar.pack(fill="x")
        for text, cmd in (("Add...", self.add), ("Edit...", self.edit), ("Turn on/off", self.toggle),
                          ("Delete", self.delete), ("Test run", self.test_click)):
            ttk.Button(bar, text=text, command=cmd,
                       style="Danger.TButton" if text == "Delete" else "TButton").pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        self.hint = ttk.Label(bar, style="Muted.TLabel")
        self.hint.pack(side="right", padx=10)
        self.refresh()

    def destroy(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        if MacrosWindow._instance is self:
            MacrosWindow._instance = None
        super().destroy()

    # ---- list ---------------------------------------------------------------
    def refresh(self) -> None:
        """Redraws the table. Rows are only rebuilt when the set of macros changed, so a selection and
        the scroll position survive the twice-a-second status update."""
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
        macros = list(self.engine.macros)
        ids = [m.id for m in macros]
        if list(self.tree.get_children()) != ids:
            sel = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            for m in macros:
                self.tree.insert("", "end", iid=m.id, values=self._values(m))
            if sel and self.tree.exists(sel[0]):
                self.tree.selection_set(sel[0])
        else:
            for m in macros:
                self.tree.item(m.id, values=self._values(m))
        self._job = self.after(REFRESH_MS, self.refresh)

    def _values(self, m) -> tuple:
        kind = mc.TYPE_LABELS.get(m.type, m.type)
        if m.type == "recorded":
            kind += f" ({mc.format_speed(m.speed)})" if m.speed != 1 else ""
            target = mc.recording_summary(m)
        else:
            target = f"{m.x}, {m.y}" if m.type == "click_at" else "cursor"
        return ("yes" if m.enabled else "no", m.name, mc.format_hotkey(m.hotkey), kind, target,
                "until stopped" if m.repeat == 0 else str(m.repeat), f"{m.intervalMs} ms", m.runCount,
                self.engine.status(m))

    def _selected(self):
        sel = self.tree.selection()
        return next((m for m in self.engine.macros if sel and m.id == sel[0]), None)

    # ---- actions ------------------------------------------------------------
    def _master(self) -> None:
        self.app.settings["macrosEnabled"] = bool(self.on_var.get())
        try:
            self.app.settings.save()
        except OSError:
            messagebox.showwarning("Macros", "Could not save the setting.", parent=self)
        if not self.on_var.get():
            self.engine.stop_all()

    def add(self) -> None:
        MacroEditor(self, None)

    def edit(self) -> None:
        macro = self._selected()
        if macro:
            MacroEditor(self, macro)

    def toggle(self) -> None:
        macro = self._selected()
        if macro:
            self.engine.set_enabled(macro, not macro.enabled)
            self.refresh()

    def delete(self) -> None:
        macro = self._selected()
        if macro and messagebox.askyesno("Delete macro", f"Delete this macro?\n\n{mc.describe(macro)}", parent=self):
            self.engine.remove(macro)
            self.refresh()

    def test_click(self) -> None:
        macro = self._selected()
        if not macro:
            return
        if self.engine.test_click(macro):
            secs = int(macro_engine.TEST_DELAY_SECONDS)
            if macro.type == "recorded":
                self.hint.configure(text=f"Replaying once in {secs} seconds. Put the mouse where it should start...")
            else:
                where = "at the saved position" if macro.type == "click_at" else "where the mouse is then"
                self.hint.configure(text=f"Clicking once {where} in {secs} seconds...")
        else:
            self.hint.configure(text="That macro is running. Press its hotkey to stop it first.")

    def commit(self, old, new) -> None:
        if old is None:
            self.engine.add(new)
        else:
            self.engine.replace(old, new)
        self.refresh()


class MacroEditor(tk.Toplevel):
    def __init__(self, parent: MacrosWindow, macro) -> None:
        super().__init__(parent)
        self.parent, self.app, self.engine, self.old = parent, parent.app, parent.engine, macro
        base = macro or mc.Macro(name="", hotkey="")
        self.title("Edit macro" if macro else "New macro")
        self.transient(parent)
        self.resizable(True, True)
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)

        self.name = tk.StringVar(value=base.name)
        self.hotkey = tk.StringVar(value=base.hotkey)
        self.type = tk.StringVar(value=mc.TYPE_LABELS[base.type])
        self.button = tk.StringVar(value=base.button)
        self.repeat = tk.StringVar(value=str(base.repeat))
        self.interval = tk.StringVar(value=str(base.intervalMs))
        self.x = tk.StringVar(value="" if base.x is None else str(base.x))
        self.y = tk.StringVar(value="" if base.y is None else str(base.y))
        self._cap_job = None
        self.events = list(base.events)                  # the recording (recorded type)
        self.speed = tk.StringVar(value=mc.format_speed(base.speed))
        self.repeat_label = tk.StringVar()
        self.interval_hint = tk.StringVar()
        self._rec_job = None
        self._recording = False

        r = 0
        ttk.Label(frm, text="Name").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.name, width=30).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="Hotkey").grid(row=r, column=0, sticky="w", pady=3)
        hk = ttk.Frame(frm)
        hk.grid(row=r, column=1, sticky="w")
        self.hotkey_entry = ttk.Entry(hk, width=22, state="readonly")
        self.hotkey_entry.pack(side="left")
        self._show_hotkey()
        ttk.Button(hk, text="Clear", command=lambda: self._set_hotkey("")).pack(side="left", padx=(6, 0))
        # Pressing an existing macro's keys must reach this box, not start that macro.
        self.hotkey_entry.bind("<FocusIn>", lambda e: self.engine.suspend_hotkeys())
        self.hotkey_entry.bind("<FocusOut>", lambda e: self.engine.sync_hotkeys())
        self.hotkey_entry.bind("<KeyPress>", self._on_key)
        r += 1
        ttk.Label(frm, style="Muted.TLabel", text="Click the box, then press the keys (for example Ctrl+Alt+F6).") \
            .grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="Type").grid(row=r, column=0, sticky="w", pady=3)
        self.type_cb = ttk.Combobox(frm, textvariable=self.type, values=list(mc.TYPE_LABELS.values()),
                                    state="readonly", width=20)
        self.type_cb.grid(row=r, column=1, sticky="w")
        self.type_cb.bind("<<ComboboxSelected>>", lambda e: self._type_changed())
        r += 1
        ttk.Label(frm, text="Position (x, y)").grid(row=r, column=0, sticky="w", pady=3)
        pos = ttk.Frame(frm)
        pos.grid(row=r, column=1, sticky="w")
        self.x_entry = ttk.Entry(pos, textvariable=self.x, width=7)
        self.x_entry.pack(side="left")
        self.y_entry = ttk.Entry(pos, textvariable=self.y, width=7)
        self.y_entry.pack(side="left", padx=(4, 8))
        self.capture_btn = ttk.Button(pos, text="Capture position", command=self._capture)
        self.capture_btn.pack(side="left")
        r += 1
        self.capture_hint = ttk.Label(frm, style="Muted.TLabel", wraplength=380, justify="left")
        self.capture_hint.grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="Recording").grid(row=r, column=0, sticky="w", pady=3)
        rec = ttk.Frame(frm)
        rec.grid(row=r, column=1, sticky="w")
        self.record_btn = ttk.Button(rec, text="Record", command=self._record)
        self.record_btn.pack(side="left")
        self.stop_btn = ttk.Button(rec, text="Stop", command=self._stop_button, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.clear_btn = ttk.Button(rec, text="Clear", command=self._clear_recording)
        self.clear_btn.pack(side="left", padx=(6, 0))
        self.rec_widgets = (self.record_btn, self.clear_btn)
        r += 1
        self.rec_hint = ttk.Label(frm, style="Muted.TLabel", wraplength=380, justify="left")
        self.rec_hint.grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="Replay speed").grid(row=r, column=0, sticky="w", pady=3)
        self.speed_cb = ttk.Combobox(frm, textvariable=self.speed, values=[mc.format_speed(v) for v in mc.SPEEDS],
                                     state="readonly", width=10)
        self.speed_cb.grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="Mouse button").grid(row=r, column=0, sticky="w", pady=3)
        self.button_cb = ttk.Combobox(frm, textvariable=self.button, values=list(mc.BUTTONS), state="readonly",
                                      width=10)
        self.button_cb.grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, textvariable=self.repeat_label).grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.repeat, width=10).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, style="Muted.TLabel", wraplength=380, justify="left",
                  text="0 = keep clicking until you press the hotkey again.").grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="Interval (ms)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.interval, width=10).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, style="Muted.TLabel", wraplength=380, justify="left",
                  textvariable=self.interval_hint).grid(row=r, column=1, sticky="w")
        r += 1
        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=self._save).pack(side="right", padx=6)
        self._type_changed()
        self.update_idletasks()
        self.minsize(self.winfo_reqwidth(), self.winfo_reqheight())
        self.grab_set()

    def destroy(self) -> None:
        for job in (self._cap_job, self._rec_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._cap_job = self._rec_job = None
        if self._recording:                 # never leave the input hooks installed
            self._recording = False
            try:
                self.engine.finish_recording()
            except Exception:
                pass
        try:
            self.engine.sync_hotkeys()      # in case the hotkey box still had the focus
        except Exception:
            pass
        super().destroy()

    # ---- hotkey box ---------------------------------------------------------
    def _show_hotkey(self) -> None:
        text = mc.format_hotkey(self.hotkey.get()) if self.hotkey.get() else ""
        self.hotkey_entry.configure(state="normal")
        self.hotkey_entry.delete(0, "end")
        self.hotkey_entry.insert(0, text)
        self.hotkey_entry.configure(state="readonly")

    def _set_hotkey(self, text: str) -> None:
        self.hotkey.set(text)
        self._show_hotkey()

    def _on_key(self, event):
        state = event.state
        ctrl, alt, shift = bool(state & STATE_CONTROL), bool(state & STATE_ALT), bool(state & STATE_SHIFT)
        if event.keysym == "Tab" and not (ctrl or alt or shift):
            return None                         # plain Tab still moves on to the next field
        text = mc.hotkey_from_keypress(event.keysym, ctrl, alt, shift)
        if text is not None:
            self._set_hotkey(text)
        return "break"                          # a bare Ctrl/Alt/Shift press is just waited out

    # ---- position -----------------------------------------------------------
    def _is_click_at(self) -> bool:
        return self.type.get() == mc.TYPE_LABELS["click_at"]

    def _is_recorded(self) -> bool:
        return self.type.get() == mc.TYPE_LABELS["recorded"]

    def _type_changed(self) -> None:
        recorded = self._is_recorded()
        state = "normal" if self._is_click_at() else "disabled"
        for w in (self.x_entry, self.y_entry, self.capture_btn):
            w.configure(state=state)
        self.speed_cb.configure(state="readonly" if recorded else "disabled")
        self.button_cb.configure(state="disabled" if recorded else "readonly")
        for w in self.rec_widgets:
            w.configure(state="normal" if recorded and not self._recording else "disabled")
        self.repeat_label.set("Repeat (replays)" if recorded else "Repeat (clicks)")
        self.interval_hint.set(f"Pause between {'replays' if recorded else 'clicks'}, at least {mc.MIN_INTERVAL_MS} ms.")
        if self._is_click_at():
            self.capture_hint.configure(text="Press the button, then move the mouse to the spot. The position is "
                                             f"read after {CAPTURE_SECONDS} seconds.")
        else:
            self.capture_hint.configure(text="" if recorded else
                                        "This type clicks wherever the mouse is when the macro runs.")
        if not self._recording:
            self._show_recording()

    # ---- recording ----------------------------------------------------------
    def _show_recording(self, text: str | None = None) -> None:
        if text is None:
            if not self._is_recorded():
                text = ""
            elif self.events:
                text = f"Recorded: {mc.events_summary(self.events)}."
            else:
                text = (f"Nothing recorded yet. Press Record, put the mouse where it should start, then do the "
                        f"clicks and keys. Stop with the Stop button or {mc.format_hotkey(mc.PANIC_HOTKEY)}.")
        self.rec_hint.configure(text=text)

    def _record(self) -> None:
        if self._recording:
            return
        for w in self.rec_widgets:
            w.configure(state="disabled")
        self.type_cb.configure(state="disabled")
        self._rec_countdown(CAPTURE_SECONDS)

    def _rec_countdown(self, left: int) -> None:
        if left > 0:
            self._show_recording(f"Recording starts in {left}... put the mouse where it should begin.")
            self._rec_job = self.after(1000, lambda: self._rec_countdown(left - 1))
            return
        self._rec_job = None
        error = self.engine.start_recording(own_hotkey=self.hotkey.get())
        if error:
            self.type_cb.configure(state="readonly")
            self._type_changed()
            messagebox.showwarning("Macro", error, parent=self)
            return
        self._recording = True
        self.stop_btn.configure(state="normal")
        self._poll_recording()

    def _poll_recording(self) -> None:
        info = self.engine.recording_info()
        if info is None or info.full:       # ended by the stop-all hotkey, or a limit was reached
            self._finish_recording(False)
            return
        self._show_recording(f"Recording... {info.count} events, {info.seconds:.0f} s. Stop with the Stop button "
                             f"or {mc.format_hotkey(mc.PANIC_HOTKEY)}.")
        self._rec_job = self.after(200, self._poll_recording)

    def _stop_button(self) -> None:
        self.stop_btn.configure(state="disabled")
        if self._rec_job is not None:       # the polling stops; the finish below replaces it
            self.after_cancel(self._rec_job)
        # a moment later, so the hook has seen the click that pressed Stop and it can be taken out
        self._rec_job = self.after(150, lambda: self._finish_recording(True))

    def _finish_recording(self, trim_click: bool) -> None:
        if self._rec_job is not None:
            try:
                self.after_cancel(self._rec_job)
            except tk.TclError:
                pass
            self._rec_job = None
        events = self.engine.finish_recording(trim_click)
        self._recording = False
        self.stop_btn.configure(state="disabled")
        self.type_cb.configure(state="readonly")
        self._type_changed()
        if events:
            self.events = events
            self._show_recording()
        else:
            self._show_recording("Nothing was recorded. If Roblox runs as administrator, run this app as "
                                 "administrator too. " + ("The earlier recording is kept." if self.events else ""))
        self.deiconify()
        self.lift()

    def _clear_recording(self) -> None:
        self.events = []
        self._show_recording()

    def _capture(self) -> None:
        if self.app.winapi() is None:
            messagebox.showwarning("Macros", "The mouse cannot be read on this system.", parent=self)
            return
        self.capture_btn.configure(state="disabled")
        self._countdown(CAPTURE_SECONDS)

    def _countdown(self, left: int) -> None:
        if left > 0:
            self.capture_hint.configure(text=f"Move the mouse to the spot... {left}")
            self._cap_job = self.after(1000, lambda: self._countdown(left - 1))
            return
        self._cap_job = None
        self.capture_btn.configure(state="normal")
        try:
            x, y = self.app.winapi().get_cursor_pos()
        except OSError:
            self.capture_hint.configure(text="Could not read the mouse position. Type it in instead.")
            return
        self.x.set(str(x))
        self.y.set(str(y))
        self.capture_hint.configure(text=f"Captured position {x}, {y}.")

    # ---- save ---------------------------------------------------------------
    def _number(self, var: tk.StringVar, label: str, allow_empty: bool = False):
        text = var.get().strip()
        if not text and allow_empty:
            return None
        try:
            return int(text)
        except ValueError:
            messagebox.showwarning("Macro", f"{label} must be a whole number.", parent=self)
            raise

    def _save(self) -> None:
        if self._recording:
            messagebox.showwarning("Macro", "Stop the recording first.", parent=self)
            return
        click_at = self._is_click_at()
        recorded = self._is_recorded()
        try:
            repeat = self._number(self.repeat, "Repeat")
            interval = self._number(self.interval, "Interval")
            x = self._number(self.x, "Position x", allow_empty=True) if click_at else None
            y = self._number(self.y, "Position y", allow_empty=True) if click_at else None
        except ValueError:
            return
        old = self.old
        macro = mc.Macro(
            id=old.id if old else mc.Macro().id, name=self.name.get().strip(), hotkey=self.hotkey.get(),
            type="recorded" if recorded else "click_at" if click_at else "click_here", x=x, y=y,
            button=self.button.get(), repeat=repeat, intervalMs=interval, enabled=old.enabled if old else True,
            lastRun=old.lastRun if old else None, runCount=old.runCount if old else 0,
            events=list(self.events) if recorded else [],
            speed=next((v for v in mc.SPEEDS if mc.format_speed(v) == self.speed.get()), 1) if recorded else 1)
        problems = mc.validate_macro(macro, self.engine.macros)
        if problems:
            messagebox.showwarning("Macro", "\n".join(problems), parent=self)
            return
        macro.hotkey = mc.parse_hotkey(macro.hotkey).text
        self.parent.commit(old, macro)
        self.destroy()
