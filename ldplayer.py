"""Indexed LDPlayer 14 commands. All calls run synchronously off the UI thread."""

from dataclasses import dataclass
from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor, wait
from hashlib import sha256
from math import cos, hypot, pi, sin
import os
from pathlib import Path
import re
import shutil
import socket
from statistics import median
import subprocess
import time
from threading import Event
from uuid import uuid4

from campus import Point
from motion import Motion


KEEP_PACKAGE = "com.gotokeep.keep"
CONSOLE_NAMES = ("ldconsole.exe", "dnconsole.exe")


class DeviceError(RuntimeError):
    pass


def wait_for_android(adb, cancel: Event, *, shared_storage=False) -> int:
    deadline = time.monotonic() + 45
    while not cancel.is_set():
        try:
            version = adb("shell getprop ro.build.version.sdk").strip()
            if version.isdigit() and (not shared_storage or adb("shell getprop sys.boot_completed").strip() == "1"):
                return int(version)
        except DeviceError as error:
            match = re.search(r"device ['\"]emulator-(\d+)['\"] not found", str(error))
            if not match and not re.search(r"\boffline\b|no devices/emulators found", str(error), re.I):
                raise
            if match:
                # A running ADB server may miss this emulator's registration on a cold boot.
                port = int(match[1]) + 1
                if 1 <= port <= 65535:
                    message = f"host:emulator:{port}".encode("ascii")
                    try:
                        with socket.create_connection(("127.0.0.1", 5037), timeout=.6) as server:
                            server.sendall(f"{len(message):04x}".encode("ascii") + message)
                    except OSError:
                        pass
        if time.monotonic() >= deadline:
            raise DeviceError("所选实例的本地 ADB 尚未就绪。请检查雷电的 ADB 调试设置。")
        cancel.wait(.5)
    raise InterruptedError("已取消连接雷电。")


class GravityPulse:
    """Stream the landing, unloading and alternating sway of each footfall."""

    def __init__(self, device: "LDPlayer", index: int, neutral: tuple[float, ...]):
        self.device, self.index, self.neutral = device, index, neutral
        magnitude = hypot(*neutral)
        self.up = tuple(v / magnitude for v in neutral)
        x, y, z = self.up
        horizontal = hypot(x, y)
        self.side = (y/horizontal, -x/horizontal, 0.0) if horizontal > 1e-6 else (1., 0., 0.)
        sx, sy, _ = self.side
        self.forward = (-z*sy, z*sx, x*sy-y*sx)
        # LDPlayer applies a command before its short-lived CLI process exits.
        # Bound overlap so a busy emulator never accumulates old sensor positions.
        self.writer = ThreadPoolExecutor(max_workers=2, thread_name_prefix="keep-eda-gravity")
        self.pending = []
        self.active = False

    def sample(self, motion: Motion, *, essential: bool = True) -> None:
        for future in self.pending[:]:
            if future.done():
                self.pending.remove(future)
                future.result()
        if self.pending and not essential:
            return
        if len(self.pending) == 2:
            self.pending.pop(0).result()
        phase = 2*pi*(motion.footfalls - 0.5)
        impact = min(6.0, max(3.6, 4.8*motion.step_length**0.35))
        vertical = impact*(cos(phase) + 0.24*cos(2*phase))
        sideways = 1.65*sin(pi*motion.footfalls)
        forward = 2.0*sin(phase) - .45*sin(2*phase)
        gain = min(1.0, motion.footfalls)
        vector = tuple(n + gain*(vertical*u + sideways*s + forward*f)
                       for n, u, s, f in zip(self.neutral, self.up, self.side, self.forward))
        self.active = True
        self.pending.append(self.writer.submit(self.device.gravity, self.index, vector))

    def rest(self) -> None:
        pending, self.pending = self.pending, []
        # Both commands must finish before the neutral vector is applied.
        wait(pending)
        try:
            for future in pending:
                future.result()
        finally:
            if self.active:
                self.device.gravity(self.index, self.neutral)
                self.active = False

    def close(self) -> None:
        try:
            self.rest()
        finally:
            self.writer.shutdown()


