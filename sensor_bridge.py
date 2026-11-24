"""Connect the selected LDPlayer instance to Android 14's virtual running sensors."""

from contextlib import ExitStack
from hashlib import sha256
from math import isfinite
from pathlib import Path
import re
import socket
import subprocess
from threading import RLock, Thread
import time

from ldplayer import DeviceError, KEEP_PACKAGE, LDPlayer, VIEWER_PACKAGE, wait_for_android
from motion import Motion


PACKAGE = VIEWER_PACKAGE
ADDRESS = "02:4B:45:44:41:14"
APK = Path(__file__).resolve().parent / "data" / "keepeda-sensors.apk"


class VirtualPedometer:
    def __init__(self, device: LDPlayer, index: int, port: int, sock: socket.socket, reader, display_id: int, version=1):
        self.device, self.index, self.port = device, index, port
        self.sock, self.reader = sock, reader
        self.display_id = display_id
        self.active = False
        self.prior_steps = 0
        self.version = version
        self.lock = RLock()

    @classmethod
    def connect(cls, device: LDPlayer, index: int):
        port = None
        sock = reader = None
        try:
            result = device.command("adb", "--index", str(index), "--command",
                                    "forward tcp:0 localabstract:xyz.keepeda.sensors.v1")
            port = int(result.strip())
            if not 0 < port <= 65535:
                return None
            sock = socket.create_connection(("127.0.0.1", port), timeout=1.5)
            reader = sock.makefile("rb")
            banner = reader.readline(64).strip()
            match = re.fullmatch(rb"EDA([1-4]) ([1-9]\d*)", banner)
            if match:
                return cls(device, index, port, sock, reader, int(match[2]), int(match[1]))
        except (OSError, ValueError, DeviceError):
            pass
        if reader is not None:
            reader.close()
        if sock is not None:
            sock.close()
        if port is not None:
            cls._remove_forward(device, index, port)
        return None

    @staticmethod
    def _remove_forward(device, index, port):
        try:
            device.command("adb", "--index", str(index), "--command", f"forward --remove tcp:{port}")
        except DeviceError:
            pass

    def _send(self, line: str, *, ack=False):
        try:
            with self.lock:
                self.sock.sendall((line+"\n").encode("ascii"))
                if ack and self.reader.readline(32).strip() != b"OK":
                    raise DeviceError("计步窗口没有确认传感器命令。")
        except OSError as error:
            raise DeviceError("Android 计步窗口已断开，请在雷电中检查该窗口。") from error

    def query(self, command: str) -> str:
        if self.version < 2:
            raise DeviceError("请先关闭旧计步窗口，再从控制台重新打开，以更新起停联动组件。")
        with self.lock:
            self._send(command)
            try:
                reply = self.reader.readline(32)
                if not reply.endswith(b"\n"):
                    raise DeviceError("计步窗口状态响应中断。")
                return reply.decode("utf-8").strip()
            except OSError as error:
                raise DeviceError("读取 Keep 状态失败，计步窗口可能已断开。") from error

    def keep_state(self) -> str:
        state = self.query("STATE")
        if state not in {"READY", "RUNNING", "PAUSED", "UNKNOWN", "LOCATION_PROMPT"}:
            raise DeviceError("计步窗口返回了无法识别的 Keep 状态。")
        return state

    def check_keep_window(self) -> None:
        self.device.keep_windows(self.index).require_viewer(self.display_id)

    def restore_empty_window(self, cancel):
        def adb(command, timeout=10):
            return self.device.command("adb", "--index", str(self.index), "--command", command, timeout=timeout)
        windows = self.device.keep_windows(self.index)
        if self.display_id not in windows.empty_displays:
            windows.require_viewer(self.display_id)
            return
        _ensure_keep_on_display(self.device, self.index, self.display_id, adb, cancel)

    def keep_action(self, action: str):
        if action not in {"SELECT_RUN", "GO", "PAUSE", "RESUME"}:
            raise ValueError("未知的 Keep 操作。")
        if action in {"SELECT_RUN", "GO"} and self.version < 4:
            raise DeviceError("当前计步窗口需要在 Keep 手动点 GO。关闭旧计步窗口再重新打开即可升级。")
        # A v4 action waits for a fresh accessibility scan and Android's click result.
        with self.lock:
            timeout = self.sock.gettimeout()
            self.sock.settimeout(4)
            try:
                reply = self.query("ACTION " + action)
            finally:
                self.sock.settimeout(timeout)
        if self.version < 4 and reply == "OK":
            return True
        if self.version >= 4 and reply in {"CLICKED", "ALREADY"}:
            return True
        if self.version >= 4 and reply in {"MISSING", "REJECTED"}:
            return False
        raise DeviceError("计步窗口返回了无法识别的起停操作结果。")

    def begin(self, steps: int, *, prior_steps=0):
        self._send(f"BEGIN {steps-prior_steps}", ack=True)
        self.prior_steps = prior_steps

    def orient(self, heading: float, *, immediate=False):
        if self.version < 3:
            raise DeviceError("当前计步窗口缺少方向传感器。请关闭雷电里的旧计步窗口，再从新版控制台重新打开。")
        if not isfinite(heading):
            raise ValueError("朝向必须是有限角度。")
        self._send(f"HEADING {heading % 360:.6f} {int(immediate)}", ack=True)

    def sample(self, motion: Motion, *, essential=True):
        self.active = True
        remaining_phase = max(0.0, motion.footfalls - self.prior_steps)
        self._send(f"S {remaining_phase:.8f} {motion.cadence:.6f} {motion.step_length:.6f}")

    def rest(self):
        if self.active:
            self._send("REST", ack=True)
            self.active = False

    def finish(self, steps: int):
        self._send(f"END {steps-self.prior_steps}", ack=True)
        self.active = False

    def close(self):
        with ExitStack() as cleanup:
            cleanup.callback(self._remove_forward, self.device, self.index, self.port)
            cleanup.callback(self.sock.close)
            cleanup.callback(self.reader.close)
            self.rest()


