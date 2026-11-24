"""Interruptible, wall-clock-paced replay on the same geometry used for previews."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from math import floor, inf
from threading import Condition, Event
import time

from campus import CampusTrack, Point, RunPlan, distance_m
from ldplayer import DeviceError, GravityPulse, LDPlayer
from keep_link import RunMonitor
from sensor_bridge import VirtualPedometer, open_step_viewer


class PlaybackControl:
    def __init__(self, now: Callable[[], float] = time.monotonic):
        self.cancel = Event()
        self.retry_start = Event()
        self.attach_paused = Event()
        self._changed = Condition()
        self._now = now
        self._elapsed = 0.0
        self._since: float | None = None
        self.suspensions = 0
        self._pause_on_sleep = False
        self._linked = False
        self._action = None
        self._attempt = 0
        self._retryable = False
        self._last_location = None
        self._last_steps = None
        self._position_in_flight = False

    def note_location(self, sample):
        with self._changed:
            self._last_location = sample

    def note_virtual_steps(self, count: int):
        with self._changed:
            self._last_steps = count

    def last_sent(self):
        with self._changed:
            return self._last_location, self._last_steps

    @contextmanager
    def position_write(self):
        with self._changed:
            allowed = self._since is not None and not self.cancel.is_set()
            self._position_in_flight = allowed
        try:
            yield allowed
        finally:
            with self._changed:
                self._position_in_flight = False
                self._changed.notify_all()

    def wait_for_position(self):
        with self._changed:
            self._changed.wait_for(lambda: not self._position_in_flight or self.cancel.is_set())
            return not self.cancel.is_set()

    def request(self, action: str) -> bool:
        with self._changed:
            if action not in ("PAUSE", "RESUME"):
                raise ValueError("未知的起停操作。")
            if self.cancel.is_set() or (self._action is not None and (not self._retryable or self._action != action)):
                return False
            if action == "PAUSE":
                self.pause()
            self._action = action
            self._attempt += 1
            self._retryable = False
            self._changed.notify_all()
            return True

    @property
    def pending_action(self):
        with self._changed:
            return self._action

    def action_request(self):
        with self._changed:
            return self._action, self._attempt, self._retryable

    def allow_retry(self, action, attempt):
        with self._changed:
            if (self._action, self._attempt) != (action, attempt) or self.cancel.is_set():
                return False
            self._retryable = True
            return True

    def sync_keep(self, paused: bool) -> bool:
        """Apply an observed state without undoing a newer desktop request."""
        with self._changed:
            wanted = "PAUSE" if paused else "RESUME"
            if self.cancel.is_set() or self._action not in (None, wanted):
                return False
            was_paused = self.paused
            changed = self._action is not None or was_paused != paused
            self._action = None
            self._retryable = False
            if was_paused != paused:
                if paused:
                    self.pause()
                else:
                    self.resume()
            return changed

    def start_live_clock(self, *, linked=False, elapsed=0.0):
        with self._changed:
            self._pause_on_sleep = True
            self._linked = linked
            self._elapsed = elapsed
            self.resume()

    def _advance(self) -> float:
        """One authoritative timeline, even when GPS and sensors wake at different times."""
        if self._since is not None:
            now = self._now()
            delta = max(0.0, now-self._since)
            if delta > 5:
                self.suspensions += 1
                if self._pause_on_sleep:
                    self._since = None
                    if self._linked:
                        self._action = "PAUSE"
                        self._attempt += 1
                        self._retryable = False
                    self._changed.notify_all()
                    return self._elapsed
                delta = .5
            self._elapsed += delta
            self._since = now
        return self._elapsed

    @property
    def paused(self) -> bool:
        with self._changed:
            return self._since is None

    def elapsed(self) -> float:
        with self._changed:
            return self._advance()

    def resume(self) -> None:
        with self._changed:
            if self._since is None:
                self._since = self._now()
            self._changed.notify_all()

    def pause(self) -> None:
        with self._changed:
            if self._since is not None:
                self._advance()
                self._since = None
            self._changed.notify_all()

    def stop(self) -> None:
        with self._changed:
            self.cancel.set()
            self._changed.notify_all()

    def wait_until(self, deadline: float, on_pause: Callable[[], None] | None = None) -> float | None:
        rested = False
        while True:
            with self._changed:
                if self.cancel.is_set():
                    return None
                if self._since is not None:
                    rested = False
                    elapsed = self._advance()
                    if self._since is None:
                        continue
                    if elapsed >= deadline:
                        return elapsed
                    self._changed.wait(deadline - elapsed)
                    continue
                if on_pause is None or rested:
                    self._changed.wait()
                    continue
            # Keep slow native commands outside the condition protecting GUI controls.
            on_pause()
            rested = True


@dataclass(frozen=True)
class Sample:
    seconds: float
    meters: float
    point: Point
    speed: float = 0.0
    cadence: float = 0.0
    steps: int = 0
    motion_sent: bool = False
    activity_allowed: bool | None = None
    virtual_steps: bool = False
    heading: float | None = None


def replay(track: CampusTrack, plan: RunPlan, control: PlaybackControl, send: Callable[[Sample], None],
           *, preview: bool = False, sensor: GravityPulse | VirtualPedometer | None = None,
           location_interval: float = 1.0, start_clock: bool = True, on_pulse: Callable | None = None,
           start_seconds: float = 0.0) -> bool:
    if control.cancel.is_set():
        return False
    speedup = 20 if preview else 1
    interval = 0.12 if preview else location_interval
    path = track.travel_path(plan)
    pacing = plan.pacing
    if start_clock:
        control.resume()
    if control.cancel.is_set():
        return False
    first = pacing.at(start_seconds)
    send(Sample(start_seconds, first.meters, path.at(first.meters), first.speed, first.cadence, first.steps,
                sensor is not None, heading=path.heading_at(first.meters)))
    duration = plan.duration / speedup
    next_gps = min((floor(start_seconds / speedup / interval) + 1) * interval, duration)
    quarter = floor(4 * first.footfalls) + 1
    next_pulse = pacing.time_for_footfall(quarter/4) if sensor else inf
    deadline = min(next_gps, next_pulse)
    while True:
        elapsed = control.wait_until(deadline, sensor.rest) if sensor else control.wait_until(deadline)
        if elapsed is None:
            return False
        played = elapsed
        if abs(played-next_gps) < 1e-8:
            played = next_gps
        finished = duration - played <= 1e-8
        seconds = plan.duration if finished else played * speedup
        motion = pacing.at(seconds)
        if finished or played+1e-8 >= next_gps:
            send(Sample(seconds, motion.meters, path.at(motion.meters), motion.speed, motion.cadence,
                        motion.steps, sensor is not None, heading=path.heading_at(motion.meters)))
            if finished:
                return not control.cancel.is_set()
            next_gps = (floor((played+1e-8)/interval) + 1) * interval
        if sensor and played+1e-8 >= next_pulse:
            # Always deliver landing and unloading. Omit an intermediate sample
            # when a slower host is still processing the previous command.
            quarter = max(quarter, floor(4*motion.footfalls+1e-8))
            phase_time = pacing.time_for_footfall(quarter/4)
            pulse = pacing.at(phase_time)
            sensor.sample(pulse, essential=quarter % 2 == 0)
            if on_pulse:
                on_pulse(pulse.steps)
            quarter += 1
            next_pulse = pacing.time_for_footfall(quarter/4)
        # Skip stale deadlines rather than queueing GPS fixes or footfalls.
        deadline = min(duration, next_gps, next_pulse)


def replay_on_device(track: CampusTrack, plan: RunPlan, control: PlaybackControl,
                     device: LDPlayer, index: int, report: Callable[[Sample], None], *, use_motion: bool = True,
                     link_keep: bool = False, status: Callable[[str, str], None] = lambda *_: None,
                     checkpoint: tuple[float, int] | None = None, paused_only=False) -> bool:
    start_seconds, prior_steps = checkpoint or (0.0, 0)
    if paused_only and not checkpoint:
        raise ValueError("找不到与原计划相符的断点。")
    if checkpoint and (not link_keep or not 0 < start_seconds < plan.duration or
                       not 0 <= prior_steps < plan.pacing.at(plan.duration).steps):
        raise ValueError("断点的时间或步数无效。")
    def send(sample: Sample) -> None:
        with control.position_write() as allowed_to_send:
            if not allowed_to_send:
                return
            check_running()
            device.locate(index, sample.point)
            control.note_location(sample)
            monitor.positioned()
            if bridge:
                bridge.orient(sample.heading, immediate=sample.seconds == 0)
            report(replace(sample, motion_sent=sensor is not None, activity_allowed=allowed, virtual_steps=bridge is not None))

    with device.reserve_location(index), ExitStack() as resources:
        if not device.prepare(index, control.cancel):
            return False
        check_running = resources.enter_context(device.watch_instance(index))
        device.check_location(index)
        allowed = device.keep_activity_permission(index) if use_motion else None
        bridge = VirtualPedometer.connect(device, index) if use_motion else None
        if link_keep and use_motion and bridge is None and not paused_only and not control.cancel.is_set():
            status("waiting", "正在打开 Keep 计步窗口。打开后请进入「运动」页。")
            open_step_viewer(device, index, control.cancel)
            bridge = VirtualPedometer.connect(device, index)
        sensor = bridge or (device.motion_sensor(index, control.cancel, check_running) if use_motion and not link_keep else None)
        monitor = RunMonitor(control, bridge if link_keep else None, status, with_steps=sensor is not None,
                             check_running=check_running)

        def pulse_sent(steps):
            if bridge:
                control.note_virtual_steps(max(prior_steps, steps))
            monitor.stepped()

        def position_start():
            nonlocal start_seconds, prior_steps
            start_seconds = prior_steps = 0
            check_running()
            if not control.cancel.is_set():
                bridge.orient(track.travel_path(plan).heading_at(0), immediate=True)
            if not control.cancel.is_set():
                device.locate(index, track.start_point(plan))

        offered_point = None

        def at_planned_start():
            nonlocal start_seconds, prior_steps, offered_point
            fix = device.last_gps_fix(index)
            if fix is None:
                return False
            if offered_point is not None:
                return distance_m(fix, offered_point) <= 5
            planned = track.travel_path(plan).at(plan.pacing.at(start_seconds).meters)
            if distance_m(fix, planned) <= 5:
                offered_point = planned
                return True
            if not paused_only and distance_m(fix, track.start_point(plan)) <= 5:
                start_seconds = prior_steps = 0
                offered_point = track.start_point(plan)
                return True
            return False

        def attach_message():
            if start_seconds:
                return f"已找到暂停的运动，断点位于 {plan.distance_at(start_seconds)/1000:.3f} 公里。"
            return "Keep 已在本计划起点暂停，将从零公里开始回放。"

        try:
            if control.cancel.is_set():
                return False
            if link_keep:
                if bridge is None:
                    raise DeviceError("起停联动需要先打开 Keep 计步窗口。")
                if not monitor.wait_for_start(position_start, click_go=bridge.version >= 4, at_start=at_planned_start,
                                              attach_message=attach_message, paused_only=paused_only):
                    return False
            elif bridge:
                bridge.orient(track.travel_path(plan).heading_at(0), immediate=True)
            # LDPlayer emits a stationary fix about a second after its last command.
            # Refresh at 2 Hz so Keep's 1 Hz listener sees successive moving points.
            if bridge:
                total_steps = plan.pacing.at(plan.duration).steps
                if prior_steps:
                    bridge.begin(total_steps, prior_steps=prior_steps)
                else:
                    bridge.begin(total_steps)
                control.note_virtual_steps(prior_steps)
            # Sample the coordinate when the native channel is ready. A queued
            # coordinate arrives late when the previous ldconsole process is slow.
            # The sensor stream shares the start/pause clock and never waits for GPS.
            monitor.start(start_seconds)
            if not sensor:
                completed = replay(track, plan, control, send, location_interval=.5, start_clock=False)
            else:
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="keep-eda-motion") as worker:
                    pulses = worker.submit(replay, track, plan, control, lambda _: None, sensor=sensor,
                                           location_interval=.5, start_clock=False, on_pulse=pulse_sent,
                                           start_seconds=start_seconds)
                    pulses.add_done_callback(lambda f: control.stop() if f.exception() else None)
                    try:
                        positioned = replay(track, plan, control, send, location_interval=.5, start_clock=False,
                                            start_seconds=start_seconds)
                    except BaseException:
                        control.stop()
                        raise
                    counted = pulses.result()
                    completed = positioned and counted
                    if completed and bridge:
                        bridge.finish(total_steps)
                        control.note_virtual_steps(total_steps)
        except BaseException:
            control.stop()
            raise
        finally:
            # Close both producers before the bridge and leave Keep at its pause screen.
            monitor.close()
            if sensor:
                try:
                    sensor.close()
                except DeviceError:
                    if not control.cancel.is_set():
                        raise
        monitor.check()
        return completed