def console_at(path: Path) -> Path | None:
    if path.name.lower() in CONSOLE_NAMES and path.is_file():
        return path
    return next((path / name for name in CONSOLE_NAMES if (path / name).is_file()), None)


def find_console() -> Path | None:
    candidates = []
    if root := os.environ.get("LDPLAYER14_HOME"):
        candidates.append(Path(root))
    if os.name == "nt":
        import winreg

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                try:
                    with winreg.OpenKey(hive, r"Software\leidian\LDPlayer14", 0, winreg.KEY_READ | view) as key:
                        candidates.append(Path(winreg.QueryValueEx(key, "InstallDir")[0]))
                except OSError:
                    pass
    for drive in "CDEFG":
        candidates.extend([Path(f"{drive}:/leidian/LDPlayer14"), Path(f"{drive}:/LDPlayer/LDPlayer14")])
    if found := shutil.which("ldconsole.exe"):
        candidates.append(Path(found))
    return next((exe for path in candidates if (exe := console_at(path))), None)


@dataclass(frozen=True)
class Instance:
    index: int
    name: str
    booted: bool
    pid: int
    vm_pid: int = -1

    @property
    def label(self) -> str:
        state = "已启动" if self.booted else ("启动中" if self.pid > 0 else "未启动")
        return f"{self.index} · {self.name}（{state}）"


def parse_list(output: str) -> list[Instance]:
    instances = []
    for raw in output.splitlines():
        # Match the two known numeric tails from the right. Titles may contain commas and digits.
        line = raw.strip()
        for tail in (r"(?:,-?\d+){3}", ""):
            match = re.fullmatch(r"(\d+),(.+),-?\d+,-?\d+,([012]),(-?\d+),(-?\d+)" + tail, line)
            if match:
                idx, name, booted, pid, vm_pid = match.groups()
                instances.append(Instance(int(idx), name, booted == "1", int(pid), int(vm_pid)))
                break
    if output.strip() and not instances:
        raise DeviceError(f"无法识别雷电的实例列表：{output[:160]}")
    return instances


