"""Interruptible, wall-clock-paced replay on the same geometry used for previews."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, replace
from math import floor, inf
from threading import Condition, Event
import time

from campus import CampusTrack, Point, RunPlan
from ldplayer import DeviceError, GravityPulse, LDPlayer
from keep_link import RunMonitor
from sensor_bridge import VirtualPedometer


class PlaybackControl:
    def __init__(self, now: Callable[[], float] = time.monotonic):
        self.cancel = Event()
        self._changed = Condition()
        self._now = now
        self._elapsed = 0.0
        self._since: float | None = None
        self.suspensions = 0
        self._action = None
        self._last_location = None
        self._last_steps = None

    def note_location(self, sample):
        with self._changed:
            self._last_location = sample

    def note_virtual_steps(self, count: int):
        with self._changed:
            self._last_steps = count

    def last_sent(self):
        with self._changed:
            return self._last_location, self._last_steps

    def request(self, action: str):
        with self._changed:
            if action == "PAUSE":
                self.pause()
            self._action = action
            self._changed.notify_all()

    def pop_action(self):
        with self._changed:
            action, self._action = self._action, None
            return action

    def _advance(self) -> float:
        """One authoritative timeline, even when GPS and sensors wake at different times."""
        if self._since is not None:
            now = self._now()
            delta = max(0.0, now-self._since)
            if delta > 5:
                delta = .5
                self.suspensions += 1
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
           location_interval: float = 1.0, start_clock: bool = True, on_pulse: Callable | None = None) -> bool:
    if control.cancel.is_set():
        return False
    speedup = 20 if preview else 1
    interval = 0.12 if preview else location_interval
    path = track.travel_path(plan)
    pacing = plan.pacing
    if start_clock:
        control.resume()
    first = pacing.at(0)
    send(Sample(0.0, 0.0, path.points[0], first.speed, first.cadence, 0, sensor is not None,
                heading=path.heading_at(0)))
    duration = plan.duration / speedup
    next_gps = min(interval, duration)
    quarter = 1
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
                     link_keep: bool = False, status: Callable[[str, str], None] = lambda *_: None) -> bool:
    next_check = 0.0

    def send(sample: Sample) -> None:
        nonlocal next_check
        if sample.seconds >= next_check or sample.meters == plan.meters:
            check_running()
            next_check = sample.seconds + 5
        device.locate(index, sample.point)
        if bridge:
            bridge.orient(sample.heading, immediate=sample.seconds == 0)
        control.note_location(sample)
        monitor.positioned()
        report(replace(sample, motion_sent=sensor is not None, activity_allowed=allowed, virtual_steps=bridge is not None))

    with device.reserve_location(index), ExitStack() as resources:
        if not device.prepare(index, control.cancel):
            return False
        check_running = resources.enter_context(device.watch_instance(index))
        device.check_location(index)
        allowed = device.keep_activity_permission(index) if use_motion else None
        bridge = VirtualPedometer.connect(device, index) if use_motion else None
        sensor = bridge or (device.motion_sensor(index, control.cancel) if use_motion and not link_keep else None)
        monitor = RunMonitor(control, bridge if link_keep else None, status, with_steps=sensor is not None)

        def pulse_sent(steps):
            if bridge:
                control.note_virtual_steps(steps)
            monitor.stepped()
        try:
            if bridge:
                bridge.orient(track.travel_path(plan).heading_at(0), immediate=True)
            if link_keep:
                if bridge is None:
                    raise DeviceError("起停联动需要先打开 Keep 计步窗口。")
                device.locate(index, track.start_point(plan))
                if not monitor.wait_for_start():
                    return False
            # LDPlayer emits a stationary fix about a second after its last command.
            # Refresh at 2 Hz so Keep's 1 Hz listener sees successive moving points.
            if not sensor:
                control.resume()
                monitor.start()
                completed = replay(track, plan, control, send, location_interval=.5, start_clock=False)
                monitor.check()
                return completed
            if bridge:
                total_steps = plan.pacing.at(plan.duration).steps
                bridge.begin(total_steps)
                control.note_virtual_steps(0)
            # Sample the coordinate when the native channel is ready. A queued
            # coordinate arrives late when the previous ldconsole process is slow.
            # The sensor stream shares the start/pause clock and never waits for GPS.
            control.resume()
            monitor.start()
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="keep-eda-motion") as worker:
                pulses = worker.submit(replay, track, plan, control, lambda _: None, sensor=sensor,
                                       location_interval=.5, start_clock=False, on_pulse=pulse_sent)
                pulses.add_done_callback(lambda f: control.stop() if f.exception() else None)
                try:
                    positioned = replay(track, plan, control, send, location_interval=.5, start_clock=False)
                except BaseException:
                    control.stop()
                    raise
                counted = pulses.result()
                completed = positioned and counted
                monitor.check()
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
        return completed
