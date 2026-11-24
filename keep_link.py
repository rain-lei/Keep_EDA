"""Keep start/pause coordination and a watchdog for the two replay producers."""

from threading import Event, Thread
import time

from ldplayer import DeviceError


class RunMonitor:
    def __init__(self, control, bridge=None, status=lambda *_: None, *, with_steps=False, check_running=lambda: None):
        self.control, self.bridge, self.status = control, bridge, status
        self.check_running = check_running
        self.done = Event()
        self.thread = None
        self.error = None
        self.started = False
        self.last_gps = time.monotonic()
        self.with_steps = with_steps
        self.last_step = self.last_gps
        self.unknown_since = None
        self.sent_action = None
        self.requested_at = 0.0
        self.next_window_check = 0.0

    def positioned(self):
        self.last_gps = time.monotonic()

    def stepped(self):
        self.last_step = time.monotonic()

    def _check_window(self):
        try:
            self.bridge.check_keep_window()
        except DeviceError:
            self.started = False
            raise
        self.next_window_check = time.monotonic() + 3

    def wait_for_start(self, position_start=lambda: None) -> bool:
        self.status("waiting", "请在计步窗口进入「户外跑步」，等控制台提示可以点 GO。")
        armed = False
        unknown_since = None
        while not self.control.cancel.is_set():
            self.check_running()
            if time.monotonic() >= self.next_window_check:
                self._check_window()
            state = self.bridge.keep_state()
            if self.control.cancel.is_set():
                return False
            if state == "UNKNOWN":
                now = time.monotonic()
                if unknown_since is None:
                    unknown_since = now
                elif now - unknown_since >= (15 if armed else 60):
                    raise DeviceError("未能识别 Keep 的开跑页面，已取消等待。请回到能看到 GO 按钮的页面重新待命。")
            else:
                unknown_since = None
            if state == "PAUSED" or (state == "RUNNING" and not armed):
                raise DeviceError("Keep 里已有正在进行或已暂停的运动。请先自行处理，再回到有 GO 按钮的页面重新待命。")
            if state == "READY" and not armed:
                self._check_window()
                if self.control.cancel.is_set():
                    return False
                position_start()
                armed = True
                self.status("armed", "起点已就绪。现在可以在计步窗口点 GO，倒计时结束后会自动回放。")
            elif state == "RUNNING":
                self._check_window()
                if self.control.cancel.is_set():
                    return False
                self.started = True
                return True
            self.control.cancel.wait(.2)
        return False

    def start(self):
        self.positioned()
        self.stepped()
        self.control.start_live_clock(linked=self.bridge is not None)
        self.thread = Thread(target=self._watch, daemon=True, name="keep-eda-monitor")
        self.thread.start()

    def _sync_keep(self):
        if time.monotonic() >= self.next_window_check:
            self._check_window()
        state = self.bridge.keep_state()
        now = time.monotonic()
        if self.done.is_set() or self.control.cancel.is_set():
            return
        if state == "READY":
            self.started = False
            raise DeviceError("Keep 已返回 GO 页面，本次回放已停止。")
        if state == "UNKNOWN":
            if self.unknown_since is None:
                self.control.pause()
                self.unknown_since = now
                self.status("checking", "暂时读不到 Keep 跑步页面，已暂停定位与计步。")
            elif now - self.unknown_since >= 3:
                self.started = False
                raise DeviceError("无法持续识别 Keep 跑步页面，回放已停止。请检查是否离开跑步页或锁屏。")
            return

        paused = state == "PAUSED"
        action = self.control.pending_action
        if action is not None and (action == "PAUSE") != paused:
            if self.sent_action != action:
                self.bridge.keep_action(action)
                self.sent_action, self.requested_at = action, now
            elif now - self.requested_at >= 4:
                raise DeviceError("Keep 未确认起停操作，回放已停止。请检查计步窗口中的跑步页面。")
            return

        changed = self.control.sync_keep(paused)
        if changed or self.unknown_since is not None:
            self.positioned()
            self.stepped()
            if paused:
                self.status("paused", "Keep 与定位、计步已暂停。点击继续可一起恢复。")
            else:
                self.status("running", "Keep 已继续，定位与计步同步恢复。")
        self.unknown_since = self.sent_action = None

    def _watch(self):
        was_paused = self.control.paused
        suspensions = self.control.suspensions
        try:
            while not self.done.wait(.2) and not self.control.cancel.is_set():
                self.check_running()
                now = time.monotonic()
                self.control.elapsed()  # Whichever producer wakes first observes the same sleep adjustment.
                if self.control.paused or was_paused:
                    self.positioned()
                    self.stepped()
                was_paused = self.control.paused
                if self.control.suspensions != suspensions:
                    suspensions = self.control.suspensions
                    if self.bridge:
                        self.status("syncing", "电脑休眠后已暂停回放，正在等待 Keep 确认暂停。")
                    else:
                        self.status("paused", "电脑休眠后已暂停回放。点击继续可恢复。")
                if not self.control.paused and now-self.last_gps > 3:
                    raise DeviceError("定位通道超过 3 秒没有响应，定位与计步已一同停止。请检查雷电并处理 Keep 中的当前运动。")
                if self.with_steps and not self.control.paused and now-self.last_step > 3:
                    raise DeviceError("计步通道超过 3 秒没有响应，定位与计步已一同停止。请检查雷电计步窗口。")
                if self.bridge:
                    self._sync_keep()
        except Exception as error:
            self.error = error
            self.control.stop()

    def check(self):
        if self.error:
            raise self.error

    def close(self):
        self.done.set()
        if self.thread:
            self.thread.join()  # Device commands and socket reads each have a timeout.
        if not self.bridge or not self.started:
            return
        try:
            self.check_running()
            self._check_window()
            state = self.bridge.keep_state()
            settling_resume = self.sent_action == "RESUME" and state == "PAUSED"
            if state == "RUNNING" or settling_resume:
                # ACTION acknowledges receipt. A just-sent RESUME can still be queued
                # while STATE reports the previous PAUSED scan for up to 1.8 seconds.
                self.bridge.keep_action("PAUSE")
                now = time.monotonic()
                confirm_after = now + 1.8 if settling_resume else now
                deadline = now + 4
                previous = state
                while time.monotonic() < deadline:
                    state = self.bridge.keep_state()
                    if state in {"UNKNOWN", "READY"}:
                        break
                    if state == "PAUSED" and time.monotonic() >= confirm_after:
                        break
                    if state == "RUNNING" and previous == "PAUSED":
                        self.bridge.keep_action("PAUSE")
                    previous = state
                    time.sleep(.15)
            if state != "PAUSED":
                self.status("notice", "定位与计步已停止；未能确认 Keep 已暂停，请在 Keep 中检查。")
            else:
                self.status("ended", "Keep 已自动暂停，定位与计步已停止。可在 Keep 中查看并结束本次运动。")
        except DeviceError:
            self.status("notice", "计步窗口连接中断，无法确认 Keep 已暂停，请在雷电中检查。")
