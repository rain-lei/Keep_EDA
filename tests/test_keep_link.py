from threading import Thread
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import time
import unittest

from keep_link import RunMonitor
from ldplayer import DeviceError
from playback import PlaybackControl, Sample
from campus import RunPlan
from live_history import LastReplay


def until(predicate, timeout=2):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.01)
    return False


class KeepState:
    state = "PAUSED"

    def __init__(self):
        self.actions = []

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
            self.assertTrue(until(lambda:bool(notices)))
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
            self.assertTrue(until(lambda:bridge.state == "PAUSED"))
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


if __name__ == "__main__":
    unittest.main()
