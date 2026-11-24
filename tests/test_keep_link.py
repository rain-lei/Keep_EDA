from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from threading import Event, Thread
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import json
import time
import unittest

from keep_link import RunMonitor
from ldplayer import DeviceError
from playback import PlaybackControl, Sample, replay_on_device
from campus import CampusTrack, RunPlan
from live_history import LastReplay


def until(predicate, timeout=2):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.01)
    return False


class KeepState:
    state = "READY"

    def __init__(self):
        self.actions = []
        self.check_keep_window = Mock()

    def keep_state(self):
        return self.state

    def keep_action(self, action):
        self.actions.append(action)
        self.state = "PAUSED" if action == "PAUSE" else "RUNNING"


class LinkTests(unittest.TestCase):
    def test_last_sent_progress_survives_failure_and_restart(self):
        with TemporaryDirectory() as folder:
            path = Path(folder,"last-live-0.json")
            control = PlaybackControl()
            log = LastReplay(path,"湖区",RunPlan(3000,360,False,123))
            point = Sample(8.5,24,(121.8,39.08),steps=24)
            control.note_location(point)
            control.note_virtual_steps(22)
            log.update(control)
            first = path.read_bytes()
            data = json.loads(first)
            self.assertEqual((data["location_m"],data["steps_sent"]),(24,22))
            # Cancelled waiting does not erase the preceding run.
            log.update(PlaybackControl(),"等待取消")
            self.assertEqual(path.read_bytes(),first)
            with patch("live_history.replace",side_effect=OSError("disk busy")):
                log.update(control,"中断")
            self.assertEqual(path.read_bytes(),first)
            self.assertEqual(list(path.parent.iterdir()),[path])
            self.assertIn("disk busy",log.error)
            log.update(control,"已停止")
            self.assertEqual(json.loads(path.read_bytes())["state"],"已停止")

    def test_wait_start_both_pause_directions_and_finish(self):
        control, bridge = PlaybackControl(), KeepState()
        notices = []
        monitor = RunMonitor(control,bridge,lambda *args:notices.append(args))
        started = []
        wait = Thread(target=lambda:started.append(monitor.wait_for_start()))
        wait.start()
        try:
            self.assertTrue(until(lambda:any(state == "armed" for state, _ in notices)))
            self.assertEqual(control.elapsed(),0)
            self.assertFalse(started)
            bridge.state = "RUNNING"
            wait.join(2)
            self.assertEqual(started,[True])
            control.resume()
            monitor.start()
            # The Keep pause button controls both replay producers.
            bridge.state = "PAUSED"
            self.assertTrue(until(lambda:control.paused))
            at = control.elapsed()
            time.sleep(.1)
            self.assertEqual(control.elapsed(),at)
            # The desktop resume button waits for Keep to confirm it.
            control.request("RESUME")
            self.assertTrue(until(lambda:not control.paused))
            self.assertIn("RESUME",bridge.actions)
            control.request("PAUSE")
            self.assertTrue(until(lambda:bridge.state == "PAUSED" and control.pending_action is None))
            self.assertTrue(control.paused)
            bridge.state = "RUNNING"
            self.assertTrue(until(lambda:not control.paused))
        finally:
            control.stop()
            wait.join(2)
            monitor.close()
        self.assertEqual(bridge.state,"PAUSED")
        self.assertEqual(notices[-1][0],"ended")

    def test_stalled_gps_stops_shared_clock_and_pulses(self):
        for channel,message in (("last_gps", "定位通道"), ("last_step", "计步通道")):
            with self.subTest(channel=channel):
                control = PlaybackControl()
                monitor = RunMonitor(control, with_steps=True)
                control.resume()
                monitor.start()
                setattr(monitor, channel, time.monotonic()-4)
                try:
                    self.assertTrue(control.cancel.wait(1))
                    self.assertIsNone(control.wait_until(100))
                    with self.assertRaisesRegex(DeviceError,message):
                        monitor.check()
                finally:
                    monitor.close()

    def test_cancel_waiting_does_not_pause_an_unstarted_keep(self):
        control, bridge = PlaybackControl(), KeepState()
        control.stop()
        monitor = RunMonitor(control,bridge)
        self.assertFalse(monitor.wait_for_start())
        monitor.close()
        self.assertEqual(bridge.actions,[])

    def test_wrong_window_while_waiting_exits_without_touching_keep(self):
        for state in ("UNKNOWN", "RUNNING"):
            with self.subTest(state=state):
                bridge = KeepState()
                bridge.state = state
                bridge.check_keep_window.side_effect = DeviceError("户外跑步页在雷电普通窗口")
                monitor = RunMonitor(PlaybackControl(), bridge)
                with self.assertRaisesRegex(DeviceError, "普通窗口"):
                    monitor.wait_for_start()
                monitor.close()
                self.assertFalse(monitor.started)
                self.assertEqual(bridge.actions, [])

    def test_existing_workout_never_relocated_or_claimed(self):
        for state in ("RUNNING", "PAUSED"):
            with self.subTest(state=state):
                device = Mock()
                device.reserve_location.return_value = nullcontext()
                device.watch_instance.return_value = nullcontext(lambda: None)
                bridge = Mock()
                bridge.keep_state.side_effect = ["UNKNOWN", state]
                with patch("playback.VirtualPedometer.connect", return_value=bridge):
                    with self.assertRaisesRegex(DeviceError, "已有"):
                        replay_on_device(CampusTrack(), RunPlan(10, 120, False, 1), PlaybackControl(),
                                         device, 0, Mock(), link_keep=True)
                device.locate.assert_not_called()
                bridge.orient.assert_not_called()
                bridge.begin.assert_not_called()
                bridge.keep_action.assert_not_called()
                bridge.close.assert_called_once()

    def test_ready_positions_once_and_tolerates_countdown(self):
        bridge = Mock()
        bridge.keep_state.side_effect = ["UNKNOWN", "READY", "READY", "UNKNOWN", "RUNNING"]
        control = PlaybackControl()
        monitor = RunMonitor(control, bridge)
        with patch.object(control.cancel, "wait"):
            position = Mock()
            self.assertTrue(monitor.wait_for_start(position))
        position.assert_called_once()
        self.assertTrue(monitor.started)
        self.assertEqual(control.elapsed(), 0)

    def test_unrecognised_screen_releases_wait_before_or_after_arming(self):
        for armed in (False, True):
            with self.subTest(armed=armed):
                bridge = Mock()
                bridge.keep_state.side_effect = (["READY"] if armed else []) + ["UNKNOWN"] * 6
                control = PlaybackControl()
                monitor = RunMonitor(control, bridge)
                now = [100]
                def tick(_):
                    now[0] += 16
                with patch("keep_link.time.monotonic", side_effect=lambda: now[0]), \
                     patch.object(control.cancel, "wait", side_effect=tick):
                    with self.assertRaisesRegex(DeviceError, "已取消等待"):
                        monitor.wait_for_start()
                self.assertFalse(monitor.started)
                bridge.keep_action.assert_not_called()

    def test_quick_clicks_are_serialized_until_keep_confirms(self):
        control, bridge = PlaybackControl(), KeepState()
        bridge.state = "RUNNING"
        bridge.keep_action = Mock()  # Android acknowledges receipt before the UI changes.
        monitor = RunMonitor(control, bridge)
        control.start_live_clock(linked=True)
        for action, target in (("PAUSE", "PAUSED"), ("RESUME", "RUNNING")):
            opposite = "RESUME" if action == "PAUSE" else "PAUSE"
            self.assertTrue(control.request(action))
            self.assertFalse(control.request(opposite))
            monitor._sync_keep()
            monitor._sync_keep()
            self.assertFalse(control.request(opposite))
            self.assertTrue(control.paused)
            self.assertEqual(control.pending_action, action)
            bridge.keep_action.assert_called_once_with(action)
            bridge.state = target
            monitor._sync_keep()
            self.assertIsNone(control.pending_action)
            self.assertEqual(control.paused, target == "PAUSED")
            bridge.keep_action.reset_mock()

    def test_hidden_page_freezes_clock_and_next_go_ends_only_our_replay(self):
        now = [20]
        control, bridge = PlaybackControl(lambda: now[0]), KeepState()
        monitor = RunMonitor(control, bridge)
        monitor.started = True
        control.start_live_clock(linked=True)
        now[0] += 1
        bridge.state = "UNKNOWN"
        monitor._sync_keep()
        self.assertTrue(control.paused)
        now[0] += 2
        self.assertEqual(control.elapsed(), 1)
        bridge.state = "RUNNING"
        monitor._sync_keep()
        self.assertFalse(control.paused)
        bridge.state = "READY"
        with self.assertRaisesRegex(DeviceError, "返回 GO"):
            monitor._sync_keep()
        bridge.state = "RUNNING"  # A different workout may have opened during cleanup.
        monitor.close()
        self.assertEqual(bridge.actions, [])

    def test_unconfirmed_resume_times_out_without_advancing_the_clock(self):
        now = [100.]
        control, bridge = PlaybackControl(lambda: now[0]), Mock()
        bridge.keep_state.return_value = "PAUSED"
        monitor = RunMonitor(control, bridge)
        control.request("RESUME")
        with patch("keep_link.time.monotonic", side_effect=lambda: now[0]):
            monitor._sync_keep()
            now[0] += 4.1
            with self.assertRaisesRegex(DeviceError, "未确认"):
                monitor._sync_keep()
        self.assertTrue(control.paused)
        self.assertEqual(control.elapsed(), 0)
        bridge.keep_action.assert_called_once_with("RESUME")

    def test_sleep_holds_both_producers_until_confirmed_resume(self):
        now = [20]
        control, bridge = PlaybackControl(lambda: now[0]), KeepState()
        monitor = RunMonitor(control, bridge)
        bridge.state = "RUNNING"
        control.start_live_clock(linked=True)
        now[0] += 1
        self.assertEqual(control.elapsed(), 1)
        now[0] += 120
        self.assertEqual(control.elapsed(), 1)
        self.assertTrue(control.paused)
        monitor._sync_keep()  # A stale RUNNING reply cannot restart the clock.
        self.assertTrue(control.paused)
        self.assertEqual(bridge.actions, ["PAUSE"])
        monitor._sync_keep()
        self.assertTrue(control.paused)
        control.request("RESUME")
        monitor._sync_keep()
        monitor._sync_keep()
        now[0] += .5
        self.assertEqual(control.elapsed(), 1.5)

    def test_disconnect_while_waiting_or_paused_releases_instance(self):
        for waiting in (True, False):
            with self.subTest(waiting=waiting):
                device, bridge = Mock(), KeepState()
                ready, offline, released = Event(), Event(), Event()
                def check():
                    if offline.is_set():
                        raise DeviceError("实例已离线")
                @contextmanager
                def reserve(_):
                    try:
                        yield
                    finally:
                        released.set()
                device.reserve_location.side_effect = reserve
                device.watch_instance.return_value = nullcontext(check)
                control = PlaybackControl()
                def report(_):
                    control.pause()
                    ready.set()
                def status(state, _message):
                    if state == "armed":
                        ready.set()
                with patch("playback.VirtualPedometer.connect", return_value=bridge), ThreadPoolExecutor(1) as pool:
                    # In linked mode this test cancels before GO and has no sensor samples.
                    bridge.orient, bridge.close = Mock(), Mock()
                    future = pool.submit(replay_on_device, CampusTrack(), RunPlan(100, 360, False, 1),
                                         control, device, 0, report, use_motion=waiting, link_keep=waiting, status=status)
                    try:
                        self.assertTrue(ready.wait(2))
                        offline.set()
                        with self.assertRaisesRegex(DeviceError, "离线"):
                            future.result(2)
                    finally:
                        control.stop()
                self.assertTrue(released.is_set())
                self.assertEqual(device.locate.call_count, 1)
                self.assertEqual(bridge.actions, [])

    def test_stop_with_resume_in_flight_checks_a_fresh_pause(self):
        control, bridge = PlaybackControl(), Mock()
        statuses = []
        monitor = RunMonitor(control, bridge, lambda state, _: statuses.append(state))
        monitor.started = True
        control.request("RESUME")
        state = ["PAUSED"]
        now = [100.]
        bridge.keep_state.side_effect = lambda: state[0]
        def tick(seconds):
            now[0] += seconds
            if now[0] < 100.6:
                state[0] = "RUNNING"
            else:
                state[0] = "PAUSED"
        with patch("keep_link.time.monotonic", side_effect=lambda: now[0]), patch("keep_link.time.sleep", side_effect=tick):
            monitor._sync_keep()
            control.stop()
            monitor.close()
        self.assertEqual([c.args[0] for c in bridge.keep_action.call_args_list], ["RESUME", "PAUSE", "PAUSE"])
        self.assertGreaterEqual(now[0], 101.8)
        self.assertEqual(statuses[-1], "ended")


if __name__ == "__main__":
    unittest.main()
