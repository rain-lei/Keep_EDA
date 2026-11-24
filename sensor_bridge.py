"""Connect the selected LDPlayer instance to Android 14's virtual running sensors."""

from hashlib import sha256
from math import isfinite
from pathlib import Path
import re
import socket
import subprocess
from threading import RLock, Thread
import time

from ldplayer import DeviceError, KEEP_PACKAGE, LDPlayer, wait_for_android
from motion import Motion


PACKAGE = "xyz.keepeda.sensors"
ADDRESS = "02:4B:45:44:41:14"
APK = Path(__file__).resolve().parent / "data" / "keepeda-sensors.apk"


class VirtualPedometer:
    def __init__(self, device: LDPlayer, index: int, port: int, sock: socket.socket, reader, display_id: int, version=1):
        self.device, self.index, self.port = device, index, port
        self.sock, self.reader = sock, reader
        self.display_id = display_id
        self.active = False
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
            match = re.fullmatch(rb"EDA([123]) ([1-9]\d*)", banner)
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
        if state not in {"READY", "RUNNING", "PAUSED", "UNKNOWN"}:
            raise DeviceError("计步窗口返回了无法识别的 Keep 状态。")
        return state

    def check_keep_window(self) -> None:
        self.device.keep_windows(self.index).require_viewer(self.display_id)

    def keep_action(self, action: str):
        if action not in {"PAUSE", "RESUME"}:
            raise ValueError("未知的 Keep 操作。")
        if self.query("ACTION " + action) != "OK":
            raise DeviceError("Keep 计步窗口未确认起停请求。")

    def begin(self, steps: int):
        self._send(f"BEGIN {steps}", ack=True)

    def orient(self, heading: float, *, immediate=False):
        if self.version < 3:
            raise DeviceError("当前计步窗口缺少方向传感器。请关闭雷电里的旧计步窗口，再从新版控制台重新打开。")
        if not isfinite(heading):
            raise ValueError("朝向必须是有限角度。")
        self._send(f"HEADING {heading % 360:.6f} {int(immediate)}", ack=True)

    def sample(self, motion: Motion, *, essential=True):
        self.active = True
        self._send(f"S {motion.footfalls:.8f} {motion.cadence:.6f} {motion.step_length:.6f}")

    def rest(self):
        if self.active:
            self._send("REST", ack=True)
            self.active = False

    def finish(self, steps: int):
        self._send(f"END {steps}", ack=True)
        self.active = False

    def close(self):
        try:
            self.rest()
        finally:
            self.reader.close()
            self.sock.close()
            self._remove_forward(self.device, self.index, self.port)


def open_step_viewer(device: LDPlayer, index: int, cancel, *, heading: float | None = None) -> str:
    def adb(command: str, timeout=10):
        return device.command("adb", "--index", str(index), "--command", command, timeout=timeout)

    if wait_for_android(adb, cancel) < 34:
        raise DeviceError("计步窗口需要 Android 14。取消勾选「同步跑步计步」可以打开普通 Keep。")
    if not adb(f"shell pm path {KEEP_PACKAGE}").startswith("package:"):
        raise DeviceError("所选雷电实例中未安装 Keep。")
    device.keep_windows(index).check_primary()
    bridge = VirtualPedometer.connect(device, index)
    reopen = bridge is not None
    if bridge is None:
        bridge = _start_step_bridge(device, index, cancel, adb)
    try:
        if cancel.is_set():
            raise DeviceError("已取消打开计步窗口。")
        if reopen:
            adb(f"shell am start --display 0 --activity-single-top -n {PACKAGE}/.Viewer")
        _ensure_keep_on_display(device, index, bridge.display_id, adb, cancel)
        if heading is not None:
            bridge.orient(heading, immediate=True)
    finally:
        bridge.close()
    return "Keep 已在计步窗口打开。请在底部有「关闭计步窗口」的页面进入户外跑步。"


def _ensure_keep_on_display(device: LDPlayer, index: int, display_id: int, adb, cancel) -> None:
    if device.keep_windows(index).viewer_has_keep(display_id):
        return
    # Android can reuse a Keep task on another display even when am start succeeds.
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
            return
        if windows.viewer_has_keep(display_id):
            return
        cancel.wait(.3)
    raise DeviceError("已取消打开计步窗口。")


def _start_step_bridge(device: LDPlayer, index: int, cancel, adb) -> VirtualPedometer:
    installed = adb(f"shell pm path {PACKAGE}").removeprefix("package:").strip()
    digest = adb(f"shell sha256sum {installed}").split() if installed.startswith("/data/app/") else []
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
    if association is None:
        adb(f"shell cmd companiondevice associate 0 {PACKAGE} {ADDRESS}")
        association = find_association()
    if association is None:
        raise DeviceError("Android 未能建立计步窗口的本地测试设备关联。")
    try:
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
        try:
            adb(f"shell am force-stop {PACKAGE}")
            adb(f"shell cmd companiondevice disassociate 0 {PACKAGE} {ADDRESS}")
        except DeviceError:
            pass
        raise


def orient_step_viewer(device: LDPlayer, index: int, heading: float) -> bool:
    """Align an already open viewer when the user repositions a random start."""
    bridge = VirtualPedometer.connect(device, index)
    if bridge is None:
        return False
    try:
        bridge.orient(heading, immediate=True)
    finally:
        bridge.close()
    return True