def open_step_viewer(device: LDPlayer, index: int, cancel, *, heading: float | None = None) -> str:
    def adb(command: str, timeout=10):
        return device.command("adb", "--index", str(index), "--command", command, timeout=timeout)

    if wait_for_android(adb, cancel) < 34:
        raise DeviceError("计步窗口需要 Android 14。取消勾选「同步跑步计步」可以打开普通 Keep。")
    if not adb(f"shell pm path {KEEP_PACKAGE}").startswith("package:"):
        raise DeviceError("所选雷电实例中未安装 Keep。")
    if cancel.is_set():
        raise InterruptedError("已取消打开计步窗口。")
    windows = device.keep_windows(index)
    windows.check_primary()
    bridge = VirtualPedometer.connect(device, index)
    if bridge is None and (windows.viewer_visible or any(i > 0 and keep for i, keep in windows.has_keep.items())):
        if cancel.wait(.25):
            raise InterruptedError("已取消打开计步窗口。")
        bridge = VirtualPedometer.connect(device, index)
        if bridge is None:
            raise DeviceError("已有计步窗口，但连接未响应。请检查原窗口，处理好当前运动后再重新打开。")
    reopen = bridge is not None
    if bridge is None:
        bridge = _start_step_bridge(device, index, cancel, adb)
    try:
        if cancel.is_set():
            raise DeviceError("已取消打开计步窗口。")
        if reopen:
            adb(f"shell am start --display 0 --activity-single-top -n {PACKAGE}/.Viewer")
        windows = _ensure_keep_on_display(device, index, bridge.display_id, adb, cancel)
        if heading is not None:
            if bridge.display_id in windows.outdoor_displays and not _at_start_screen(bridge, cancel):
                return "已返回 Keep 计步窗口。请先在 Keep 中处理当前运动，再开始新的回放。"
            bridge.orient(heading, immediate=True)
    finally:
        bridge.close()
    return "Keep 已在计步窗口打开。请在底部有「关闭计步窗口」的页面进入户外跑步。"


def _ensure_keep_on_display(device: LDPlayer, index: int, display_id: int, adb, cancel):
    windows = device.keep_windows(index)
    if windows.viewer_has_keep(display_id) and windows.viewer_visible:
        return windows
    # Android can reuse a Keep task on another display even when am start succeeds.
    if not windows.has_keep[display_id]:
        resolved = adb(f"shell cmd package resolve-activity --brief -a android.intent.action.MAIN "
                       f"-c android.intent.category.LAUNCHER {KEEP_PACKAGE}")
        component = next((line.strip() for line in resolved.splitlines()
                          if re.fullmatch(re.escape(KEEP_PACKAGE) + r"/[\w.$]+", line.strip())), None)
        if not component:
            raise DeviceError("找不到 Keep 的启动入口。")
        if cancel.is_set():
            raise DeviceError("已取消打开计步窗口。")
        adb(f"shell am start --display {display_id} -n {component}")
    deadline = time.monotonic() + 4
    while not cancel.is_set():
        windows = device.keep_windows(index)
        if time.monotonic() >= deadline:
            windows.require_viewer(display_id)
            return windows
        if windows.viewer_has_keep(display_id) and windows.viewer_visible:
            return windows
        cancel.wait(.3)
    raise DeviceError("已取消打开计步窗口。")


