"""
The Schedules window (Tk). Imported on demand by gui.py.

All the logic (validation, due-time rules, running) is in scheduler.py; this file is only the
forms. Edits go to app.scheduler.rules and are saved through app.scheduler.save().
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

import groups
import scheduler
from places import parse_place_input


class ScheduleWindow(tk.Toplevel):
    def __init__(self, app) -> None:
        super().__init__(app.root)
        self.app = app
        self.title("Schedules")
        self.transient(app.root)
        self.geometry("760x340")

        cols = ("enabled", "target", "action", "time", "days", "game", "last")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="browse")
        for col, text, width in (("enabled", "On", 40), ("target", "Target", 150), ("action", "Action", 60),
                                 ("time", "Time", 55), ("days", "Days", 130), ("game", "Game", 110),
                                 ("last", "Last run", 150)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        self.tree.bind("<Double-1>", lambda e: self.edit())

        bar = ttk.Frame(self, padding=(10, 4, 10, 10))
        bar.pack(fill="x")
        for text, cmd in (("Add...", self.add), ("Edit...", self.edit), ("Turn on/off", self.toggle),
                          ("Delete", self.delete), ("Run now", self.run_now)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Close", command=self.destroy).pack(side="right")
        self.hint = ttk.Label(bar, foreground="#777777")
        self.hint.pack(side="right", padx=10)
        self.refresh()

    # ---- list ---------------------------------------------------------------
    def refresh(self) -> None:
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for rule in self.app.scheduler.rules:
            kind = "account" if rule.targetType == "account" else "group"
            game = str(rule.placeId) if rule.placeId else ("-" if rule.action == "stop" else "default")
            self.tree.insert("", "end", iid=rule.id, values=(
                "yes" if rule.enabled else "no", f"{kind}: {rule.target}", rule.action, rule.time,
                scheduler.format_days(rule.days), game, (rule.lastRun or "never").replace("T", " ")))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        window = self.app.settings["scheduleMissedWindowMinutes"]
        self.hint.configure(text=f"A run missed while the app was closed or asleep still fires within {window} min.")

    def _selected(self):
        sel = self.tree.selection()
        return next((r for r in self.app.scheduler.rules if sel and r.id == sel[0]), None)

    # ---- actions ------------------------------------------------------------
    def add(self) -> None:
        RuleEditor(self, None)

    def edit(self) -> None:
        rule = self._selected()
        if rule:
            RuleEditor(self, rule)

    def toggle(self) -> None:
        rule = self._selected()
        if rule:
            rule.enabled = not rule.enabled
            self.app.scheduler.save()
            self.refresh()

    def delete(self) -> None:
        rule = self._selected()
        if rule and messagebox.askyesno("Delete schedule", f"Delete this rule?\n\n{scheduler.Scheduler.describe(rule)}",
                                        parent=self):
            self.app.scheduler.rules.remove(rule)
            self.app.scheduler.save()
            self.refresh()

    def run_now(self) -> None:
        rule = self._selected()
        if rule:
            self.app.scheduler.run_now(rule)

    def commit(self, old, new) -> None:
        rules = self.app.scheduler.rules
        if old is None:
            rules.append(new)
        else:
            rules[rules.index(old)] = new
        self.app.scheduler.save()
        self.refresh()


class RuleEditor(tk.Toplevel):
    def __init__(self, parent: ScheduleWindow, rule) -> None:
        super().__init__(parent)
        self.parent, self.app, self.old = parent, parent.app, rule
        base = rule or scheduler.Rule()
        self.title("Edit schedule" if rule else "New schedule")
        self.transient(parent)
        self.resizable(False, False)
        frm = ttk.Frame(self, padding=14)
        frm.pack()

        self.action = tk.StringVar(value=base.action)
        self.ttype = tk.StringVar(value=base.targetType)
        self.target = tk.StringVar(value=base.target)
        self.time = tk.StringVar(value=base.time)
        saved_place = self.app.places.find(str(base.placeId)) if base.placeId else None
        self.game = tk.StringVar(value=(saved_place.name if saved_place else str(base.placeId or "")))
        self.job = tk.StringVar(value=base.jobId or "")
        self.days = [tk.BooleanVar(value=i in base.days) for i in range(7)]

        r = 0
        ttk.Label(frm, text="Action").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Combobox(frm, textvariable=self.action, values=list(scheduler.ACTIONS), state="readonly",
                     width=10).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="Applies to").grid(row=r, column=0, sticky="w", pady=3)
        kinds = ttk.Frame(frm)
        kinds.grid(row=r, column=1, sticky="w")
        for value, text in (("group", "A group"), ("account", "One account")):
            ttk.Radiobutton(kinds, text=text, value=value, variable=self.ttype,
                            command=self._fill_targets).pack(side="left", padx=(0, 10))
        r += 1
        ttk.Label(frm, text="Name").grid(row=r, column=0, sticky="w", pady=3)
        self.target_cb = ttk.Combobox(frm, textvariable=self.target, width=26)
        self.target_cb.grid(row=r, column=1, sticky="w")
        self._fill_targets()
        r += 1
        ttk.Label(frm, text="Time (24-hour, HH:MM)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.time, width=8).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="Days").grid(row=r, column=0, sticky="w", pady=3)
        days = ttk.Frame(frm)
        days.grid(row=r, column=1, sticky="w")
        for i, name in enumerate(scheduler.DAY_NAMES):
            ttk.Checkbutton(days, text=name, variable=self.days[i]).pack(side="left")
        r += 1
        ttk.Label(frm, text="Game (start only)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Combobox(frm, textvariable=self.game, values=[p.name for p in self.app.places.places],
                     width=26).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, text="Server (job) ID, optional").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.job, width=28).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(frm, foreground="#777777", wraplength=380, justify="left",
                  text="Leave the game empty to use the group's default game, or the game an account "
                       "last played.").grid(row=r, column=0, columnspan=2, sticky="w", pady=(2, 0))
        r += 1
        btns = ttk.Frame(frm)
        btns.grid(row=r, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=self._save).pack(side="right", padx=6)
        self.grab_set()

    def _fill_targets(self) -> None:
        accounts = self.app.store.accounts
        values = [a.username for a in accounts] if self.ttype.get() == "account" else groups.names(accounts)
        self.target_cb["values"] = values

    def _save(self) -> None:
        h = scheduler.parse_time(self.time.get())
        place_id, job_id = None, (self.job.get().strip() or None)
        raw = self.game.get().strip()
        if raw:
            saved = self.app.places.find(raw)
            if saved:
                place_id = saved.placeId
            else:
                place_id, parsed_job = parse_place_input(raw)
                job_id = job_id or parsed_job
                if place_id is None:
                    messagebox.showwarning("Schedule", "The game is not a saved game, a place ID or a roblox.com "
                                           "link.", parent=self)
                    return
        if job_id and len(job_id) < 8:
            messagebox.showwarning("Schedule", "That does not look like a server (job) ID.", parent=self)
            return
        rule = scheduler.Rule(
            id=self.old.id if self.old else scheduler.Rule().id,
            enabled=self.old.enabled if self.old else True,
            targetType=self.ttype.get(), target=self.target.get().strip(), action=self.action.get(),
            time=f"{h[0]:02d}:{h[1]:02d}" if h else self.time.get().strip(),
            days=[i for i, v in enumerate(self.days) if v.get()], placeId=place_id,
            jobId=job_id if place_id else None, lastRun=self.old.lastRun if self.old else None)
        if self.old is None or (rule.time, rule.days) != (self.old.time, self.old.days):
            # a new or re-timed rule only fires at FUTURE times, never for a moment that already passed today
            rule.lastRun = datetime.now().isoformat(timespec="seconds")
        problems = scheduler.validate_rule(rule, self.app.store.accounts, self.app.settings["groupGames"])
        if problems:
            messagebox.showwarning("Schedule", "\n".join(problems), parent=self)
            return
        self.parent.commit(self.old, rule)
        self.destroy()