def decode_output(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return raw.decode("gb18030", errors="replace").strip()


class LDPlayer:
    def __init__(self, executable: Path):
        self.executable = executable

    @contextmanager
    def reserve_location(self, index: int):
        """Keep different windows from sending interleaved points to one instance."""
        if os.name != "nt":
            yield
            return
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        directory = os.path.normcase(str(self.executable.parent.resolve()))
        identity = sha256(directory.encode("utf-8")).hexdigest()[:20]
        name = f"Local\\KeepEDA-location-{identity}-{index}"
        ctypes.set_last_error(0)
        handle = kernel.CreateMutexW(None, False, name)
        already_open = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS
        if not handle:
            raise DeviceError("无法为此实例预留定位接口。")
        try:
            if already_open:
                raise DeviceError(f"另一窗口正在控制实例 {index} 的定位。请先结束该窗口的回放。")
            yield
        finally:
            kernel.CloseHandle(handle)

    def command(self, *arguments: str, timeout: float = 8) -> str:
        try:
            result = subprocess.run(
                [str(self.executable), *arguments], cwd=self.executable.parent,
                capture_output=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as error:
            raise DeviceError(f"雷电命令 {arguments[0]} 超时。请检查模拟器是否响应。") from error
        except OSError as error:
            raise DeviceError(f"无法调用雷电控制台：{error}") from error
        output = decode_output(result.stdout)
        errors = decode_output(result.stderr)
        detail = "\n".join(part for part in (output, errors) if part)
        if result.returncode or re.search(
            r"(?im)^\s*(?:error(?:\s|:)|failed\b|failure\b|unknown command|invalid (?:parameter|command)|"
            r"permission denied|adb(?:\.exe)?:.*(?:offline|not found|cannot)|device offline|模拟器未启动|操作失败)", detail
        ):
            raise DeviceError(f"雷电 {arguments[0]} 执行失败：{detail[:240] or result.returncode}")
        return output

    def instances(self) -> list[Instance]:
        return parse_list(self.command("list2"))

    def instance(self, index: int) -> Instance:
        for device in self.instances():
            if device.index == index:
                return device
        raise DeviceError(f"找不到雷电实例 {index}，请刷新实例列表。")

    def require_running(self, index: int) -> None:
        if not self.instance(index).booted:
            raise DeviceError(f"雷电实例 {index} 已离线，轨迹发送已停止。")

    @contextmanager
    def watch_instance(self, index: int):
        """Check the selected VM's lifetime without holding the LDPlayer command channel."""
        instance = self.instance(index)
        if not instance.booted:
            raise DeviceError(f"雷电实例 {index} 已离线，轨迹发送已停止。")
        if os.name != "nt":
            yield lambda: self.require_running(index)
            return
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        with ExitStack() as cleanup:
            handles = []
            for pid in {instance.pid, instance.vm_pid}:
                if pid > 0:
                    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                    if handle:
                        cleanup.callback(kernel.CloseHandle, handle)
                        handles.append(handle)

            def check():
                if any(kernel.WaitForSingleObject(handle, 0) != 258 for handle in handles):  # WAIT_TIMEOUT means alive
                    raise DeviceError(f"雷电实例 {index} 已离线，轨迹发送已停止。")

            yield check if handles else lambda: self.require_running(index)

    def prepare(self, index: int, cancel: Event) -> bool:
        if cancel.is_set():
            return False
        device = self.instance(index)
        if not device.booted and device.pid <= 0:
            self.command("launch", "--index", str(index))
        deadline = time.monotonic() + 120
        while not device.booted:
            if cancel.wait(0.5):
                return False
            if time.monotonic() >= deadline:
                raise DeviceError("等待雷电启动超时。请在多开器中查看该实例，再重试。")
            device = self.instance(index)
        return not cancel.is_set()

    def locate(self, index: int, point: Point) -> None:
        longitude, latitude = point
        self.command("locate", "--index", str(index), "--LLI", f"{longitude:.7f},{latitude:.7f}")

    def gravity(self, index: int, vector: tuple[float, ...]) -> None:
        self.command("action", "--index", str(index), "--key", "call.gravity", "--value",
                     ",".join(f"{v:.3f}" for v in vector))

    def accelerations(self, index: int) -> list[tuple[float, ...]]:
        info = self.command("adb", "--index", str(index), "--command", "shell dumpsys sensorservice")
        header = re.search(r"(?im)^[^\r\n:]*accelerometer[^\r\n:]*: last \d+ events[^\r\n]*", info)
        if not header:
            return []
        readings = []
        for line in info[header.end():].splitlines():
            if not line.strip():
                continue
            event = re.match(r"\s*\d+\s+\(ts=([\d.]+),[^)]*\)\s*([\d.-]+),\s*([\d.-]+),\s*([\d.-]+)", line)
            if not event:
                break
            readings.append(tuple(map(float, event.groups())))
        return readings

    def motion_sensor(self, index: int, cancel: Event) -> GravityPulse | None:
        try:
            readings = self.accelerations(index)
        except DeviceError:
            return None  # Native GPS remains available when local ADB is disabled.
        if not readings or cancel.is_set():
            return None
        base = tuple(median(row[i] for row in readings[-5:]) for i in range(1, 4))
        if not 25 < sum(v*v for v in base) < 200:
            return None
        # Measure the HAL offset at rest so that repeated runs do not accumulate bias.
        try:
            self.gravity(index, base)
        except DeviceError:
            return None
        cancel.wait(.45)
        try:
            fresh = [r for r in self.accelerations(index) if r[0] > readings[-1][0]]
        except DeviceError:
            fresh = []
        if fresh:
            observed = tuple(median(row[i] for row in fresh[-3:]) for i in range(1, 4))
            if all(abs(a-b) < 1 for a, b in zip(base, observed)):
                base = tuple(2*a-b for a, b in zip(base, observed))
                self.gravity(index, base)
        return GravityPulse(self, index, base)

    def check_location(self, index: int) -> None:
        try:
            enabled = self.command("adb", "--index", str(index), "--command", "shell cmd location is-location-enabled")
        except DeviceError:
            return  # Native location injection does not require Android debugging.
        if enabled.strip().lower() == "false":
            raise DeviceError("此实例已关闭系统定位。请在 Android 设置中开启位置信息。")

    def keep_activity_permission(self, index: int) -> bool | None:
        try:
            info = self.command("adb", "--index", str(index), "--command", f"shell dumpsys package {KEEP_PACKAGE}")
            match = re.search(r"android\.permission\.ACTIVITY_RECOGNITION:\s*granted=(true|false)", info)
            if not match:
                return None
            if match.group(1) == "false":
                return False
            ops = self.command("adb", "--index", str(index), "--command",
                               f"shell cmd appops get {KEEP_PACKAGE} ACTIVITY_RECOGNITION")
            return not bool(re.search(r"ACTIVITY_RECOGNITION:\s*(ignore|deny|errored)", ops, re.I))
        except DeviceError:
            return None

    def open_keep_permissions(self, index: int) -> str:
        self.command("adb", "--index", str(index), "--command",
                     f"shell am start -a android.settings.APPLICATION_DETAILS_SETTINGS -d package:{KEEP_PACKAGE}")
        return "在 Keep 的应用信息里点「权限 → 身体活动」，可查看或更改授权。"

    def open_keep(self, index: int) -> str:
        # The native launch command also works on installations with ADB disabled.
        try:
            package = self.command("adb", "--index", str(index), "--command", f"shell pm path {KEEP_PACKAGE}")
        except DeviceError:
            package = None
        if package is not None and not package.strip():
            raise DeviceError(f"实例 {index} 中未找到 Keep。请从 Keep 官网下载安卓版，拖入这个实例安装后重试。")
        self.command("runapp", "--index", str(index), "--packagename", KEEP_PACKAGE)
        if package is None or "package:" not in package:
            return "已请求雷电打开 Keep。请在模拟器中确认应用已打开。"
        return "已打开 Keep。进入户外跑步，在地图上确认起点。"

    def send_fit(self, index: int, source: Path, meters: int) -> str:
        name = f"DUT-EDA-sim-{meters}m-{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}.fit"
        return self._send_download(index, source, name)

    def send_batch_zip(self, index: int, source: Path, cancel: Event | None = None) -> str:
        digest = sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b""):
                if cancel and cancel.is_set():
                    raise InterruptedError("已取消发送。")
                digest.update(chunk)
        short_name = re.sub(r"[^A-Za-z0-9_-]", "_", source.stem)[:100]
        name = f"{short_name}-{digest.hexdigest()[:10]}.zip"
        return self._send_download(index, source, name, digest.hexdigest(), cancel)

    def _send_download(self, index: int, source: Path, name: str, digest: str | None = None,
                       cancel: Event | None = None) -> str:
        target = f"/sdcard/Download/{name}"
        partial = f"{target}.{uuid4().hex[:12]}.part"
        size = str(source.stat().st_size)

        def adb(command: str, timeout=20) -> str:
            return self.command("adb", "--index", str(index), "--command", command, timeout=timeout)

        wait_for_android(adb, cancel or Event(), shared_storage=True)
        try:
            if cancel and cancel.is_set():
                raise InterruptedError("已取消发送。")
            adb(f'push "{source}" {partial}', timeout=180 if digest else 20)
            if adb(f"shell stat -c %s {partial}").strip() != size:
                raise DeviceError("传输校验失败。请检查该实例的本地 ADB 调试和下载目录。")
            if digest and adb(f"shell sha256sum {partial}", timeout=120).split()[:1] != [digest]:
                raise DeviceError("ZIP 内容校验失败，请重新发送。")
            if cancel and cancel.is_set():
                raise InterruptedError("已取消发送。")
            adb(f"shell mv {partial} {target}")
            if adb(f"shell stat -c %s {target}").strip() != size:
                raise DeviceError("未在雷电下载目录中找到完整的运动文件。")
        except (DeviceError, InterruptedError):
            try:
                adb(f"shell rm -f {partial}")
            except DeviceError:
                pass
            raise
        return name