def _start_step_bridge(device: LDPlayer, index: int, cancel, adb) -> VirtualPedometer:
    installed = adb(f"shell pm path {PACKAGE}").removeprefix("package:").strip()
    digest = adb(f"shell sha256sum {installed}").split() if installed.startswith("/data/app/") else []
    if cancel.is_set():
        raise InterruptedError("已取消打开计步窗口。")
    if not digest or digest[0].lower() != sha256(APK.read_bytes()).hexdigest():
        output = adb(f'install -r --no-incremental "{APK}"', timeout=40)
        if "Success" not in output:
            raise DeviceError(f"Android 计步组件安装失败。{output[:160]}")

    def find_association():
        for line in adb("shell cmd companiondevice list 0").splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[0] == PACKAGE and fields[1].upper() == ADDRESS and fields[2].isdigit():
                return int(fields[2])
        return None

    association = find_association()
    new_association = association is None
    if cancel.is_set():
        raise InterruptedError("已取消打开计步窗口。")
    if new_association:
        adb(f"shell cmd companiondevice associate 0 {PACKAGE} {ADDRESS}")
        association = find_association()
    if association is None:
        raise DeviceError("Android 未能建立计步窗口的本地测试设备关联。")
    runner = None
    try:
        if cancel.is_set():
            raise DeviceError("已取消打开计步窗口。")
        # Android creates an UiAutomation connection only for a waiting runner.
        # Keep that command alive while the emulator window remains open.
        try:
            runner = subprocess.Popen(
                [str(device.executable), "adb", "--index", str(index), "--command",
                 f"shell am instrument -w --no-hidden-api-checks -e association {association} {PACKAGE}/.Controller"],
                cwd=device.executable.parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as error:
            raise DeviceError(f"无法启动 Android 计步组件。{error}") from error
        Thread(target=runner.wait, daemon=True, name="keep-eda-android").start()
        deadline = time.monotonic()+14
        while not cancel.is_set() and time.monotonic() < deadline:
            bridge = VirtualPedometer.connect(device, index)
            if bridge:
                return bridge
            if runner.poll() is not None:
                break
            cancel.wait(.25)
        if cancel.is_set():
            raise DeviceError("已取消打开计步窗口。")
        raise DeviceError("Android 计步窗口未就绪。请确认雷电窗口已解锁，再点击「打开 Keep 计步窗口」。")
    except DeviceError:
        if runner is not None and runner.poll() is None:
            try:
                adb(f"shell am force-stop {PACKAGE}")
            except DeviceError:
                pass
        if new_association:
            try:
                adb(f"shell cmd companiondevice disassociate 0 {PACKAGE} {ADDRESS}")
            except DeviceError:
                pass
        raise


def _at_start_screen(bridge, cancel) -> bool:
    # The Android UI sampler wakes on the first query after an idle period.
    deadline = time.monotonic() + 2
    while not cancel.is_set():
        state = bridge.keep_state()
        if state != "UNKNOWN":
            return state == "READY"
        if time.monotonic() >= deadline:
            return False
        cancel.wait(.2)
    raise InterruptedError("已取消定位。")


def locate_start(device, index, point, heading, cancel, *, motion, linked):
    windows = device.keep_windows(index) if linked else None
    if windows:
        windows.check_primary()
    device.check_location(index)
    bridge = VirtualPedometer.connect(device, index) if motion else None
    try:
        if windows and windows.outdoor_displays:
            if bridge is None or bridge.display_id not in windows.outdoor_displays:
                raise DeviceError("已有户外跑步页面，请先在 Keep 中检查当前运动。")
            windows.require_viewer(bridge.display_id)
            if not _at_start_screen(bridge, cancel):
                raise DeviceError("当前户外跑步页尚未回到 GO。请先处理当前运动，再重新定位起点。")
        if cancel.is_set():
            raise InterruptedError("已取消定位。")
        if bridge:
            bridge.orient(heading, immediate=True)
        if cancel.is_set():
            raise InterruptedError("已取消定位。")
        device.locate(index, point)
    finally:
        if bridge:
            bridge.close()
