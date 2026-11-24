"""Windows desktop controller for the DUT Development Zone campus route."""

from collections.abc import Callable
from datetime import datetime
import json
import os
import sqlite3
from pathlib import Path
from queue import Empty, Queue
from secrets import randbits
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from batch_dialog import BatchDialog
from batch_fit import archive_name, export_batch
from batch_state import BatchStore
from campus import ROUTES, CampusTrack, RunPlan, clock_text
from fit_export import export_fit
from ldplayer import LDPlayer, console_at, find_console
from live_history import LastReplay
from playback import PlaybackControl, Sample, replay, replay_on_device
from route_view import FONT, GREEN, INK, RouteView
from sensor_bridge import open_step_viewer, orient_step_viewer


BACKGROUND, PAPER, LIGHT, MUTED = "#f4f6f2", "#ffffff", "#eaf0e9", "#536a5e"
SETTINGS = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "KeepEDA" / "settings.json"
INITIAL_HINT = "打开 Keep 计步窗口，点「实时回放」待命，再在 Keep 开始户外跑步；默认自动联动起停。"
PERMISSION_HINT = "Keep 的「身体活动」权限已关闭。可在 Android 的「应用 → Keep → 权限」中允许身体活动。"


def read_settings(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def label(frame, text="", *, size=10, bold=False, color=INK, bg=PAPER, **options):
    return tk.Label(frame, text=text, bg=bg, fg=color, anchor="w",
                    font=(FONT, size, "bold" if bold else "normal"), **options)


def button(frame, text, command, *, primary=False):
    return tk.Button(frame, text=text, command=command, font=(FONT, 10, "bold" if primary else "normal"),
                     fg="white" if primary else INK, bg=GREEN if primary else LIGHT,
                     activeforeground="white", activebackground="#18664f", disabledforeground="#86968b",
                     padx=10, pady=5, relief="flat", overrelief="ridge", bd=0, cursor="hand2")


class Dashboard:
    def __init__(self, root: tk.Tk, settings_path: Path = SETTINGS):
        self.root = root
        self.settings_path = settings_path
        self.batch_store = BatchStore(settings_path.with_name("batches.sqlite3"))
        prefs = read_settings(settings_path)
        route_id = prefs.get("route", "lake")
        self.track = CampusTrack(route_id if isinstance(route_id, str) and route_id in ROUTES else "lake")
        self.seed = randbits(48)
        self.events = Queue()
        self.job = ""
        self.control = PlaybackControl()
        self.paused = False
        self.closing = False
        self.has_sample = False
        self.devices = []
        self.batch_dialog = None
        self.selected_index = prefs.get("index", 0)
        self.console = console_at(Path(str(prefs.get("console", "")))) or find_console()
        self.km = tk.StringVar(value="3.00")
        self.pace = tk.StringVar(value=prefs.get("pace", "6:00"))
        self.route = tk.StringVar(value=self.track.name)
        self.random_start = tk.BooleanVar(value=prefs.get("random_start", True))
        self.motion_enabled = tk.BooleanVar(value=prefs.get("motion", True))
        self.link_enabled = tk.BooleanVar(value=prefs.get("link_keep", True))
        self.linking = False
        self.direction = tk.StringVar(value="顺时针" if prefs.get("clockwise") is True else "逆时针")
        self.choice = tk.StringVar()
        self.summary = tk.StringVar()
        self.stats = [tk.StringVar(value=s) for s in ("0.000 km", "00:00", "—", "0")]
        self.lap_info = tk.StringVar()
        self.split_info = tk.StringVar()
        self.gait_info = tk.StringVar()
        self.status = tk.StringVar(value=INITIAL_HINT)
        self.distance = 0.0
        self.plan = None
        self.editors = []
        self._build()
        for variable in (self.km, self.pace, self.direction):
            variable.trace_add("write", lambda *_: self.plan_changed())
        self.plan_changed()
        self.update_controls()
        self.root.after(60, self.poll)
        self.root.after(100, self.refresh)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _build(self) -> None:
        root = self.root
        root.title("开发区校园跑 · Keep EDA · 仅供娱乐")
        if os.name == "nt":
            root.iconbitmap(str(Path(__file__).resolve().parent / "data" / "keepeda.ico"))
        root.configure(bg=BACKGROUND)
        scale = max(1, root.winfo_fpixels("1i") / 96)
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        width, height = min(round(990*scale), screen_w-36), min(round(680*scale), screen_h-90)
        root.geometry(f"{width}x{height}")
        root.minsize(min(round(805*scale), width), min(round(605*scale), height))
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TCombobox", fieldbackground="white", padding=4, arrowsize=14)
        style.map("TCombobox", fieldbackground=[("readonly", "white")],
                  selectbackground=[("readonly", "white")], selectforeground=[("readonly", INK)])
        style.configure("TEntry", fieldbackground="white", padding=5)
        style.configure("Run.Horizontal.TProgressbar", background=GREEN, troughcolor=LIGHT,
                        bordercolor=LIGHT, lightcolor=GREEN, darkcolor=GREEN, thickness=6)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        header = tk.Frame(root, bg=BACKGROUND)
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(15, 12))
        label(header, "校园跑控制台", size=21, bold=True, bg=BACKGROUND).pack(side="left")
        label(header, "大连理工大学 · 开发区校区  /  仅供娱乐用途", size=10, color=MUTED, bg=BACKGROUND).pack(
            side="left", anchor="s", padx=13, pady=5)

        body = tk.Frame(root, bg=BACKGROUND)
        body.grid(row=1, column=0, sticky="nsew", padx=22)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=1)
        left = tk.Frame(body, bg=PAPER, padx=16, pady=13)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        map_header = tk.Frame(left, bg=PAPER)
        map_header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.route_box = ttk.Combobox(map_header, state="readonly", values=list(ROUTES.values()),
                                      textvariable=self.route, width=16, font=(FONT, 11, "bold"))
        self.route_box.pack(side="left")
        self.route_box.bind("<<ComboboxSelected>>", lambda _: self.change_route())
        label(map_header, textvariable=self.lap_info, size=9, color=MUTED).pack(side="right")
        self.map = RouteView(left, self.track)
        self.map.grid(row=1, column=0, sticky="nsew")
        metrics = tk.Frame(left, bg=PAPER)
        metrics.grid(row=2, column=0, sticky="ew", pady=(12, 7))
        for i, caption in enumerate(("回放里程", "运动用时", "当前配速 / km", "模拟步数")):
            metrics.columnconfigure(i, weight=1, uniform="metric")
            label(metrics, caption, size=9, color=MUTED).grid(row=0, column=i, sticky="w")
            tk.Label(metrics, textvariable=self.stats[i], font=("Segoe UI", 16, "bold"), bg=PAPER, fg=INK,
                     anchor="w").grid(row=1, column=i, sticky="w")
        self.progress = ttk.Progressbar(left, maximum=100, style="Run.Horizontal.TProgressbar")
        self.progress.grid(row=3, column=0, sticky="ew")
        label(metrics, textvariable=self.gait_info, size=9, color=MUTED).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(3, 0))

        right = tk.Frame(body, bg=PAPER, padx=16, pady=13)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        label(right, "雷电模拟器 14", size=13, bold=True).grid(row=0, column=0, sticky="w")
        self.path_button = button(right, "选择目录", self.choose_console)
        self.path_button.grid(row=0, column=1, sticky="e")
        self.instances = ttk.Combobox(right, state="readonly", width=18, font=(FONT, 10), textvariable=self.choice)
        self.instances.grid(row=1, column=0, sticky="ew", pady=7, padx=(0, 6))
        self.instances.bind("<<ComboboxSelected>>", lambda _: self.select_instance())
        self.refresh_button = button(right, "刷新", self.refresh)
        self.refresh_button.grid(row=1, column=1, sticky="ew", pady=7)
        device_actions = tk.Frame(right, bg=PAPER)
        device_actions.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.locate_button = button(device_actions, "定位起点", lambda: self.device_action("locate"))
        self.keep_button = button(device_actions, "打开 Keep", lambda: self.device_action("keep"))
        self.locate_button.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.keep_button.pack(side="left", fill="x", expand=True, padx=(4, 0))
        tk.Frame(right, bg=LIGHT, height=1).grid(row=3, column=0, columnspan=2, sticky="ew", pady=13)
        label(right, "跑步设置", size=13, bold=True).grid(row=4, column=0, sticky="w")
        self.reroll_button = button(right, "重新生成", self.reroll)
        self.reroll_button.grid(row=4, column=1, sticky="e")
        fields = tk.Frame(right, bg=PAPER)
        fields.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(6, 3))
        for i, (caption, variable) in enumerate((("距离（公里）", self.km), ("平均配速（分/公里）", self.pace))):
            fields.columnconfigure(i, weight=1, uniform="field")
            pad = (0, 5) if i == 0 else (5, 0)
            label(fields, caption, size=9, color=MUTED).grid(row=0, column=i, sticky="w", padx=pad)
            edit = ttk.Entry(fields, width=9, textvariable=variable, font=("Segoe UI", 12))
            edit.grid(row=1, column=i, sticky="ew", padx=pad, pady=2)
            self.editors.append(edit)
        label(right, "跑步方向", size=9, color=MUTED).grid(row=6, column=0, sticky="w")
        self.start_check = tk.Checkbutton(right, text="随机起点", variable=self.random_start,
                                         command=self.change_start, bg=PAPER, activebackground=PAPER,
                                         fg=INK, font=(FONT, 9))
        self.start_check.grid(row=6, column=1, sticky="e")
        self.direction_box = ttk.Combobox(right, state="readonly", font=(FONT, 10), width=8,
                                          values=("逆时针", "顺时针"), textvariable=self.direction)
        self.direction_box.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(2, 4))
        label(right, textvariable=self.summary, size=9, color=MUTED).grid(row=8, column=0, columnspan=2, sticky="w")
        label(right, textvariable=self.split_info, size=9, color=MUTED).grid(row=9, column=0, columnspan=2, sticky="nw")
        right.rowconfigure(9, weight=1)
        self.modes = ttk.Notebook(right)
        self.modes.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        live_panel = tk.Frame(self.modes, bg=PAPER, padx=7, pady=5)
        files_panel = tk.Frame(self.modes, bg=PAPER, padx=7, pady=5)
        self.modes.add(live_panel, text="Keep 实时记录")
        self.modes.add(files_panel, text="FIT 文件导入")
        sensor_row = tk.Frame(live_panel, bg=PAPER)
        sensor_row.pack(fill="x")
        self.motion_check = tk.Checkbutton(sensor_row, text="同步跑步计步", variable=self.motion_enabled, command=self.update_controls,
                                           bg=PAPER, activebackground=PAPER, fg=INK, font=(FONT, 9), anchor="w")
        self.motion_check.pack(side="left")
        self.permission_button = tk.Button(sensor_row, text="权限", font=(FONT, 9), command=lambda: self.device_action("permissions"),
                                          relief="flat", fg=GREEN, bg=PAPER, bd=0, padx=4, cursor="hand2")
        self.permission_button.pack(side="right")
        link_row = tk.Frame(live_panel, bg=PAPER)
        link_row.pack(fill="x")
        self.link_check = tk.Checkbutton(link_row, text="与 Keep 起停联动", variable=self.link_enabled,
                                         bg=PAPER, activebackground=PAPER, fg=INK, font=(FONT,9), anchor="w")
        self.link_check.pack(side="left")
        self.last_button = tk.Button(link_row, text="上次回放", command=self.show_last_replay,
                                     font=(FONT,9), fg=GREEN, bg=PAPER, relief="flat", bd=0, cursor="hand2")
        self.last_button.pack(side="right")
        actions = tk.Frame(live_panel, bg=PAPER)
        actions.pack(fill="x", pady=(3, 0))
        actions.columnconfigure((0, 1), weight=1, uniform="actions")
        self.play_button = button(actions, "实时回放", lambda: self.start_run(False), primary=True)
        self.play_button.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        self.pause_button = button(actions, "暂停", self.toggle_pause, primary=True)
        self.stop_button = button(actions, "结束", self.stop)
        self.preview_button = button(actions, "离线预演 ×20", lambda: self.start_run(True))
        self.preview_button.grid(row=1, column=0, columnspan=2, sticky="ew")
        label(files_panel, "通过此入口导入，Keep 会显示「数据导入」。", size=9, color=MUTED).pack(anchor="w", pady=(1, 5))
        self.batch_button = button(files_panel, "批量日期导入…", self.open_batch, primary=True)
        self.batch_button.pack(fill="x", pady=(0, 4))
        single = tk.Frame(files_panel, bg=PAPER)
        single.pack(fill="x")
        single.columnconfigure((0,1), weight=1, uniform="single")
        self.fit_button = button(single, "保存单个 FIT", self.save_fit)
        self.fit_button.grid(row=0, column=0, sticky="ew", padx=(0,2))
        self.transfer_button = button(single, "发送单个 FIT", self.send_fit)
        self.transfer_button.grid(row=0, column=1, sticky="ew", padx=(2,0))
        self.status_label = label(root, textvariable=self.status, size=10, color=MUTED, bg=BACKGROUND,
                                  wraplength=width-55, justify="left")
        self.status_label.grid(row=2, column=0, sticky="ew", padx=23, pady=(10, 13))
        root.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(300, event.width-55))
                  if event.widget is root else None)

    def change_route(self) -> None:
        key = next(key for key, name in ROUTES.items() if name == self.route.get())
        self.track = CampusTrack(key)
        self.map.set_track(self.track)
        self.plan_changed()
        self.tell("路线已切换。实时回放前请重新定位起点。")

    def change_start(self) -> None:
        self.plan_changed()
        self.tell("起点已更新。实时回放前请重新定位起点。")

    def reroll(self) -> None:
        self.seed = randbits(48)
        self.plan_changed()
        self.tell("已生成新的走线、配速和步频。实时回放前请确认地图上的起点并重新定位。")

    def plan_changed(self) -> None:
        self.lap_info.set(f"基准单圈 {self.track.length:.1f} 米")
        try:
            self.plan = RunPlan.parse(self.km.get(), self.pace.get(), self.direction.get() == "顺时针",
                                      self.seed, self.random_start.get())
            estimate = self.plan.pacing.at(self.plan.duration).steps
            self.summary.set(f"预计 {clock_text(self.plan.duration)}   ·   约 {estimate:,} 步")
            times = [0] + [round(self.plan.pacing.time_at(d)) for d in range(1000, min(3000, self.plan.meters)+1, 1000)]
            splits = " / ".join(clock_text(b-a) for a, b in zip(times, times[1:]))
            self.split_info.set(f"公里分段  {splits}" if splits else "")
        except ValueError as error:
            self.plan = None
            self.summary.set(str(error))
            self.split_info.set("")
        self.reset_metrics()
        self.update_controls()

    def reset_metrics(self) -> None:
        start = self.track.start_point(self.plan) if self.plan else self.map.start
        heading = self.track.travel_path(self.plan).heading_at(0) if self.plan else None
        self.sample(Sample(0, 0, start, heading=heading))

    def sample(self, sample: Sample) -> None:
        self.distance = sample.meters
        for variable, value in zip(self.stats, (f"{sample.meters/1000:.3f} km", clock_text(sample.seconds),
                                                clock_text(1000/sample.speed) if sample.speed else "—", f"{sample.steps:,}")):
            variable.set(value)
        if sample.cadence:
            self.gait_info.set(f"模拟步频 {sample.cadence:.0f} 步/分    ·    单步步幅 {60*sample.speed/sample.cadence:.2f} 米")
        elif self.plan:
            steps = self.plan.pacing.at(self.plan.duration).footfalls
            self.gait_info.set(f"预计平均步频 {60*steps/self.plan.duration:.0f} 步/分    ·    单步步幅 {self.plan.meters/steps:.2f} 米")
        else:
            self.gait_info.set("")
        self.progress["value"] = 100 * sample.meters / self.plan.meters if self.plan else 0
        self.map.set_position(sample.point, self.direction.get() == "顺时针",
                              reset=sample.seconds == 0, heading=sample.heading)

    def update_controls(self) -> None:
        idle = not self.job
        connected = bool(self.devices and self.console)
        active = self.job in ("live", "preview")
        self.keep_button.configure(text="打开 Keep 计步窗口" if self.motion_enabled.get() else "打开 Keep")
        for widget, enabled in ((self.path_button, idle), (self.refresh_button, idle), (self.reroll_button, idle),
                                (self.motion_check, idle), (self.start_check, idle),
                                (self.link_check, idle and self.motion_enabled.get()),
                                (self.last_button, idle),
                                (self.permission_button, idle and connected),
                                (self.locate_button, idle and connected and self.plan), (self.keep_button, idle and connected),
                                (self.play_button, idle and connected and self.plan),
                                (self.transfer_button, idle and connected and self.plan),
                                (self.batch_button, idle and self.plan),
                                (self.preview_button, idle and self.plan), (self.fit_button, idle and self.plan)):
            widget.configure(state="normal" if enabled else "disabled")
        for widget in self.editors:
            widget.configure(state="normal" if idle else "disabled")
        self.instances.configure(state="readonly" if idle and connected else "disabled")
        self.direction_box.configure(state="readonly" if idle else "disabled")
        self.route_box.configure(state="readonly" if idle else "disabled")
        self.modes.tab(1, state="disabled" if active else "normal")
        if active:
            self.play_button.grid_remove()
            self.pause_button.grid(row=0, column=0, sticky="ew", padx=(0, 3), pady=(0, 5))
            self.stop_button.grid(row=0, column=1, sticky="ew", padx=(3, 0), pady=(0, 5))
            self.pause_button.configure(text="继续" if self.paused else "暂停",
                                       state="normal" if self.has_sample and not self.control.cancel.is_set() else "disabled")
            self.stop_button.configure(state="disabled" if self.control.cancel.is_set() else "normal")
        else:
            self.pause_button.grid_remove()
            self.stop_button.grid_remove()
            self.play_button.grid()

    def save_settings(self) -> None:
        prefs = dict(index=self.selected_index, console=str(self.console or ""), route=self.track.route_id,
                     random_start=self.random_start.get(),
                     link_keep=self.link_enabled.get(),
                     motion=self.motion_enabled.get(), pace=self.pace.get(), clockwise=self.direction.get() == "顺时针")
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile("w", dir=self.settings_path.parent, delete=False, encoding="utf-8") as temp:
                json.dump(prefs, temp, ensure_ascii=False, indent=2)
            Path(temp.name).replace(self.settings_path)
        except OSError:
            pass  # A read-only profile does not prevent a run.

    def choose_console(self) -> None:
        folder = filedialog.askdirectory(title="选择含 ldconsole.exe 的雷电 14 安装目录", parent=self.root,
                                         initialdir=str(self.console.parent) if self.console else None)
        if not folder:
            return
        candidate = console_at(Path(folder))
        if candidate:
            self.console = candidate
            self.refresh()
        else:
            self.tell("所选目录内没有 ldconsole.exe 或 dnconsole.exe。请选择雷电安装目录。", True)

    def select_instance(self) -> None:
        at = self.instances.current()
        if 0 <= at < len(self.devices):
            self.selected_index = self.devices[at].index

    def show_instances(self, devices) -> None:
        self.devices = devices
        self.instances.configure(values=[d.label for d in devices])
        if devices:
            preferred = next((d for d in devices if d.index == self.selected_index), None)
            chosen = preferred or next((d for d in devices if d.booted), devices[0])
            self.selected_index = chosen.index
            self.instances.current(devices.index(chosen))
        else:
            self.choice.set("未发现实例")
        self.update_controls()

    def tell(self, message: str, error=False) -> None:
        self.status.set(message)
        self.status_label.configure(fg="#a23c2e" if error else MUTED)

    def task(self, kind: str, message: str, work: Callable[[PlaybackControl], str]) -> None:
        if self.job:
            return
        self.job = kind
        self.control = PlaybackControl()
        self.paused = self.has_sample = False
        self.update_controls()
        self.tell(message)
        control = self.control

        def worker():
            try:
                self.events.put(("done", work(control)))
            except Exception as error:
                self.events.put(("error", str(error)))
        Thread(target=worker, daemon=True, name=f"keep-eda-{kind}").start()

    def refresh(self) -> None:
        if self.job:
            return
        if not self.console:
            self.console = find_console()
        if not self.console:
            self.show_instances([])
            self.tell("请选择雷电 14 安装目录。离线预演和 FIT 导出已可用。")
            return
        device = LDPlayer(self.console)
        self.show_instances([])

        def scan(_control):
            devices = device.instances()
            self.events.put(("devices", devices))
            return INITIAL_HINT if devices else "请先在雷电多开器中创建实例，然后点击刷新。"
        self.task("scan", "正在读取雷电实例…", scan)

    def device_action(self, kind: str) -> None:
        device, index = LDPlayer(self.console), self.selected_index
        start = self.track.start_point(self.plan) if self.plan else None
        heading = self.track.travel_path(self.plan).heading_at(0) if self.plan else None
        check_motion = self.motion_enabled.get()

        def action(control):
            if not device.prepare(index, control.cancel):
                return "已取消。"
            self.events.put(("devices", device.instances()))
            if kind == "keep":
                if check_motion:
                    with device.reserve_location(index):
                        return open_step_viewer(device, index, control.cancel, heading=heading)
                return device.open_keep(index)
            if kind == "permissions":
                return device.open_keep_permissions(index)
            if start is None:
                raise ValueError("请先填写有效的跑步距离和配速。")
            with device.reserve_location(index):
                device.check_location(index)
                device.locate(index, start)
                if check_motion:
                    orient_step_viewer(device, index, heading)
            self.events.put(("sample", Sample(0, 0, start, heading=heading)))
            hint = (PERMISSION_HINT if check_motion and device.keep_activity_permission(index) is False
                    else "点击「实时回放」待命，再在 Keep 中开始户外跑步。")
            return f"已定位到{self.track.name}起点。{hint}"
        self.task(kind, f"正在连接实例 {index}，等待 Android 就绪…", action)

    def start_run(self, preview: bool) -> None:
        plan = self.plan
        if not plan or self.job:
            return
        self.reset_metrics()
        self.save_settings()
        use_motion = self.motion_enabled.get()
        self.linking = not preview and use_motion and self.link_enabled.get()
        linking = self.linking
        device = LDPlayer(self.console) if not preview and self.console else None
        index = self.selected_index

        def run(control):
            history = LastReplay(self.last_replay_path(index), self.track.name, plan) if not preview else None
            ending = {}
            def status(state, message):
                if state in {"ended", "notice"}:
                    ending["message"] = message
                if history and state in {"paused", "running"}:
                    history.update(control, "已暂停" if state == "paused" else "回放中")
                self.events.put(("live_state", (state, message)))
            def report(sample):
                if history:
                    history.update(control)
                self.events.put(("sample", sample))
            if preview:
                complete = replay(self.track, plan, control, report, preview=True)
                return "离线预演完成。" if complete else "已结束离线预演。"
            try:
                complete = replay_on_device(self.track, plan, control, device, index, report, use_motion=use_motion,
                                            link_keep=linking, status=status)
            except Exception as error:
                history.update(control, f"中断 · {error}")
                raise
            history.update(control, "到达目标" if complete else "手动结束")
            if linking and ending:
                message = ("目标里程已到。" if complete else "回放已结束。") + ending["message"]
            elif linking and control.last_sent()[0] is None:
                message = "已取消等待 Keep 开始。"
            else:
                message = ("目标里程已到，定位已停在终点。请在 Keep 中结束本次跑步。" if complete
                           else "已停止发送定位。请在 Keep 中检查当前运动。")
            return message + (f" 最近回放进度未能保存：{history.error}" if history.error else "")
        self.task("preview" if preview else "live", "准备离线预演…" if preview else "正在连接雷电实例…", run)

    def last_replay_path(self, index):
        return self.settings_path.with_name(f"last-live-{index}.json")

    def show_last_replay(self):
        data = read_settings(self.last_replay_path(self.selected_index))
        try:
            steps = data["steps_sent"]
            step_text = "未启用虚拟计步窗口" if steps is None else f"{steps:,} 步"
            body = (f"{data['route']}  ·  {data['state']}\n{data['updated']}\n\n"
                    f"最后发出的定位    {data['location_m']/1000:.3f} / {data['target_m']/1000:g} 公里\n"
                    f"运动用时    {clock_text(data['elapsed_s'])}\n"
                    f"经度 {data['longitude']:.7f}    纬度 {data['latitude']:.7f}\n"
                    f"最后发出的计步进度    {step_text}")
        except (KeyError, ValueError, TypeError):
            body = "该实例暂无可读取的回放进度。"
        messagebox.showinfo(f"上次回放 · 实例 {self.selected_index}", body, parent=self.root)

    def toggle_pause(self) -> None:
        if self.job == "live" and self.linking:
            self.control.request("RESUME" if self.paused else "PAUSE")
            self.paused = not self.paused
            self.tell("正在等待 Keep 确认继续…" if not self.paused else "回放已暂停，正在同步 Keep…")
            self.update_controls()
            return
        if self.paused:
            self.control.resume()
            self.tell("离线预演中（20 倍速）。" if self.job == "preview" else "正在向雷电发送实时定位。")
        else:
            self.control.pause()
            self.tell("预演已暂停。" if self.job == "preview" else "定位回放已暂停。Keep 的计时由应用内控制。")
        self.paused = not self.paused
        self.update_controls()

    def stop(self) -> None:
        self.control.stop()
        self.update_controls()
        self.tell("正在结束回放…")

    def save_fit(self) -> None:
        plan = self.plan
        if not plan:
            return
        name = f"DUT-EDA-sim-{plan.meters}m-{datetime.now():%Y%m%d-%H%M%S}.fit"
        dest = filedialog.asksaveasfilename(parent=self.root, title="保存模拟跑步 FIT", initialfile=name,
                                            defaultextension=".fit", filetypes=[("FIT 运动文件", "*.fit")])
        if dest:
            def write(_control):
                export_fit(Path(dest), self.track, plan)
                return f"已保存模拟 FIT：{dest}"
            self.task("fit", "正在生成模拟 FIT 文件…", write)

    def send_fit(self) -> None:
        plan = self.plan
        if not plan or not self.console:
            return
        device, index = LDPlayer(self.console), self.selected_index

        def transfer(control):
            if not device.prepare(index, control.cancel):
                return "已取消传输。"
            with TemporaryDirectory(prefix="keep-eda-") as folder:
                path = Path(folder) / "activity.fit"
                export_fit(path, self.track, plan)
                if control.cancel.is_set():
                    return "已取消传输。"
                name = device.send_fit(index, path, plan.meters)
            return f"已传入实例 {index}。在 Keep 的运动数据文件导入中选择「下载」里的 {name}。"
        self.task("transfer", "正在生成并传输模拟 FIT…", transfer)

    def open_batch(self) -> None:
        if self.job or self.plan is None:
            return
        if self.batch_dialog and self.batch_dialog.window.winfo_exists():
            self.batch_dialog.window.lift()
            return
        track, plan, index = self.track, self.plan, self.selected_index
        device = LDPlayer(self.console) if self.console and self.devices else None

        def submit(runs, destination):
            if self.job:
                self.batch_dialog.finish("控制台正在连接雷电，请稍后再次点击。", False)
                return
            draft = self.batch_dialog.snapshot()
            batch_track, batch_plan = CampusTrack(draft.route_id), draft.plan
            def write(control):
                def progress(done, total):
                    self.events.put(("batch_progress", f"已生成 {done} / {total} 条运动。"))

                def remember(path, kind, target):
                    try:
                        self.batch_store.record_export(path, kind, target, draft, runs)
                        return ""
                    except (OSError, sqlite3.Error) as error:
                        return f" 导出已完成，但历史保存失败：{error}"

                with TemporaryDirectory(prefix="keep-eda-batch-") as folder:
                    path = destination or Path(folder, archive_name(runs))
                    if not export_batch(path, batch_track, batch_plan, runs, control.cancel, progress):
                        return "已取消批量生成。"
                    if destination:
                        return f"已将 {len(runs)} 条运动保存到 {path}。" + remember(path, "保存 ZIP", str(path))
                    self.events.put(("batch_progress", f"正在向雷电实例 {index} 发送 ZIP…"))
                    if not device.prepare(index, control.cancel) or control.cancel.is_set():
                        return "已取消发送。"
                    try:
                        name = device.send_batch_zip(index, path, control.cancel)
                    except InterruptedError:
                        return "已取消发送。"
                    note = remember(path, "发送到雷电", f"实例 {index} /sdcard/Download/{name}")
                return f"已发送 {len(runs)} 条运动。请在 Keep 的文件导入页选择「下载」里的 {name}。" + note

            self.task("batch", "正在生成批量 FIT 文件…", write)

        self.batch_dialog = BatchDialog(self.root, track, plan, index if device else None,
                                        submit, lambda: self.control.stop(), self.batch_store)

    def poll(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "devices":
                    self.show_instances(value)
                elif kind == "batch_progress":
                    self.tell(value)
                    if self.batch_dialog and self.batch_dialog.window.winfo_exists():
                        self.batch_dialog.message(value)
                elif kind == "sample":
                    self.sample(value)
                    if self.job in ("live", "preview") and not self.has_sample:
                        self.has_sample = True
                        if not self.control.cancel.is_set():
                            if self.job == "preview":
                                self.tell("离线预演中（20 倍速）。")
                            else:
                                if value.virtual_steps:
                                    status = "正在向计步窗口发送 GPS、Android 步数和跑步加速度。"
                                elif value.motion_sent:
                                    status = "正在发送 GPS 和雷电加速度。使用普通窗口时，Keep 可能少计步数。"
                                else:
                                    status = "正在发送 GPS。"
                                self.tell(status + (PERMISSION_HINT if value.activity_allowed is False else ""),
                                          value.activity_allowed is False)
                        self.update_controls()
                elif kind == "live_state":
                    state, message = value
                    if state in {"paused", "running"}:
                        self.paused = state == "paused"
                        self.update_controls()
                    self.tell(message, state == "notice")
                else:
                    if self.job == "batch" and self.batch_dialog and self.batch_dialog.window.winfo_exists():
                        self.batch_dialog.finish(value, kind == "error")
                    self.job = ""
                    self.update_controls()
                    self.tell(value, kind == "error")
        except Empty:
            pass
        if self.closing and not self.job:
            self.root.destroy()
        else:
            self.root.after(60, self.poll)

    def close(self) -> None:
        if self.closing:
            return
        self.save_settings()
        self.closing = True
        self.control.stop()
        if self.job:
            self.root.title("正在关闭 · 开发区校园跑")
            self.tell("正在结束当前操作，完成后会自动关闭。")
            self.update_controls()
        else:
            self.root.destroy()


def main() -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    Dashboard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
