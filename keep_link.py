"""Keep start/pause coordination and a watchdog for the two replay producers."""

from threading import Event, Thread
import time

from ldplayer import DeviceError


class RunMonitor:
    def __init__(self, control, bridge=None, status=lambda *_: None, *, with_steps=False):
        self.control, self.bridge, self.status = control, bridge, status
        self.done = Event()
        self.thread = None
        self.error = None
        self.started = False
        self.last_gps = time.monotonic()
        self.with_steps = with_steps
        self.last_step = self.last_gps

    def positioned(self):
        self.last_gps = time.monotonic()

    def stepped(self):
        self.last_step = time.monotonic()

    def wait_for_start(self) -> bool:
        self.status("waiting", "请在底部有「关闭计步窗口」的 Keep 页面开始户外跑步。倒计时结束后会自动回放。")
        next_window_check = time.monotonic() + 3
        while not self.control.cancel.is_set():
            state = self.bridge.keep_state()
            if state == "RUNNING" or time.monotonic() >= next_window_check:
                self.bridge.check_keep_window()
                next_window_check = time.monotonic() + 3
            if self.control.cancel.is_set():
                return False
            if state == "RUNNING":
                self.started = True
                return True
            self.control.cancel.wait(.2)
        return False

    def start(self):
        self.positioned()
        self.stepped()
        self.thread = Thread(target=self._watch, daemon=True, name="keep-eda-monitor")
        self.thread.start()

    def _watch(self):
        unknown_since = None
        pending = None
        requested_at = 0
        paused = False
        was_paused = self.control.paused
        suspensions = self.control.suspensions
        try:
            while not self.done.wait(.2) and not self.control.cancel.is_set():
                now = time.monotonic()
                self.control.elapsed()  # Whichever producer wakes first observes the same sleep adjustment.
                if self.control.paused or was_paused:
                    self.positioned()
                    self.stepped()
                was_paused = self.control.paused
                if self.control.suspensions != suspensions:
                    suspensions = self.control.suspensions
                    if self.bridge:
                        self.control.request("PAUSE")
                    else:
                        self.control.pause()
                    self.status("paused", "检测到电脑暂停或休眠，回放已暂停。检查 Keep 后可继续。")
                if not self.control.paused and now-self.last_gps > 3:
                    raise DeviceError("定位通道超过 3 秒没有响应，定位与计步已一同停止。请检查雷电并处理 Keep 中的当前运动。")
                if self.with_steps and not self.control.paused and now-self.last_step > 3:
                    raise DeviceError("计步通道超过 3 秒没有响应，定位与计步已一同停止。请检查雷电计步窗口。")
                if self.bridge is None:
                    continue
                action = self.control.pop_action()
                if action:
                    pending, requested_at = action, now
                    self.bridge.keep_action(action)
                state = self.bridge.keep_state()
                if pending:
                    wanted = "PAUSED" if pending == "PAUSE" else "RUNNING"
                    if state == wanted:
                        pending = None
                    elif now-requested_at > 4:
                        raise DeviceError("Keep 未确认起停操作，回放已停止。请检查计步窗口中的跑步页面。")
                    else:
                        continue
                if state in {"UNKNOWN", "READY"}:
                    if unknown_since is None:
                        unknown_since = now
                    if now-unknown_since > 3:
                        raise DeviceError("无法持续识别 Keep 跑步页面，定位与计步已停止。请检查是否离开跑步页或锁屏。")
                    continue
                unknown_since = None
                if state == "PAUSED" and not paused:
                    self.control.pause()
                    paused = True
                    self.status("paused", "Keep 与定位、计步已暂停。点击继续可一起恢复。")
                elif state == "RUNNING" and (paused or self.control.paused):
                    self.positioned()
                    self.stepped()
                    self.control.resume()
                    paused = False
                    self.status("running", "Keep 已继续，定位与计步同步恢复。")
        except Exception as error:
            self.error = error
            self.control.stop()

    def check(self):
        if self.error:
            raise self.error

    def close(self):
        self.done.set()
        if self.thread:
            self.thread.join(4)
        if not self.bridge or not self.started:
            return
        try:
            state = self.bridge.keep_state()
            if state == "RUNNING":
                self.bridge.keep_action("PAUSE")
                deadline = time.monotonic()+4
                while time.monotonic() < deadline:
                    state = self.bridge.keep_state()
                    if state == "PAUSED":
                        break
                    time.sleep(.15)
            if state != "PAUSED":
                self.status("notice", "定位与计步已停止；未能确认 Keep 已暂停，请在 Keep 中检查。")
            else:
                self.status("ended", "Keep 已自动暂停，定位与计步已停止。可在 Keep 中查看并结束本次运动。")
        except DeviceError:
            self.status("notice", "计步窗口连接中断，无法确认 Keep 已暂停，请在雷电中检查。")
