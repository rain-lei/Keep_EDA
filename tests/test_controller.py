from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from itertools import pairwise
import json
from math import floor, hypot, inf
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from campus import CampusTrack, RunPlan, distance_m
from ldplayer import DeviceError, GravityPulse, Instance, KeepWindows, LDPlayer, parse_list
from playback import PlaybackControl, replay, replay_on_device
from fit_export import export_fit
from motion import Motion
from sensor_bridge import VirtualPedometer, open_step_viewer


class VirtualClock(PlaybackControl):
    """Advance directly to each requested deadline."""

    def wait_until(self, deadline, on_pause=None):
        return None if self.cancel.is_set() else deadline


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.track = CampusTrack()

    def test_course_and_closure_in_both_directions(self):
        t = self.track
        self.assertAlmostEqual(t.length, 421.526, places=2)
        self.assertEqual(t.position(t.length * 2), t.points[0])
        self.assertLess(distance_m(t.position(25, True), t.position(t.length - 25)), 0.001)
        self.assertLess(t.position(10)[1], t.points[0][1])  # CCW heads south on the west straight.

    def test_mapped_lake_route_and_seeded_laps(self):
        lake = CampusTrack("lake")
        self.assertAlmostEqual(lake.length, 742.614, places=2)
        self.assertEqual(lake.points[0], lake.points[-1])
        for t in (lake, self.track):
            for clockwise in (False, True):
                plan = RunPlan.parse("3", "6:00", clockwise, seed=5432)
                with self.subTest(route=t.name, clockwise=clockwise):
                    path = t.travel_path(plan)
                    self.assertEqual(path.points[0], t.start_point(plan))
                    self.assertAlmostEqual(path.length, 3000, places=3)
                    expected = t.at((plan.start_fraction*t.length + (-10 if clockwise else 10)) % t.length)
                    self.assertLess(distance_m(path.at(10), expected), 2)
                    self.assertNotEqual(path.at(100), path.at(100 + t.length))
                    self.assertEqual(path.points, CampusTrack(t.route_id).travel_path(plan).points)
                    other = RunPlan(3000, 360, clockwise, 5433)
                    self.assertNotEqual(path.at(100), t.travel_path(other).at(100))
                    samples = []
                    replay(t, plan, VirtualClock(), samples.append)
                    measured = sum(distance_m(a.point, b.point) for a, b in pairwise(samples))
                    self.assertLess(abs(measured - 3000), 20)

    def test_start_is_uniform_along_the_route_and_can_be_locked(self):
        for track in (self.track, CampusTrack("lake")):
            starts = set()
            for seed in range(32):
                plan = RunPlan(100, 360, False, seed)
                start = track.start_point(plan)
                self.assertEqual(start, track.travel_path(plan).points[0])
                self.assertEqual(start, track.start_point(RunPlan(500, 375, True, seed)))
                starts.add(int(plan.start_fraction*8))
            self.assertGreaterEqual(len(starts), 7)
            for seed in (1, 87):
                for clockwise in (True, False):
                    fixed = RunPlan(1000, 375, clockwise, seed, random_start=False)
                    self.assertEqual(track.start_point(fixed), track.points[0])
                    self.assertEqual(track.travel_path(fixed).points[0], track.points[0])

    def test_pace_distance_and_step_integrals_agree(self):
        plan = RunPlan(3000, 360, False, 5432)
        pace = plan.pacing
        self.assertEqual(plan.duration, 1080)
        laps = [b-a for a, b in pairwise([pace.time_at(m) for m in (0, 1000, 2000, 3000)])]
        self.assertAlmostEqual(sum(laps), 1080)
        self.assertLess(max(laps)-min(laps), 13)
        self.assertGreater(max(laps)-min(laps), 1)
        self.assertGreater(max(pace.cadences)-min(pace.cadences), 2)
        self.assertAlmostEqual(pace.at(plan.duration).meters, 3000)
        for seconds in (0, 0.3, pace.interval, 104.17, 540, 1079.7, 1080):
            motion = pace.at(seconds)
            self.assertAlmostEqual(pace.time_at(motion.meters), seconds, places=6)
            self.assertAlmostEqual(pace.time_for_footfall(motion.footfalls), seconds, places=6)
            self.assertEqual(motion.steps, round(motion.footfalls))
        self.assertEqual(pace.time_for_footfall(pace.footfalls[-1]+1), inf)

    def test_smooth_pace_and_consistent_stride_over_different_runs(self):
        for seed in range(24):
            curve = RunPlan(3000, 360, False, seed).pacing
            samples = [curve.at(t) for t in range(1081)]
            pace = [1000/s.speed for s in samples]
            with self.subTest(seed=seed):
                self.assertTrue(all(344 < value < 378 for value in pace))
                self.assertGreater(max(pace)-min(pace), 4)
                self.assertLess(max(abs(a-b) for a,b in pairwise(pace)), .7)
                self.assertTrue(all(159 < s.cadence < 178 and .90 < s.step_length < 1.1 for s in samples))
                self.assertLess(max(abs(a.cadence-b.cadence) for a,b in pairwise(samples)), .15)
                self.assertAlmostEqual(sum((a.speed+b.speed)/2 for a,b in pairwise(samples)), 3000, places=2)
                self.assertAlmostEqual(curve.footfalls[-1], sum((a.cadence+b.cadence)/120 for a,b in pairwise(samples)), places=3)

    def test_input_validation(self):
        for km, pace in (("NaN", "6:00"), ("Infinity", "6:00"), ("-2", "6:00"),
                         ("0.0001", "6:00"), ("42.196", "6:00"), ("1", "2:60"),
                         ("1", "20:01"), ("1", "1:59"), ("", "")):
            with self.subTest(km=km, pace=pace), self.assertRaises(ValueError):
                RunPlan.parse(km, pace, False)
        self.assertEqual(RunPlan.parse("2.50", "6：15", True).duration, 937.5)

    def test_exact_fractional_end_after_lap(self):
        plan = RunPlan.parse("0.453", "6:15", True)
        points = []
        self.assertTrue(replay(self.track, plan, VirtualClock(), points.append))
        self.assertEqual((points[0].meters, points[0].point), (0, self.track.start_point(plan)))
        self.assertEqual(points[-1].seconds, 169.875)
        self.assertEqual(points[-1].point, self.track.travel_path(plan).at(453))
        self.assertEqual(points[-1].meters, 453)
        self.assertTrue(all(a.seconds < b.seconds and a.meters < b.meters for a, b in zip(points, points[1:])))

    def test_resume_excludes_pause_time(self):
        now = [100.0]
        c = PlaybackControl(lambda: now[0])
        c.resume()
        now[0] += 3
        c.pause()
        now[0] += 40
        c.resume()
        now[0] += 0.75
        self.assertEqual(c.wait_until(0), 3.75)

    def test_sleep_does_not_jump_to_the_finish(self):
        now = [100.0]
        control = PlaybackControl(lambda: now[0])
        control.resume()
        now[0] += .5
        self.assertEqual(control.wait_until(0), .5)
        now[0] += 120
        # Both producers read the same compensated time, in either order.
        self.assertEqual(control.wait_until(0), 1)
        self.assertEqual(control.wait_until(0), 1)
        now[0] += .5
        self.assertEqual(control.wait_until(0), 1.5)
        self.assertEqual(control.suspensions, 1)

    def test_stop_wakes_paused_replay(self):
        c = PlaybackControl()
        got = []
        thread = threading.Thread(target=lambda: got.append(c.wait_until(100)), daemon=True)
        thread.start()
        c.stop()
        thread.join(1)
        self.assertEqual(got, [None])

    def test_stationary_at_first_and_last_point_of_preview(self):
        plan = RunPlan.parse("0.010", "2:00", False)
        points = []
        self.assertTrue(replay(self.track, plan, VirtualClock(), points.append, preview=True))
        self.assertEqual([p.meters for p in points], [0, 10])

    def test_sensor_samples_follow_integrated_footfalls_on_the_gps_clock(self):
        plan = RunPlan(100, 300, False, 42)
        events, samples = [], []
        sensor = Mock()
        sensor.sample.side_effect = lambda m, *, essential: events.append((m.footfalls, essential))
        self.assertTrue(replay(self.track, plan, VirtualClock(), samples.append, sensor=sensor))
        self.assertEqual(len(events), floor(4*plan.pacing.footfalls[-1]))
        for quarter, (footfalls, essential) in enumerate(events, 1):
            self.assertAlmostEqual(footfalls, quarter/4, places=7)
            self.assertEqual(essential, quarter % 2 == 0)
        self.assertEqual(sum(i % 4 == 2 for i in range(1, len(events)+1)), samples[-1].steps)
        self.assertTrue(all(s.motion_sent for s in samples))
        self.assertEqual([s.seconds for s in samples], list(range(31)))

    def test_pause_rests_accelerometer_and_stop_wakes_replay(self):
        control = PlaybackControl()
        sensor = Mock()
        pulsed, rested = threading.Event(), threading.Event()
        sensor.sample.side_effect = lambda _, **kw: pulsed.set()
        sensor.rest.side_effect = rested.set
        plan = RunPlan(100, 300, False, 42)
        result = []
        worker = threading.Thread(target=lambda: result.append(
            replay(self.track, plan, control, lambda _: None, sensor=sensor)), daemon=True)
        worker.start()
        try:
            self.assertTrue(pulsed.wait(2))
            control.pause()
            self.assertTrue(rested.wait(1))
        finally:
            control.stop()
            worker.join(2)
        self.assertEqual(result, [False])


class DeviceTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows command output and location reservation")
    def test_daemon_inheriting_output_does_not_hold_location(self):
        # ADB may start a server that outlives ldconsole with both output handles open.
        device = LDPlayer(Path(sys.executable))
        index = 73
        launcher = (
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', sys.argv[1]], "
            "stdin=subprocess.DEVNULL, stdout=sys.stdout, stderr=sys.stderr)\n"
            "print('34', flush=True)\n"
            "print('starting server', file=sys.stderr, flush=True)\n"
            "time.sleep(float(sys.argv[2]))"
        )
        with socket.socket() as listener, ThreadPoolExecutor(max_workers=1) as worker:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(6)
            helper = (f"import socket; s=socket.create_connection({listener.getsockname()!r}, 10); "
                      "s.recv(1)")

            def query(delay):
                with device.reserve_location(index):
                    return device.command("-c", launcher, helper, str(delay), timeout=2)

            for delay in (0, 15):
                with self.subTest(command_times_out=bool(delay)):
                    future = worker.submit(query, delay)
                    connection, _ = listener.accept()
                    with connection:  # Keep the descendant alive through the retry.
                        if delay:
                            with self.assertRaisesRegex(DeviceError, "超时"):
                                future.result(timeout=5)
                        else:
                            self.assertEqual(future.result(timeout=5), "34")
                        with device.reserve_location(index):
                            pass

    @unittest.skipUnless(os.name == "nt", "Windows process lifetime monitor")
    def test_vm_exit_wakes_native_check_without_another_console_query(self):
        device = LDPlayer(Path("ldconsole.exe"))
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            with patch.object(device, "instance", return_value=Instance(2, "test", True, child.pid)) as lookup:
                with device.watch_instance(2) as check:
                    check()
                    child.terminate()
                    child.wait(timeout=5)
                    with self.assertRaisesRegex(DeviceError, "实例 2 已离线"):
                        check()
                lookup.assert_called_once_with(2)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)

    def test_list2_chinese_numeric_title_and_legacy_stopped_instance(self):
        devices = parse_list("0,雷电,1,2,526896,2689582,1,47400,6972,1080,1920,480\r\n3,测试,0,0,0,-1,-1\n")
        self.assertEqual((devices[0].name, devices[0].booted, devices[0].pid), ("雷电,1,2", True, 47400))
        self.assertEqual(devices[0].vm_pid, 6972)
        self.assertEqual((devices[1].index, devices[1].booted), (3, False))
        starting = parse_list("0,雷电,920318,790152,2,9252,-1,1080,1920,480")[0]
        self.assertFalse(starting.booted)
        self.assertEqual(starting.pid, 9252)
        with self.assertRaises(DeviceError):
            parse_list("not a list2 response")

    @patch("playback.VirtualPedometer.connect", return_value=None)
    def test_no_gps_after_disconnect(self, connect):
        device = LDPlayer(Path("ldconsole.exe"))
        device.prepare = Mock(return_value=True)
        device.check_location = Mock()
        device.keep_activity_permission = Mock(return_value=False)
        sensor = Mock()
        device.motion_sensor = Mock(return_value=sensor)
        device.locate = Mock()
        device.require_running = Mock(side_effect=[None, DeviceError("offline")])
        device.watch_instance = Mock(return_value=nullcontext(lambda: device.require_running(7)))
        points = []
        plan = RunPlan.parse("0.05", "5:00", False)
        with self.assertRaisesRegex(DeviceError, "offline"):
            replay_on_device(CampusTrack(), plan, VirtualClock(), device, 7, points.append)
        self.assertEqual([sample.seconds for sample in points], [i/2 for i in range(10)])
        self.assertTrue(all(call.args[0] == 7 for call in device.locate.call_args_list))
        self.assertTrue(all(s.activity_allowed is False for s in points))
        sensor.close.assert_called_once()

    @patch("playback.VirtualPedometer.connect", return_value=None)
    def test_no_steps_interface_falls_back_to_gps(self, connect):
        device = Mock(spec=LDPlayer)
        device.reserve_location.return_value = nullcontext()
        device.watch_instance.return_value = nullcontext(lambda: None)
        device.prepare.return_value = True
        device.motion_sensor.return_value = None
        plan = RunPlan(10, 120, False, 17)
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                device.reset_mock()
                samples = []
                self.assertTrue(replay_on_device(CampusTrack("lake"), plan, VirtualClock(),
                                                 device, 0, samples.append, use_motion=enabled))
                self.assertEqual(device.motion_sensor.call_count, int(enabled))
                self.assertEqual(device.locate.call_count, len(samples))
                self.assertTrue(all(not s.motion_sent for s in samples))

    @patch("playback.VirtualPedometer.connect")
    def test_android_window_receives_exact_steps_and_stops_at_interruption(self, connect):
        device = Mock(spec=LDPlayer)
        device.reserve_location.return_value = nullcontext()
        device.watch_instance.return_value = nullcontext(lambda: None)
        device.prepare.return_value = True
        sensor = connect.return_value
        plan = RunPlan(20, 360, False, 321)
        total = plan.pacing.at(plan.duration).steps
        samples = []
        self.assertTrue(replay_on_device(CampusTrack(), plan, VirtualClock(), device, 0, samples.append))
        self.assertTrue(all(s.virtual_steps and s.motion_sent for s in samples))
        sensor.begin.assert_called_once_with(total)
        sensor.finish.assert_called_once_with(total)
        sensor.close.assert_called_once()
        device.motion_sensor.assert_not_called()
        sensor.reset_mock()
        control = VirtualClock()
        sensor.sample.side_effect = lambda *_a, **_kw: control.stop()
        self.assertFalse(replay_on_device(CampusTrack(), plan, control, device, 0, samples.append))
        sensor.finish.assert_not_called()
        sensor.close.assert_called_once()

    def test_gait_alternates_side_and_restores_after_the_last_write(self):
        device = Mock(spec=LDPlayer)
        neutral = (0.05, 9.7, 1.5)
        sensor = GravityPulse(device, 3, neutral)
        sensor.rest()
        device.gravity.assert_not_called()
        try:
            for footfall in (3.5, 4.0, 4.5):
                sensor.sample(Motion(5, 2.8, 168, footfall))
                sensor.pending[-1].result(timeout=2)
            sensor.rest()
        finally:
            sensor.close()
        self.assertEqual(device.gravity.call_count, 4)
        first, trough, second, last = [call.args[1] for call in device.gravity.call_args_list]
        self.assertGreater(hypot(*first), 15)
        self.assertLess(hypot(*trough), 8)
        self.assertAlmostEqual(hypot(*first), hypot(*second))
        self.assertGreater(hypot(*(a-b for a,b in zip(first,second))), 3.0)
        self.assertEqual(last, neutral)

    def test_slow_sensor_writer_drops_obsolete_samples_and_drains_before_rest(self):
        device = Mock(spec=LDPlayer)
        gate = threading.Event()
        device.gravity.side_effect = lambda *_: gate.wait(2)
        neutral = (0, 9.81, 0)
        sensor = GravityPulse(device, 0, neutral)
        try:
            sensor.sample(Motion(0, 2.7, 168, .5))
            for i in range(1, 40):
                sensor.sample(Motion(0, 2.7, 168, i/8), essential=False)
            sensor.sample(Motion(0, 2.7, 168, 1))
            self.assertEqual(len(sensor.pending), 2)
        finally:
            gate.set()
            sensor.close()
        self.assertEqual(device.gravity.call_count, 3)
        self.assertEqual(device.gravity.call_args.args, (0, neutral))

    def test_keep_permission_uses_grant_and_app_op(self):
        device = LDPlayer(Path("ldconsole.exe"))
        key = "android.permission.ACTIVITY_RECOGNITION: granted="
        for answers, expected in (([key+"false"], False), ([key+"true","ACTIVITY_RECOGNITION: ignore"], False),
                                  ([key+"true","ACTIVITY_RECOGNITION: allow"], True), ([""], None)):
            device.command = Mock(side_effect=answers)
            self.assertIs(device.keep_activity_permission(0), expected)

    def test_same_device_reserved_across_controllers(self):
        import os
        if os.name != "nt":
            self.skipTest("Windows named mutex")
        device = LDPlayer(Path("ldconsole.exe"))
        with device.reserve_location(0):
            with self.assertRaises(DeviceError), device.reserve_location(0):
                pass
            with device.reserve_location(1):
                pass
        with device.reserve_location(0):
            pass

    def test_location_argument_order(self):
        device = LDPlayer(Path("ldconsole.exe"))
        device.command = Mock()
        device.locate(2, (121.8076369, 39.085169))
        device.command.assert_called_once_with("locate", "--index", "2", "--LLI", "121.8076369,39.0851690")


