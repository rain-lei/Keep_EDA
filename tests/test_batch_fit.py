from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from batch_fit import BEIJING, DEFAULT_PERIOD, archive_name, export_batch, parse_dates, read_dates, schedule_runs
from campus import CampusTrack, RunPlan
from ldplayer import DeviceError, LDPlayer, wait_for_android


class BatchTests(unittest.TestCase):
    plan = RunPlan(3000, 360, False, 5432)

    def test_download_waits_for_selected_instance_and_storage_on_cold_boot(self):
        adb = Mock(side_effect=[DeviceError("device 'emulator-5570' not found"), "34", "", "34", "1"])
        cancel = Mock()
        cancel.is_set.return_value = False
        with patch("ldplayer.socket.create_connection") as connect:
            self.assertEqual(wait_for_android(adb, cancel, shared_storage=True), 34)
        message = b"host:emulator:5571"
        connect.return_value.__enter__.return_value.sendall.assert_called_once_with(
            f"{len(message):04x}".encode() + message)
        self.assertEqual(cancel.wait.call_count, 2)

    def test_date_import_ranges_deduplicate_and_reject_invalid_days(self):
        expected = [date(2024,2,29), date(2024,3,1), date(2024,3,2), date(2024,3,8)]
        self.assertEqual(parse_dates("2024/3/8、2024-02-29 至 2024-03-02\n2024.3.1"), expected)
        for text in ("2023-2-29", "2026-11-2 ~ 2026-11-1", "2026-11-1,坏数据", "日期"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_dates(text)
        with TemporaryDirectory() as directory:
            path = Path(directory, "日期.csv")
            for coding in ("utf-8-sig", "gb18030", "utf-16"):
                path.write_text('日期\n"2024-2-29 至 2024-3-2"\n"2024-3-8"\n', encoding=coding)
                self.assertEqual(read_dates(path), expected)

    def test_windows_are_local_and_reroll_changes_only_time(self):
        days = parse_dates("2026-11-01~2026-11-30")
        old = schedule_runs(days, self.plan, DEFAULT_PERIOD, 517)
        new = schedule_runs(days, self.plan, DEFAULT_PERIOD, 518)
        self.assertEqual(old[4], schedule_runs([days[4]], self.plan, DEFAULT_PERIOD, 517)[0])
        self.assertEqual({r.start.hour >= 18 for r in old}, {True,False})
        self.assertEqual([r.route_seed for r in old], [r.route_seed for r in new])
        self.assertEqual(len(set(r.route_seed for r in old)),len(days))
        self.assertNotEqual([r.start for r in old],[r.start for r in new])
        for day, run in zip(days, old):
            self.assertEqual((run.start.date(),run.start.tzinfo,run.end-run.start),(day,BEIJING,timedelta(minutes=18)))
            lo,hi = ("14:00","18:00") if run.start.hour < 18 else ("18:30","21:30")
            self.assertGreaterEqual(run.start.strftime("%H:%M:%S"), lo+":00")
            self.assertLessEqual(run.end.strftime("%H:%M:%S"), hi+":00")
        with self.assertRaises(ValueError):
            schedule_runs(days,self.plan,"自定义",4,("14:00","14:17"))
        end = schedule_runs([days[0]],self.plan,"自定义",4,("23:42","24:00"))[0].end
        self.assertEqual(end, datetime(2026,11,2,0,0,tzinfo=BEIJING))

    def test_zip_records_use_previewed_dates_even_across_utc_midnight(self):
        from fit_tool import FitFile
        from fit_tool.profile.messages.activity_message import ActivityMessage
        from fit_tool.profile.messages.lap_message import LapMessage
        from fit_tool.profile.messages.record_message import RecordMessage
        from fit_tool.profile.messages.session_message import SessionMessage
        days = parse_dates("2026-10-31,2026-11-02")
        runs = schedule_runs(days,self.plan,"自定义",117,("06:00","06:18"))
        epoch = datetime(1989,12,31,tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            dest = Path(directory,archive_name(runs))
            track = CampusTrack("lake")
            self.assertTrue(export_batch(dest,track,self.plan,runs,Event(),lambda *_: None))
            with ZipFile(dest) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(len(archive.namelist()),len(days))
                for name,run in zip(archive.namelist(),runs):
                    self.assertEqual(name,f"DUT-EDA-sim-{run.start:%Y%m%d-%H%M%S}-3000m.fit")
                    records = FitFile.from_bytes(archive.read(name)).records
                    data = [x.message for x in records if not x.is_definition]
                    samples = [m for m in data if isinstance(m,RecordMessage)]
                    session = next(m for m in data if isinstance(m,SessionMessage))
                    laps = [m for m in data if isinstance(m,LapMessage)]
                    self.assertEqual(samples[0].timestamp,int(run.start.timestamp())*1000)
                    self.assertEqual(samples[-1].timestamp,int(run.end.timestamp())*1000)
                    self.assertEqual((session.start_time,session.total_distance,session.total_timer_time),
                                     (samples[0].timestamp,3000,1080))
                    self.assertEqual((laps[0].start_time,laps[-1].timestamp),(samples[0].timestamp,samples[-1].timestamp))
                    activity = next(m for m in data if isinstance(m,ActivityMessage))
                    self.assertEqual(activity.local_timestamp,int((run.end-epoch).total_seconds())+8*3600)
            initial = dest.read_bytes()
            cancel = Event()
            self.assertFalse(export_batch(dest,track,self.plan,runs,cancel,lambda *_: cancel.set()))
            self.assertEqual(dest.read_bytes(),initial)
            with patch("batch_fit.MAX_ZIP_BYTES",128), self.assertRaisesRegex(ValueError,"500M"):
                export_batch(dest,track,self.plan,runs,Event(),lambda *_: None)
            self.assertEqual(list(dest.parent.iterdir()),[dest])
            self.assertEqual(dest.read_bytes(),initial)
            self.assertTrue(export_batch(dest,track,self.plan,runs,Event(),lambda *_: None))
            self.assertEqual(dest.read_bytes(),initial)

    def test_single_fit_cancellation_and_same_size_corruption_never_publish(self):
        device = LDPlayer(Path("ldconsole.exe"))
        with TemporaryDirectory() as folder:
            source = Path(folder, "activity.fit")
            source.write_bytes(b"fit data")
            for mode in ("cancel", "damaged"):
                with self.subTest(mode=mode):
                    cancel = Event()
                    def command(*args, **kwargs):
                        cmd = args[-1]
                        if cmd.startswith("push ") and mode == "cancel":
                            cancel.set()
                        if "getprop" in cmd:
                            return "1" if "boot_completed" in cmd else "34"
                        if "stat -c" in cmd:
                            return str(source.stat().st_size)
                        if "sha256sum" in cmd:
                            return "0" * 64
                        return ""
                    with patch.object(device, "command", side_effect=command) as calls:
                        with self.assertRaises(InterruptedError if mode == "cancel" else DeviceError):
                            device.send_fit(0, source, 3000, cancel)
                    commands = [call.args[-1] for call in calls.call_args_list]
                    self.assertFalse(any(cmd.startswith("shell mv ") for cmd in commands))
                    self.assertTrue(commands[-1].startswith("shell rm -f ") and commands[-1].endswith(".part"))

    def test_corrupt_or_cancelled_transfer_never_publishes_its_zip(self):
        from hashlib import sha256
        device = LDPlayer(Path("ldconsole.exe"))
        with TemporaryDirectory() as directory:
            source = Path(directory,"dates.zip")
            source.write_bytes(b"test zip")
            digest = sha256(source.read_bytes()).hexdigest()
            for mode in ("corrupt", "cancel", "success"):
                with self.subTest(mode=mode):
                    cancel = Event()
                    def android(*args,**kwargs):
                        cmd = args[-1]
                        if cmd == "shell getprop ro.build.version.sdk":
                            return "34"
                        if cmd == "shell getprop sys.boot_completed":
                            return "1"
                        if "stat -c" in cmd:
                            return str(source.stat().st_size)
                        if "sha256sum" in cmd:
                            if mode == "cancel":
                                cancel.set()
                            return "wrong" if mode == "corrupt" else digest+"  file"
                        return ""
                    with patch.object(device,"command",side_effect=android) as calls:
                        if mode == "success":
                            self.assertEqual(device.send_batch_zip(2,source,cancel),f"dates-{digest[:10]}.zip")
                        else:
                            with self.assertRaises(InterruptedError if mode=="cancel" else DeviceError):
                                device.send_batch_zip(2,source,cancel)
                        commands=[c.args[-1] for c in calls.call_args_list]
                        self.assertEqual(any(c.startswith("shell mv ") for c in commands),mode=="success")
                        self.assertEqual(any(c.startswith("shell rm -f ") for c in commands),mode!="success")
