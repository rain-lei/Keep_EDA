"""Keep start/pause coordination and a watchdog for the two replay producers."""

from threading import Event, Thread
import time

from ldplayer import DeviceError, EmptyKeepWindow


class RunMonitor:
    def __init__(self, control, bridge=None, status=lambda *_: None, *, with_steps=False, check_running=lambda: None):
        self.control, self.bridge, self.status = control, bridge, status
        self.check_running = check_running
        self.done = Event()
        self.thread = None
        self.error = None
        self.started = False
        self.pending_start = None
        self.last_gps = time.monotonic()
        self.with_steps = with_steps
        self.last_step = self.last_gps
        self.unknown_since = None
        self.sent_action = None
        self.sent_attempt = None
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
            self.pending_start = None
            raise
        self.next_window_check = time.monotonic() + 3

    def _continue_existing(self, at_start, attach_message) -> bool:
        if not at_start():
            raise DeviceError("Keep 已有暂停的运动，当前位置与本计划的接入点不符。请先处理当前运动。")
        self.status("attach_paused", f"{attach_message()}点「接入并继续」可恢复 Keep 和定位。")
        sent_at = None
        while not self.control.cancel.wait(.2):
            self.check_running()
            if time.monotonic() >= self.next_window_check:
                self._check_window()
            state = self.bridge.keep_state()
            if state == "RUNNING" and sent_at is not None:
                return True
            if state == "READY" or (state == "RUNNING" and sent_at is None):
                raise DeviceError("先前暂停的运动状态已改变，本次接入已取消。")
            if self.control.attach_paused.is_set():
                self.control.attach_paused.clear()
                self._check_window()
                if state != "PAUSED" or not at_start() or self.bridge.keep_state() != "PAUSED":
                    raise DeviceError("Keep 页面或起点已改变，已取消接入。")
                if self.control.cancel.is_set():
                    return False
                if self.bridge.keep_action("RESUME") is not False:
                    self.pending_start = "RESUME"
                    sent_at = time.monotonic()
                    self.status("armed", "已请求继续当前运动，正在等 Keep 恢复计时。")
                else:
                    self.status("attach_paused", "Keep 未接受继续操作，请检查跑步页后再点「接入并继续」。")
            elif sent_at is not None and time.monotonic() - sent_at >= 5 and state == "PAUSED":
                sent_at = None
                self.status("attach_paused", "Keep 仍然暂停。可点「接入并继续」重试。")
        return False

    def wait_for_start(self, position_start=lambda: None, *, click_go=False, at_start=None,
                       attach_message=lambda: "Keep 已在本计划起点暂停。", paused_only=False) -> bool:
        self.status("waiting", "请在计步窗口打开 Keep 的「运动」页，等待出现户外跑步的 GO 按钮。")
        armed = False
        clicked_at = None
        restored = location_prompt = False
        unknown_since = None
        while not self.control.cancel.is_set():
            self.check_running()
            if time.monotonic() >= self.next_window_check:
                try:
                    self._check_window()
                except EmptyKeepWindow:
                    if armed or restored:
                        raise
                    self.status("waiting", "计步窗口里没有页面，正在重新打开 Keep。")
                    self.bridge.restore_empty_window(self.control.cancel)
                    restored = True
                    self._check_window()
            state = self.bridge.keep_state()
            if self.control.cancel.is_set():
                return False
            if paused_only and state == "READY":
                raise DeviceError("断点续跑需要原运动停在暂停页。Keep 现在显示 GO，本次续跑已取消。")
            if state == "PAUSED" and at_start is not None:
                if not self._continue_existing(at_start, attach_message):
                    return False
                state = "RUNNING"
                armed = True
            if state == "LOCATION_PROMPT":
                if not location_prompt:
                    self.status("waiting", "Keep 正显示「精准运动定位」。请在雷电中处理该提示，控制台会等待。")
                location_prompt = True
                unknown_since = None
            elif state == "UNKNOWN":
                now = time.monotonic()
                if unknown_since is None:
                    unknown_since = now
                elif armed and now - unknown_since >= 3 and not location_prompt:
                    self.status("waiting", "正在等 Keep 进入跑步页。若计步窗口有提示，请在 Keep 处理。")
                    location_prompt = True
                elif not armed and now - unknown_since >= 60:
                    raise DeviceError("未能识别 Keep 的开跑页面，已取消等待。请回到能看到 GO 按钮的页面重新待命。")
            else:
                unknown_since = None
            if state == "PAUSED" or (state == "RUNNING" and not armed):
                raise DeviceError("Keep 里已有正在进行或已暂停的运动。请先自行处理，再回到有 GO 按钮的页面重新待命。")
            if state == "READY" and (not armed or self.control.retry_start.is_set()):
                self.control.retry_start.clear()
                self._check_window()
                if self.control.cancel.is_set():
                    return False
                if not armed:
                    position_start()
                armed = True
                location_prompt = False
                clicked_at = None
                if click_go and not self.control.cancel.is_set() and self.bridge.keep_action("SELECT_RUN"):
                    if self.control.cancel.wait(.35):
                        return False
                    self._check_window()
                    if not self.control.cancel.is_set() and self.bridge.keep_action("GO"):
                        self.pending_start = "GO"
                        clicked_at = time.monotonic()
                if clicked_at is None:
                    if click_go:
                        self.status("start_retry", "起点已就绪。可在电脑点「重试开跑」，或在 Keep 确认户外跑步后点 GO。")
                    else:
                        self.status("armed", "起点已就绪。现在可以在计步窗口点 GO。升级计步组件后，电脑也可以直接开跑。")
                else:
                    self.status("armed", "已点击 Keep 的 GO，正在等待倒计时结束。")
            elif state == "READY" and (location_prompt or (clicked_at is not None and time.monotonic() - clicked_at >= 6)):
                clicked_at = None
                self.pending_start = None
                location_prompt = False
                if click_go:
                    self.status("start_retry", "Keep 已回到准备页，可点「重试开跑」或在 Keep 中点 GO。")
                else:
                    self.status("armed", "Keep 已回到准备页，请在 Keep 中点 GO。")
            elif state == "RUNNING":
                self._check_window()
                if self.control.cancel.is_set():
                    return False
                self.started = True
                self.pending_start = None
                return True
            self.control.cancel.wait(.2)
        return False

    def start(self, elapsed=0.0):
        self.positioned()
        self.stepped()
        self.control.start_live_clock(linked=self.bridge is not None, elapsed=elapsed)
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
        if state in {"UNKNOWN", "LOCATION_PROMPT"}:
            if self.unknown_since is None:
                self.control.pause()
                self.unknown_since = now
                self.status("checking", "暂时读不到 Keep 跑步页面，已暂停定位与计步。")
            elif state != "LOCATION_PROMPT" and now - self.unknown_since >= 3:
                self.started = False
                raise DeviceError("无法持续识别 Keep 跑步页面，回放已停止。请检查是否离开跑步页或锁屏。")
            return

        paused = state == "PAUSED"
        action, attempt, retryable = self.control.action_request()
        if action is not None and (action == "PAUSE") != paused:
            if retryable:
                return
            if self.sent_attempt != attempt:
                clicked = self.bridge.keep_action(action)
                self.sent_action, self.sent_attempt, self.requested_at = action, attempt, now
                if clicked is not False:
                    return
            elif now - self.requested_at < 4:
                return
            if self.control.allow_retry(action, attempt):
                label = "暂停" if action == "PAUSE" else "继续"
                self.status("retry", f"Keep 尚未确认{label}，回放保持暂停。可点「重试{label}」，也可在 Keep 中{label}。")
            return

        changed = self.control.sync_keep(paused)
        if changed or self.unknown_since is not None:
            if paused and not self.control.wait_for_position():
                return
            self.positioned()
            self.stepped()
            if paused:
                self.status("paused", "Keep 与定位、计步已暂停。点击继续可一起恢复。")
            else:
                self.status("running", "Keep 已继续，定位与计步同步恢复。")
        self.unknown_since = self.sent_action = self.sent_attempt = None

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
                if not self.control.paused:
                    stale = "定位" if now - self.last_gps > 3 else None
                    if not stale and self.with_steps and now - self.last_step > 3:
                        stale = "计步"
                    if stale:
                        if not self.bridge:
                            raise DeviceError(f"{stale}通道超过 3 秒没有响应，定位与计步已一同停止。")
                        self.control.request("PAUSE")
                        self.status("syncing", f"{stale}暂时没有响应，已保留进度，正在同步暂停 Keep。")
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
        if not self.bridge or (not self.started and self.pending_start is None):
            return
        try:
            self.check_running()
            self._check_window()
            state = self.bridge.keep_state()
            if self.pending_start == "GO":
                # The user can cancel AFTER Android accepted GO but BEFORE Keep ends its countdown.
                deadline = time.monotonic() + 7
                while state in {"READY", "UNKNOWN"} and time.monotonic() < deadline:
                    time.sleep(.2)
                    self.check_running()
                    state = self.bridge.keep_state()
                if state == "RUNNING":
                    self._check_window()
            settling_resume = (self.sent_action == "RESUME" or self.pending_start == "RESUME") and state == "PAUSED"
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