class BridgeTests(unittest.TestCase):
    def test_android_protocol_counts_once_and_releases_the_selected_forward(self):
        device = Mock(spec=LDPlayer)
        device.command.return_value = "31041"
        outbound, inbound = socket.socketpair()
        received = []

        def serve():
            with inbound, inbound.makefile("rb") as reader:
                inbound.sendall(b"EDA1 8\n")
                for raw in reader:
                    line = raw.decode().strip()
                    received.append(line)
                    if not line.startswith("S "):
                        inbound.sendall(b"OK\n")

        listener = threading.Thread(target=serve, daemon=True)
        listener.start()
        with patch("sensor_bridge.socket.create_connection", return_value=outbound):
            bridge = VirtualPedometer.connect(device, 7)
        try:
            self.assertEqual(bridge.display_id, 8)
            bridge.begin(10)
            bridge.sample(Motion(1, 2.8, 168, 1.5))
            bridge.rest()
            bridge.rest()
            bridge.sample(Motion(9, 2.8, 168, 9.6))
            bridge.finish(10)
        finally:
            bridge.close()
        listener.join(1)
        self.assertEqual([line.split()[0] for line in received], ["BEGIN","S","REST","S","END"])
        self.assertEqual(received[-1], "END 10")
        device.command.assert_called_with("adb", "--index", "7", "--command", "forward --remove tcp:31041")

    @patch("sensor_bridge.VirtualPedometer.connect")
    def test_reopen_preserves_active_keep_or_restores_only_its_empty_display(self, connect):
        package = "com.gotokeep.keep"
        connect.return_value.display_id = 4
        for active in (True, False):
            with self.subTest(active=active):
                device = LDPlayer(Path("ldconsole.exe"))
                restored = active
                replies = {"shell getprop ro.build.version.sdk": "34", f"shell pm path {package}": "package:/data/app/base.apk"}
                def android(*args, **kwargs):
                    nonlocal restored
                    command = args[-1]
                    if command == "shell dumpsys activity activities":
                        record = "    topResumedActivity=ActivityRecord{123 u0 com.gotokeep.keep/.Run}\n" if restored else ""
                        return "Display #4 (activities from top to bottom):\n" + record + "Display #0 (activities from top to bottom):\n"
                    if command.startswith("shell am start --display 4 "):
                        restored = True
                    if "resolve-activity" in command:
                        return package + "/.Main"
                    return replies.get(command,"")
                device.command = Mock(side_effect=android)
                open_step_viewer(device, 0, threading.Event())
                launches = [call.args[-1] for call in device.command.call_args_list if "--display 4 -n" in call.args[-1]]
                self.assertEqual(launches, [] if active else ["shell am start --display 4 -n com.gotokeep.keep/.Main"])

    def test_regular_workout_and_global_summary_cannot_start_linked_replay(self):
        dump = ("Display #0 (activities from top to bottom):\n"
                "    topResumedActivity=ActivityRecord{123 u0 com.gotokeep.keep/.training.VpOutdoorTrainingActivity t7}\n"
                "Display #2 (activities from top to bottom):\n"
                "  ResumedActivity: ActivityRecord{123 u0 com.gotokeep.keep/.training.VpOutdoorTrainingActivity t7}\n"
                "ActivityTaskSupervisor state:\n"
                "    * Hist  #1: ActivityRecord{123 u0 com.gotokeep.keep/.training.VpOutdoorTrainingActivity t7}")
        windows = KeepWindows.from_activities(dump)
        self.assertEqual(windows.has_keep, {0:True, 2:False})
        device = Mock(spec=LDPlayer)
        device.keep_windows.return_value = windows
        device.reserve_location.return_value = nullcontext()
        device.watch_instance.return_value = nullcontext(lambda: None)
        bridge = Mock(spec=VirtualPedometer)
        bridge.check_keep_window.side_effect = lambda: windows.require_viewer(2)
        with patch("playback.VirtualPedometer.connect", return_value=bridge):
            with self.assertRaisesRegex(DeviceError, "户外跑步页在雷电普通窗口"):
                replay_on_device(CampusTrack(), RunPlan(10, 120, False, 17), PlaybackControl(),
                                 device, 0, lambda _: None, link_keep=True)
        device.locate.assert_not_called()
        bridge.orient.assert_not_called()
        bridge.begin.assert_not_called()
        bridge.keep_action.assert_not_called()
        bridge.close.assert_called_once()
        with self.assertRaisesRegex(DeviceError, "无法确认"):
            KeepWindows.from_activities("no display sections")

    def test_cold_and_warm_viewer_never_report_ready_for_misdirected_launch(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                device = Mock(spec=LDPlayer)
                empty = KeepWindows({0:True, 2:False}, False)
                wrong = KeepWindows({0:True, 2:False}, True)
                device.keep_windows.side_effect = [empty, empty, wrong]
                bridge = Mock(spec=VirtualPedometer)
                bridge.display_id = 2
                def adb(*args, **kwargs):
                    command = args[-1]
                    if command == "shell getprop ro.build.version.sdk":
                        return "34"
                    if "pm path" in command:
                        return "package:/data/app/base.apk"
                    if "resolve-activity" in command:
                        return "com.gotokeep.keep/.Main"
                    return "Status: ok"
                device.command.side_effect = adb
                with patch("sensor_bridge.VirtualPedometer.connect", return_value=bridge if existing else None), \
                     patch("sensor_bridge._start_step_bridge", return_value=bridge):
                    with self.assertRaisesRegex(DeviceError, "户外跑步页在雷电普通窗口"):
                        open_step_viewer(device, 0, threading.Event(), heading=90)
                bridge.close.assert_called_once()
                bridge.orient.assert_not_called()

    @patch("sensor_bridge.VirtualPedometer.connect")
    def test_successful_launch_that_leaves_viewer_empty_times_out(self, connect):
        device = Mock(spec=LDPlayer)
        device.keep_windows.return_value = KeepWindows({0:True, 2:False}, False)
        connect.return_value.display_id = 2
        replies = {"shell getprop ro.build.version.sdk":"34", "shell pm path com.gotokeep.keep":"package:/base.apk"}
        def adb(*args, **kwargs):
            if "resolve-activity" in args[-1]:
                return "com.gotokeep.keep/.Main"
            return replies.get(args[-1], "Status: ok")
        device.command.side_effect = adb
        with patch("sensor_bridge.time") as clock:
            clock.monotonic.side_effect = [100, 105]
            with self.assertRaisesRegex(DeviceError, "Keep 未进入计步窗口"):
                open_step_viewer(device, 0, threading.Event(), heading=90)
        connect.return_value.orient.assert_not_called()
        connect.return_value.close.assert_called_once()


class ExportTests(unittest.TestCase):
    def test_fit_decodes_to_requested_course_time_and_distance(self):
        from fit_tool import FitFile
        from fit_tool.profile.messages.record_message import RecordMessage
        from fit_tool.profile.messages.session_message import SessionMessage

        track = CampusTrack()
        plan = RunPlan.parse("0.453", "6:15", True, seed=21)
        end = datetime(2026, 11, 11, 8, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            path = Path(directory, "模拟跑.fit")
            export_fit(path, track, plan, end)
            messages = [r.message for r in FitFile.from_file(str(path)).records]
        samples = [m for m in messages if isinstance(m, RecordMessage)]
        session = next(m for m in messages if isinstance(m, SessionMessage))
        self.assertEqual(len(samples), round(plan.duration) + 1)
        self.assertEqual(session.total_distance, samples[-1].distance)
        self.assertEqual(samples[-1].distance, 453)
        self.assertEqual(samples[-1].timestamp, int(end.timestamp()) * 1000)
        steps = plan.pacing.at(plan.duration).steps
        self.assertEqual(2*(session.total_cycles+session.total_fractional_cycles), steps)
        self.assertEqual(samples[-1].total_cycles, steps//2)
        self.assertGreater(len({(r.cadence, r.fractional_cadence) for r in samples}), 20)
        self.assertGreater(len({r.speed for r in samples}), 20)
        self.assertTrue(all(a.total_cycles <= b.total_cycles for a, b in pairwise(samples)))
        for i in (0, len(samples)//2, len(samples)-1):
            motion = plan.pacing.at(plan.duration*i/(len(samples)-1))
            self.assertEqual(samples[i].total_cycles, motion.steps//2)
            self.assertAlmostEqual(2*(samples[i].cadence+samples[i].fractional_cadence),
                                   motion.cadence*plan.duration/round(plan.duration), delta=.016)
            self.assertAlmostEqual(samples[i].distance, motion.meters, delta=0.01)
            self.assertAlmostEqual(samples[i].step_length, motion.speed*60000/motion.cadence, delta=0.1)
        for row, expected in ((samples[0], track.start_point(plan)), (samples[-1], track.travel_path(plan).at(453))):
            self.assertLess(distance_m((row.position_long, row.position_lat), expected), 0.02)

    def test_fit_laps_preserve_odd_steps_and_tiny_final_partial(self):
        from fit_tool import FitFile
        from fit_tool.profile.messages.lap_message import LapMessage
        from fit_tool.profile.messages.session_message import SessionMessage

        with TemporaryDirectory() as directory:
            file = Path(directory, "lake.fit")
            for meters in (10, 1001, 3000):
                with self.subTest(meters=meters):
                    plan = RunPlan(meters, 360, False, 5432)
                    export_fit(file, CampusTrack("lake"), plan)
                    messages = [r.message for r in FitFile.from_file(str(file)).records]
                    laps = [m for m in messages if isinstance(m, LapMessage)]
                    session = next(m for m in messages if isinstance(m, SessionMessage))
                    self.assertEqual(len(laps), session.num_laps)
                    self.assertEqual(sum(l.total_distance for l in laps), meters)
                    self.assertEqual(sum(l.total_timer_time for l in laps), round(plan.duration))
                    self.assertTrue(all(l.total_timer_time > 0 for l in laps))
                    steps = sum(2*l.total_cycles + 2*l.total_fractional_cycles for l in laps)
                    self.assertEqual(steps, plan.pacing.at(plan.duration).steps)
                    self.assertEqual(steps, 2*(session.total_cycles+session.total_fractional_cycles))
                    if meters == 3000:
                        self.assertEqual([l.total_distance for l in laps], [1000]*3)
                        self.assertEqual(steps % 2, 1)


class DashboardTests(unittest.TestCase):
    def test_default_route_distance_and_source_tab_with_old_preferences(self):
        import tkinter as tk
        from app import Dashboard
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        root.withdraw()
        try:
            with TemporaryDirectory() as folder:
                prefs = Path(folder, "settings.json")
                prefs.write_text(json.dumps({"km": "3.05", "pace": "6:00"}), encoding="utf-8")
                with patch("app.find_console", return_value=None):
                    dash = Dashboard(root, prefs)
                self.assertEqual((dash.km.get(), dash.track.route_id, dash.plan.duration), ("3.00", "lake", 1080))
                self.assertEqual(dash.modes.tab(dash.modes.select(), "text"), "Keep 实时记录")
                self.assertTrue(dash.motion_enabled.get())
                self.assertTrue(dash.random_start.get())
                self.assertEqual(dash.map.start, dash.track.start_point(dash.plan))
                with patch("app.LDPlayer") as factory, patch.object(
                    dash, "task", side_effect=lambda kind, msg, work: work(PlaybackControl())
                ):
                    dash.device_action("locate")
                    factory.return_value.locate.assert_called_once_with(0, dash.map.start)
                dash.random_start.set(False)
                dash.change_start()
                self.assertEqual(dash.map.start, dash.track.points[0])
                dash.km.set("2.5")
                dash.route.set("开发区校区田径场")
                dash.change_route()
                self.assertEqual((dash.plan.meters, dash.track.route_id), (2500, "track"))
                with patch("app.randbits", return_value=456):
                    dash.reroll()
                self.assertEqual(dash.plan.seed, 456)
                dash.save_settings()
                self.assertNotIn("km", json.loads(prefs.read_text(encoding="utf-8")))
                self.assertFalse(json.loads(prefs.read_text(encoding="utf-8"))["random_start"])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
