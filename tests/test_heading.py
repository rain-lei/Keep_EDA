from contextlib import nullcontext
from math import atan2, degrees, hypot
from pathlib import Path
import shutil
import socket
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from campus import CampusTrack, Line, RunPlan, bearing_degrees
from ldplayer import DeviceError, LDPlayer
from playback import replay, replay_on_device
from sensor_bridge import VirtualPedometer
from test_controller import VirtualClock


def difference(a, b):
    return (a-b+180) % 360-180


class HeadingTests(unittest.TestCase):
    def test_cardinals_endpoints_and_reversing(self):
        for endpoint, expected in (((0, .001), 0), ((.001, 0), 90), ((0, -.001), 180), ((-.001, 0), 270)):
            with self.subTest(expected=expected):
                line = Line([(0, 0), endpoint])
                for offset in (-10, 0, line.length/2, line.length, line.length+10):
                    self.assertAlmostEqual(line.heading_at(offset), expected)
                reverse = Line([endpoint, (0, 0)])
                self.assertAlmostEqual(reverse.heading_at(0), (expected+180) % 360)

    def test_corner_turns_smoothly_through_north_and_hairpin_stays_finite(self):
        line = Line([(-.0001, -.001), (0, 0), (-.0001, .001)])
        corner = line.offsets[1]
        headings = [line.heading_at(corner-4+i/10) for i in range(81)]
        self.assertLess(abs(difference(headings[40], 0)), .01)
        self.assertLess(max(abs(difference(b, a)) for a, b in zip(headings, headings[1:])), 1)
        hairpin = Line([(0, 0), (0, .001), (0, 0)])
        self.assertAlmostEqual(hairpin.heading_at(hairpin.offsets[1]), 180)

    def test_actual_randomized_routes_and_preview_use_travel_heading(self):
        for route in ("lake", "track"):
            for clockwise in (True, False):
                plan = RunPlan(500, 360, clockwise, 5432)
                track = CampusTrack(route)
                path = track.travel_path(plan)
                for preview in (False, True):
                    samples = []
                    self.assertTrue(replay(track, plan, VirtualClock(), samples.append, preview=preview))
                    for sample in samples:
                        self.assertTrue(0 <= sample.heading < 360)
                        self.assertEqual(sample.heading, path.heading_at(sample.meters))
                    self.assertEqual(samples[0].point, track.start_point(plan))
                    self.assertEqual(samples[-1].meters, 500)
                a, b = path.at(20), path.at(25)
                self.assertLess(abs(difference(path.heading_at(22.5), bearing_degrees(a, b))), 10)

    @patch("playback.VirtualPedometer.connect")
    def test_device_gets_heading_at_start_and_each_position(self, connect):
        device = Mock(spec=LDPlayer)
        device.reserve_location.return_value = nullcontext()
        device.watch_instance.return_value = nullcontext(lambda: None)
        device.prepare.return_value = True
        samples = []
        track, plan = CampusTrack("lake"), RunPlan(10, 120, True, 123)
        self.assertTrue(replay_on_device(track, plan, VirtualClock(), device, 0, samples.append))
        calls = connect.return_value.orient.call_args_list
        self.assertEqual(calls[0].args, (track.travel_path(plan).heading_at(0),))
        self.assertTrue(calls[0].kwargs["immediate"])
        self.assertEqual([c.args[0] for c in calls[1:]], [s.heading for s in samples])
        self.assertEqual(device.locate.call_count, len(samples))

    def test_heading_protocol_normalizes_and_rejects_old_component(self):
        device = Mock(spec=LDPlayer)
        device.command.return_value = "31042"
        outbound, inbound = socket.socketpair()
        received = []

        def serve():
            with inbound, inbound.makefile("rb") as reader:
                inbound.sendall(b"EDA3 8\n")
                for raw in reader:
                    received.append(raw.decode().strip())
                    inbound.sendall(b"OK\n")

        listener = Thread(target=serve, daemon=True)
        listener.start()
        with patch("sensor_bridge.socket.create_connection", return_value=outbound):
            bridge = VirtualPedometer.connect(device, 0)
        try:
            bridge.orient(-1, immediate=True)
            bridge.orient(361)
            for invalid in (float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    bridge.orient(invalid)
            bridge.version = 2
            with self.assertRaisesRegex(DeviceError, "方向传感器"):
                bridge.orient(90)
        finally:
            bridge.close()
        listener.join(1)
        self.assertEqual(received, ["HEADING 359.000000 1", "HEADING 1.000000 0"])

    def test_map_arrow_points_in_compass_direction(self):
        import tkinter as tk
        from route_view import RouteView
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        root.withdraw()
        try:
            view = RouteView(root, CampusTrack())
            point = view.track.points[0]
            for heading in (0, 90, 180, 270, 359):
                view.set_position(point, False, heading=heading)
                x, y = view.project(point)
                tip_x, tip_y = view.coords(view.find_withtag("heading")[0])[:2]
                actual = degrees(atan2(tip_x-x, y-tip_y)) % 360
                self.assertAlmostEqual(difference(actual, heading), 0)
                self.assertAlmostEqual(hypot(tip_x-x, tip_y-y), 12)
        finally:
            root.destroy()

    @unittest.skipUnless(shutil.which("javac") and shutil.which("java"), "JDK required for Android attitude math")
    def test_android_attitude_axes_and_shortest_arc(self):
        root = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as folder:
            subprocess.run(["javac", "--release", "8", "-encoding", "UTF-8", "-d", folder,
                            str(root/"android"/"Heading.java"), str(root/"tests"/"java"/"HeadingTest.java")],
                           check=True, capture_output=True, timeout=30)
            subprocess.run(["java", "-cp", folder, "xyz.keepeda.sensors.HeadingTest"],
                           check=True, capture_output=True, timeout=15)
