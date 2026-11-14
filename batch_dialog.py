"""Desktop date and time selection for Keep's ZIP import."""

from collections.abc import Callable
from calendar import Calendar
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from secrets import randbits
import sqlite3
import tkinter as tk
from tkinter import filedialog, ttk

from batch_fit import (BEIJING, DEFAULT_PERIOD, PERIODS, ScheduledRun, archive_name, edit_run,
                       filter_dates, parse_dates, read_dates, schedule_runs)
from batch_state import BatchDraft, BatchStore
from campus import ROUTES, CampusTrack, RunPlan, clock_text
from route_view import FONT, GREEN, INK


class BatchDialog:
    def __init__(self, parent, track: CampusTrack, plan: RunPlan, device_index: int | None,
                 submit: Callable[[tuple[ScheduledRun, ...], Path | None], None], cancel,
                 store: BatchStore | None = None):
        self.plan, self.submit, self.cancel = plan, submit, cancel
        self.track = track
        self.initial_track, self.initial_plan = track, plan
        self.store = store
        self.rows = {}
        self.exported = set()
        self.pending_days = set()
        self.loading = True
        self.storage_blocked = False
        self.device_index = device_index
        self.runs = ()
        self.time_seed = randbits(64)
        self.busy = False
        self.pending = None
        self.window = win = tk.Toplevel(parent)
        win.withdraw()
        win.title("批量日期导入 · Keep EDA")
        win.transient(parent)
        win.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(win)
        style.configure("Batch.TFrame", background="white")
        style.configure("Batch.TLabel", background="white", foreground=INK, font=(FONT, 9))
        style.configure("Batch.TButton", background="#eaf0e9", foreground=INK, font=(FONT, 9), padding=(7,4))
        style.configure("BatchPrimary.TButton", background=GREEN, foreground="white", font=(FONT,9,"bold"), padding=(7,4))
        style.map("BatchPrimary.TButton", background=[("disabled","#eaf0e9"),("active","#18664f")],
                  foreground=[("disabled","#86968b")])
        style.configure("Batch.Treeview", font=(FONT, 9), rowheight=round(parent.winfo_fpixels("1i")*.23))
        style.configure("Batch.Treeview.Heading", font=(FONT, 9, "bold"))
        body = ttk.Frame(win, padding=(18, 14), style="Batch.TFrame")
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(5, weight=1)
        top = ttk.Frame(body, style="Batch.TFrame")
        top.grid(row=0, column=0, sticky="ew", pady=(0,8))
        self.heading = tk.StringVar(win)
        ttk.Label(top, textvariable=self.heading, style="Batch.TLabel", font=(FONT, 10, "bold")).pack(side="left")
        self.history_button = ttk.Button(top, text="导出历史…", style="Batch.TButton", command=self.history)
        self.history_button.pack(side="right")
        self.new_button = ttk.Button(top, text="新批次", style="Batch.TButton", command=self.new_batch)
        self.new_button.pack(side="right", padx=5)

        dates_header = ttk.Frame(body, style="Batch.TFrame")
        dates_header.grid(row=1, column=0, sticky="ew")
        ttk.Label(dates_header, text="跑步日期", style="Batch.TLabel").pack(side="left")
        self.file_button = ttk.Button(dates_header, text="读取日期文件…", style="Batch.TButton", command=self.import_dates)
        self.file_button.pack(side="right")
        self.week_button = ttk.Button(dates_header, text="前 7 天", style="Batch.TButton", command=self.last_week)
        self.week_button.pack(side="right", padx=5)
        self.calendar_button = ttk.Button(dates_header, text="日历多选…", style="Batch.TButton", command=self.pick_dates)
        self.calendar_button.pack(side="right")
        dates_box = ttk.Frame(body, style="Batch.TFrame")
        dates_box.grid(row=2, column=0, sticky="ew", pady=(3,7))
        self.dates = tk.Text(dates_box, width=48, height=2, relief="solid", borderwidth=1,
                             wrap="word", undo=True, font=(FONT, 10), padx=6, pady=4)
        self.dates.pack(fill="x")
        today = datetime.now(BEIJING).date()
        example = today.replace(day=1)
        ttk.Label(dates_box, text=f"多项用逗号或换行分隔。连续日期可写 {example:%Y-%m-%d} 至 "
                                  f"{example+timedelta(days=4):%Y-%m-%d}。", foreground="#536a5e",
                  style="Batch.TLabel").pack(anchor="w")

        timing = ttk.Frame(body, style="Batch.TFrame")
        timing.grid(row=3, column=0, sticky="ew", pady=(1,6))
        ttk.Label(timing, text="随机时段", style="Batch.TLabel").grid(row=0, column=0, sticky="w", padx=(0,6))
        self.period = tk.StringVar(win, value=DEFAULT_PERIOD)
        self.period_box = ttk.Combobox(timing, width=20, state="readonly", textvariable=self.period,
                                       values=(*PERIODS, "自定义"))
        self.period_box.grid(row=0, column=1, sticky="w")
        self.time_hint = tk.StringVar(win)
        self.time_label = ttk.Label(timing, textvariable=self.time_hint, style="Batch.TLabel")
        self.time_label.grid(row=0, column=2, sticky="w", padx=8)
        self.custom_box = ttk.Frame(timing, style="Batch.TFrame")
        self.earliest = tk.StringVar(win, value="14:00")
        self.latest = tk.StringVar(win, value="21:30")
        self.custom_entries = []
        for col, var in ((0,self.earliest), (2,self.latest)):
            entry = ttk.Entry(self.custom_box, textvariable=var, width=7)
            entry.grid(row=0, column=col)
            self.custom_entries.append(entry)
        ttk.Label(self.custom_box, text=" 至 ", style="Batch.TLabel").grid(row=0, column=1)

        self.weight_box = ttk.Frame(timing, style="Batch.TFrame")
        self.weight_box.grid(row=0, column=3)
        ttk.Label(self.weight_box, text="晚上占比 %", style="Batch.TLabel").pack(side="left")
        self.evening = tk.StringVar(win, value="50")
        self.weight_entry = ttk.Spinbox(self.weight_box, from_=0, to=100, increment=10, width=4, textvariable=self.evening)
        self.weight_entry.pack(side="left", padx=4)
        rules = ttk.Frame(timing, style="Batch.TFrame")
        rules.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6,3))
        ttk.Label(rules, text="保留星期", style="Batch.TLabel").pack(side="left", padx=(0,5))
        self.weekdays = [tk.BooleanVar(win, value=True) for _ in range(7)]
        self.day_checks = []
        for caption, var in zip("一二三四五六日", self.weekdays):
            check = ttk.Checkbutton(rules, text=caption, variable=var, command=self.changed)
            check.pack(side="left", padx=2)
            self.day_checks.append(check)
        ttk.Label(rules, text="  排除日期", style="Batch.TLabel").pack(side="left")
        self.exclusions = tk.StringVar(win)
        self.exclude_entry = ttk.Entry(rules, textvariable=self.exclusions, width=26)
        self.exclude_entry.pack(side="left", padx=5)

        preview_header = ttk.Frame(body, style="Batch.TFrame")
        preview_header.grid(row=4, column=0, sticky="ew", pady=(2,3))
        self.count = tk.StringVar(win, value="时间预览（北京时间）")
        ttk.Label(preview_header, textvariable=self.count, foreground=INK,
                  font=(FONT, 10, "bold"), style="Batch.TLabel").pack(side="left")
        self.draw_button = ttk.Button(preview_header, text="重抽未锁定时间", style="Batch.TButton", command=self.redraw)
        self.draw_button.pack(side="right")
        self.row_draw_button = ttk.Button(preview_header, text="重抽选中日期", style="Batch.TButton", command=self.redraw_selected)
        self.row_draw_button.pack(side="right", padx=5)
        self.lock_button = ttk.Button(preview_header, text="锁定 / 解锁", style="Batch.TButton", command=self.toggle_lock)
        self.lock_button.pack(side="right")
        self.edit_button = ttk.Button(preview_header, text="编辑当天…", style="Batch.TButton", command=self.edit_selected)
        self.edit_button.pack(side="right", padx=5)
        table = ttk.Frame(body)
        table.grid(row=5, column=0, sticky="nsew")
        self.tree = ttk.Treeview(table, columns=("day","start","end","route","km","pace","state"),
                                 show="headings", height=8, style="Batch.Treeview")
        for key, heading, width in (("day","日期",106), ("start","起跑",85), ("end","结束",85),
                                    ("route","路线",156), ("km","公里",58), ("pace","配速",65), ("state","状态",152)):
            self.tree.heading(key, text=heading)
            self.tree.column(key, anchor="center", width=width)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("attention", foreground="#946013")
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.controls())
        self.tree.bind("<Double-1>", lambda _: self.edit_selected())

        self.status = tk.StringVar(win)
        self.status_label = ttk.Label(body, textvariable=self.status, wraplength=810, style="Batch.TLabel")
        self.status_label.grid(row=6, column=0, sticky="ew", pady=(7,5))
        actions = ttk.Frame(body, style="Batch.TFrame")
        actions.grid(row=7, column=0, sticky="ew")
        self.save_button = ttk.Button(actions, text="保存批量 ZIP…", style="BatchPrimary.TButton", command=lambda: self.export(False))
        self.save_button.pack(side="left", padx=(0,6))
        send_text = "发送 ZIP 到雷电" if device_index is None else f"发送 ZIP 到雷电 {device_index}"
        self.send_button = ttk.Button(actions, text=send_text, style="Batch.TButton", command=lambda: self.export(True))
        self.send_button.pack(side="left")
        self.close_button = ttk.Button(actions, text="关闭", style="Batch.TButton", command=self.close)
        self.close_button.pack(side="right")

        self.dates.bind("<<Modified>>", self.text_changed)
        self.period.trace_add("write", lambda *_: self.changed(regenerate=True))
        self.earliest.trace_add("write", lambda *_: self.changed(regenerate=True))
        self.latest.trace_add("write", lambda *_: self.changed(regenerate=True))
        self.evening.trace_add("write", lambda *_: self.changed(regenerate=True))
        self.exclusions.trace_add("write", lambda *_: self.changed())
        problem = None
        if store:
            try:
                self.exported = store.exported_dates()
                draft = store.load_draft()
                if draft:
                    self.load(draft)
            except (OSError, ValueError, sqlite3.Error) as error:
                problem = f"草稿未载入：{error}。可点「新批次」重新开始。"
                self.storage_blocked = True
        self.loading = False
        self.refresh()
        if problem:
            self.message(problem, error=True)
        win.update_idletasks()
        w = min(parent.winfo_screenwidth()-24, max(win.winfo_reqwidth()+12, int(parent.winfo_fpixels("1i")*5.9)))
        h = min(parent.winfo_screenheight()-65, win.winfo_reqheight()+10)
        x = min(max(0,parent.winfo_rootx()+(parent.winfo_width()-w)//2), max(0,parent.winfo_screenwidth()-w))
        y = min(max(0,parent.winfo_rooty()+(parent.winfo_height()-h)//2), max(0,parent.winfo_screenheight()-h-45))
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.minsize(min(w, win.winfo_reqwidth()), min(h, win.winfo_reqheight()))
        win.deiconify()
        win.grab_set()
        self.dates.focus_set()

    def message(self, text, *, error=False):
        self.status.set(text)
        self.status_label.configure(foreground="#9d3328" if error else "#536a5e")

    def text_changed(self, _event):
        if self.dates.edit_modified():
            self.dates.edit_modified(False)
            self.changed()

    def changed(self, *, regenerate=False):
        if self.busy or self.loading:
            return
        if regenerate:
            self.pending_days.update(day for day, row in self.rows.items() if not row.locked)
        if self.pending is not None:
            self.window.after_cancel(self.pending)
        self.runs = ()
        self.controls()
        self.pending = self.window.after(150, self.refresh)

    def refresh(self):
        self.pending = None
        period = self.period.get()
        if period == "自定义":
            self.time_label.grid_remove()
            self.custom_box.grid(row=0, column=2, padx=8)
        else:
            self.custom_box.grid_remove()
            self.time_hint.set(" 或 ".join(f"{a}–{b}" for a,b in PERIODS[period]))
            self.time_label.grid()
        if period == DEFAULT_PERIOD:
            self.weight_box.grid()
        else:
            self.weight_box.grid_remove()
        selection = self.tree.selection()
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        try:
            days = parse_dates(self.dates.get("1.0", "end-1c"))
            excluded = parse_dates(self.exclusions.get()) if self.exclusions.get().strip() else []
            days = filter_dates(days, [i for i,var in enumerate(self.weekdays) if var.get()], excluded)
            if not days:
                raise ValueError("筛选后没有日期，请调整星期或排除日期。")
            evening = self.evening_percent()
            rows = {}
            for day in days:
                key = day.isoformat()
                old = self.rows.get(key)
                if old and (old.locked or key not in self.pending_days):
                    rows[key] = old
                    continue
                plan = old.effective_plan(self.plan) if old else self.plan
                fresh = schedule_runs([day], plan, period, self.time_seed,
                                      (self.earliest.get(), self.latest.get()), evening_percent=evening)[0]
                rows[key] = replace(fresh, route_id=old.route_id if old else self.track.route_id,
                                    plan=plan, route_seed=old.route_seed if old else fresh.route_seed)
            self.rows.update(rows)
            self.runs = tuple(rows.values())
            self.pending_days.difference_update(rows)
        except ValueError as error:
            self.runs = ()
            self.message(str(error), error=bool(self.dates.get("1.0","end-1c").strip()))
        else:
            now = datetime.now(BEIJING)
            duplicates = future = 0
            for run in self.runs:
                key = run.start.date().isoformat()
                duplicate, later = key in self.exported, run.end > now
                duplicates += duplicate
                future += later
                state = " · ".join(s for s,yes in (("锁定",run.locked),("已导出",duplicate),("未来时间",later)) if yes)
                row_plan = run.effective_plan(self.plan)
                end = run.end.strftime("%H:%M:%S")
                if run.end.date() != run.start.date():
                    end += "（次日）"
                self.tree.insert("", "end", iid=key, values=(key,run.start.strftime("%H:%M:%S"),end,
                                 ROUTES[run.route_id or self.track.route_id],f"{row_plan.meters/1000:g}",
                                 clock_text(row_plan.seconds_per_km),state), tags=("attention",) if duplicate or later else ())
            self.tree.selection_set([key for key in selection if self.tree.exists(key)])
            hints = []
            if duplicates:
                hints.append(f"{duplicates} 天曾经导出")
            if future:
                hints.append(f"{future} 条结束时间在未来")
            self.message(("，".join(hints) + "。" if hints else "") + "双击一行可编辑当天，锁定后保留时间与参数。")
        self.heading.set(f"默认 {self.plan.meters/1000:g} 公里 · {clock_text(self.plan.seconds_per_km)} / 公里 · 每天一条")
        self.count.set(f"时间预览（北京时间） · {len(self.runs)} 条")
        self.controls()
        self.persist()

    def evening_percent(self):
        try:
            value = int(self.evening.get())
            if 0 <= value <= 100:
                return value
        except ValueError:
            pass
        if self.period.get() != DEFAULT_PERIOD:
            return 50
        raise ValueError("晚上占比请填 0 至 100 的整数。")

    def snapshot(self):
        return BatchDraft(self.track.route_id, self.plan, self.time_seed, self.dates.get("1.0","end-1c"),
                          self.period.get(), (self.earliest.get(),self.latest.get()), self.evening_percent(),
                          tuple(i for i,var in enumerate(self.weekdays) if var.get()), self.exclusions.get(),
                          tuple(self.rows[key] for key in sorted(self.rows)), tuple(sorted(self.pending_days)))

    def persist(self):
        if self.store and not self.storage_blocked:
            try:
                self.store.save_draft(self.snapshot())
            except ValueError:
                pass  # Leave the last valid preferences intact while typing a number.
            except (OSError, sqlite3.Error) as error:
                self.message(f"草稿保存失败：{error}。仍可导出 ZIP。", error=True)

    def load(self, draft):
        self.loading = True
        self.track, self.plan = CampusTrack(draft.route_id), draft.plan
        self.time_seed, self.rows = draft.time_seed, {r.start.date().isoformat():r for r in draft.rows}
        self.dates.delete("1.0", "end")
        self.dates.insert("1.0", draft.dates_text)
        self.dates.edit_modified(False)
        self.period.set(draft.period)
        self.earliest.set(draft.custom[0])
        self.latest.set(draft.custom[1])
        self.evening.set(str(draft.evening_percent))
        for i,var in enumerate(self.weekdays):
            var.set(i in draft.weekdays)
        self.exclusions.set(draft.exclusions)
        self.pending_days = set(draft.pending_days)
        self.loading = False

    def new_batch(self):
        if not self.busy:
            self.storage_blocked = False
            self.load(BatchDraft(self.initial_track.route_id, self.initial_plan, randbits(64)))
            self.changed()

    def set_dates(self, text):
        self.dates.delete("1.0", "end")
        self.dates.insert("1.0", text)
        self.dates.edit_modified(False)
        self.changed()

    def last_week(self):
        yesterday = datetime.now(BEIJING).date() - timedelta(days=1)
        self.set_dates(f"{yesterday-timedelta(days=6)} 至 {yesterday}")

    def import_dates(self):
        path = filedialog.askopenfilename(parent=self.window, title="读取日期列表",
                                          filetypes=[("日期文件", "*.txt *.csv"), ("所有文件", "*.*")])
        if path:
            try:
                days = read_dates(Path(path))
            except (OSError, ValueError) as error:
                self.message(str(error), error=True)
                return
            self.set_dates("，".join(d.isoformat() for d in days))

    def pick_dates(self):
        try:
            selected = set(parse_dates(self.dates.get("1.0","end-1c")))
        except ValueError:
            selected = set()
        today = datetime.now(BEIJING).date()
        month = min(selected, default=today).replace(day=1)
        anchor = None
        picker = tk.Toplevel(self.window)
        picker.title("选择跑步日期")
        picker.transient(self.window)
        body = ttk.Frame(picker, padding=10)
        body.pack()
        title = tk.StringVar(picker)
        counter = tk.StringVar(picker)
        cells = ttk.Frame(body)
        cells.grid(row=2, column=0, columnspan=3, sticky="nsew")

        def draw():
            title.set(f"{month.year} 年 {month.month} 月")
            counter.set(f"已选 {len(selected)} 天")
            for widget in cells.winfo_children():
                widget.destroy()
            for col, day in enumerate("一二三四五六日"):
                ttk.Label(cells, text=day, anchor="center").grid(row=0, column=col, sticky="ew")
            for row, week in enumerate(Calendar().monthdayscalendar(month.year,month.month),1):
                for col, n in enumerate(week):
                    if not n:
                        continue
                    day = month.replace(day=n)
                    chosen = day in selected
                    cell = tk.Button(cells, text=str(n), width=3, relief="flat", font=(FONT,10),
                                     bg=GREEN if chosen else "#f4f6f2", fg="white" if chosen else INK,
                                     command=lambda d=day: toggle(d))
                    cell.grid(row=row, column=col, padx=1, pady=1, sticky="ew")
                    cell.bind("<Shift-ButtonPress-1>", lambda _, d=day: toggle(d, entire_range=True))
                    cell.bind("<Shift-ButtonRelease-1>", lambda _: "break")

        def toggle(day, entire_range=False):
            nonlocal anchor
            if entire_range and anchor:
                a,b = sorted((day.toordinal(), anchor.toordinal()))
                selected.update(date.fromordinal(i) for i in range(a,b+1))
            elif day in selected:
                selected.remove(day)
            else:
                selected.add(day)
            anchor = day
            draw()
            return "break"

        def move(step):
            nonlocal month
            year, m = divmod(month.year*12 + month.month-1 + step, 12)
            if 1 <= year <= 9999:
                month = date(year,m+1,1)
                draw()

        def accept():
            self.set_dates("，".join(d.isoformat() for d in sorted(selected)))
            picker.destroy()

        ttk.Button(body, text="上一月", command=lambda: move(-1)).grid(row=0,column=0)
        ttk.Label(body,textvariable=title,anchor="center").grid(row=0,column=1,padx=6)
        ttk.Button(body, text="下一月", command=lambda: move(1)).grid(row=0,column=2)
        ttk.Label(body,text="单击选多天，Shift + 单击选连续日期。").grid(row=1,column=0,columnspan=3,pady=5)
        ttk.Label(body,textvariable=counter).grid(row=3,column=0,sticky="w",pady=(7,0))
        ttk.Button(body,text="取消",command=picker.destroy).grid(row=3,column=1,pady=(7,0))
        ttk.Button(body,text="使用所选日期",command=accept).grid(row=3,column=2,pady=(7,0))
        draw()
        picker.grab_set()
        picker.wait_window()
        if self.window.winfo_exists():
            self.window.grab_set()

    def redraw(self):
        self.time_seed = randbits(64)
        self.changed(regenerate=True)

    def redraw_selected(self):
        if self.busy:
            return
        try:
            updates = {}
            for key in self.tree.selection():
                old = self.rows[key]
                if old.locked:
                    continue
                fresh = schedule_runs([old.start.date()], old.effective_plan(self.plan), self.period.get(),
                                      randbits(64), (self.earliest.get(), self.latest.get()),
                                      evening_percent=self.evening_percent())[0]
                updates[key] = replace(old, start=fresh.start, end=fresh.end)
            self.rows.update(updates)
            self.changed()
        except ValueError as error:
            self.message(str(error), error=True)

    def toggle_lock(self):
        if self.busy:
            return
        keys = self.tree.selection()
        lock = any(not self.rows[key].locked for key in keys)
        for key in keys:
            self.rows[key] = replace(self.rows[key], locked=lock)
        self.changed()

    def edit_selected(self):
        selected = self.tree.selection()
        if self.busy or not self.runs or len(selected) != 1:
            return
        key = selected[0]
        row = self.rows[key]
        plan = row.effective_plan(self.plan)
        editor = tk.Toplevel(self.window)
        editor.title(f"编辑 {key}")
        editor.transient(self.window)
        box = ttk.Frame(editor, padding=16)
        box.pack(fill="both", expand=True)
        km = tk.StringVar(editor, value=f"{plan.meters/1000:g}")
        pace = tk.StringVar(editor, value=clock_text(plan.seconds_per_km))
        start = tk.StringVar(editor, value=row.start.strftime("%H:%M:%S"))
        route = tk.StringVar(editor, value=ROUTES[row.route_id or self.track.route_id])
        clockwise = tk.BooleanVar(editor, value=plan.clockwise)
        random_start = tk.BooleanVar(editor, value=plan.random_start)
        locked = tk.BooleanVar(editor, value=True)
        for i,(caption,var) in enumerate((("距离（公里）",km),("配速（分/公里）",pace),("起跑时间",start))):
            ttk.Label(box, text=caption).grid(row=i, column=0, sticky="w", pady=5)
            ttk.Entry(box, textvariable=var, width=22).grid(row=i, column=1, padx=10, pady=5)
        ttk.Label(box, text="路线").grid(row=3,column=0,sticky="w")
        ttk.Combobox(box, textvariable=route, values=tuple(ROUTES.values()), state="readonly", width=22).grid(row=3,column=1,padx=10,pady=5)
        options = ttk.Frame(box)
        options.grid(row=4,column=0,columnspan=2,sticky="w",pady=8)
        for caption,var in (("顺时针",clockwise),("随机起点",random_start),("锁定当天",locked)):
            ttk.Checkbutton(options,text=caption,variable=var).pack(side="left",padx=3)
        error = tk.StringVar(editor)
        ttk.Label(box,textvariable=error,foreground="#9d3328",wraplength=350).grid(row=5,column=0,columnspan=2,sticky="w")

        def accept():
            try:
                updated = edit_run(row, self.plan, km=km.get(), pace=pace.get(), clock=start.get(),
                                   route_id=next(k for k,v in ROUTES.items() if v == route.get()),
                                   clockwise=clockwise.get(), random_start=random_start.get(), locked=locked.get())
            except ValueError as problem:
                error.set(str(problem))
                return
            self.rows[key] = updated
            editor.destroy()
            self.changed()

        ttk.Button(box, text="保存当天", command=accept).grid(row=6,column=1,sticky="e",pady=(10,0))
        ttk.Button(box, text="取消", command=editor.destroy).grid(row=6,column=0,sticky="w",pady=(10,0))
        editor.grab_set()
        editor.wait_window()
        if self.window.winfo_exists():
            self.window.grab_set()

    def history(self):
        if self.busy or not self.store:
            return
        try:
            entries = self.store.history()
        except (OSError, sqlite3.Error) as error:
            self.message(f"无法读取历史：{error}", error=True)
            return
        dialog = tk.Toplevel(self.window)
        dialog.title("导出历史 · 可恢复批次")
        dialog.transient(self.window)
        body = ttk.Frame(dialog, padding=12)
        body.pack(fill="both",expand=True)
        ttk.Label(body,text="只记录成功保存或发送的 ZIP。Keep 的导入结果需在应用内核对。").pack(anchor="w",pady=(0,8))
        tree = ttk.Treeview(body,columns=("created","kind","count"),show="headings",height=9)
        for col,title,width in (("created","时间（北京时间）",190),("kind","操作",125),("count","条数",65)):
            tree.heading(col,text=title)
            tree.column(col,width=width,anchor="center")
        tree.pack(fill="both",expand=True)
        for entry in entries:
            tree.insert("","end",iid=str(entry["id"]),values=(entry["created"][:19].replace("T"," "),entry["kind"],entry["count"]))
        details = tk.StringVar(dialog,value="选择一条历史后可恢复当时的日期、时间与每一天的参数。" if entries else "还没有成功导出的批次。")
        ttk.Label(body,textvariable=details,wraplength=470).pack(anchor="w",pady=8)
        def select(_):
            if tree.selection():
                item = next(e for e in entries if str(e["id"]) == tree.selection()[0])
                details.set(item["target"])
        tree.bind("<<TreeviewSelect>>",select)
        def restore():
            if not tree.selection():
                return
            try:
                draft = self.store.restore_export(int(tree.selection()[0]))
            except (OSError, ValueError, sqlite3.Error) as error:
                details.set(str(error))
                return
            self.storage_blocked = False
            self.load(draft)
            self.changed()
            dialog.destroy()
        ttk.Button(body,text="恢复选中批次",command=restore).pack(side="left")
        ttk.Button(body,text="关闭",command=dialog.destroy).pack(side="right")
        dialog.grab_set()
        dialog.wait_window()
        if self.window.winfo_exists():
            self.window.grab_set()

    def controls(self):
        idle = not self.busy
        for widget in (self.dates,self.file_button,self.week_button,self.calendar_button,self.new_button,
                       self.weight_entry,self.exclude_entry,*self.day_checks,*self.custom_entries):
            widget.configure(state="normal" if idle else "disabled")
        self.period_box.configure(state="readonly" if idle else "disabled")
        for widget, enabled in ((self.draw_button, self.runs), (self.save_button, self.runs),
                                (self.send_button, self.runs and self.device_index is not None),
                                (self.history_button, self.store is not None),
                                (self.edit_button, self.runs and len(self.tree.selection()) == 1),
                                (self.row_draw_button, self.runs and self.tree.selection()),
                                (self.lock_button, self.runs and self.tree.selection())):
            widget.configure(state="normal" if idle and enabled else "disabled")
        self.close_button.configure(text="取消任务" if self.busy else "关闭")

    def export(self, send):
        if self.busy or not self.runs or (send and self.device_index is None):
            return
        dest = None
        if not send:
            file = filedialog.asksaveasfilename(parent=self.window, title="保存批量运动 ZIP",
                        initialfile=archive_name(self.runs), defaultextension=".zip", filetypes=[("ZIP 压缩包", "*.zip")])
            if not file:
                return
            dest = Path(file)
        self.busy = True
        self.controls()
        self.message("正在准备批量 ZIP…")
        self.submit(self.runs, dest)

    def finish(self, message, error):
        self.busy = False
        if self.store:
            try:
                self.exported = self.store.exported_dates()
                self.refresh()
            except (OSError, sqlite3.Error):
                pass
        self.message(message, error=error)
        self.controls()

    def close(self):
        if self.busy:
            self.cancel()
            self.message("正在取消任务…")
        else:
            if self.pending is not None:
                self.window.after_cancel(self.pending)
                self.refresh()
            self.persist()
            self.window.destroy()
