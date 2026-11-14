from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from zipfile import ZipFile

from batch_fit import (DEFAULT_PERIOD, edit_run, export_batch, filter_dates, parse_dates, schedule_runs)
from batch_state import BatchDraft, BatchStore
from campus import CampusTrack, RunPlan


class WorkspaceTests(unittest.TestCase):
    plan = RunPlan(3000, 360, False, 123)

    def test_weekdays_exclusions_and_weighted_periods(self):
        days = parse_dates("2026-11-01 至 2026-11-30")
        kept = filter_dates(days, range(5), [date(2026,11,2)])
        self.assertTrue(all(d.weekday() < 5 for d in kept))
        self.assertNotIn(date(2026,11,2), kept)
        for weight in (0, 100):
            runs = schedule_runs(days, self.plan, DEFAULT_PERIOD, 55, evening_percent=weight)
            self.assertTrue(all((r.start.hour >= 18) == bool(weight) for r in runs))
        runs = schedule_runs(days, self.plan, "工作日晚 / 周末午后", 55)
        self.assertTrue(all((r.start.hour >= 18) == (r.start.weekday() < 5) for r in runs))

    def test_per_day_overrides_restore_exact_fit_and_history(self):
        from fit_tool import FitFile
        from fit_tool.profile.messages.session_message import SessionMessage
        days = parse_dates("2026-11-02,2026-11-03")
        rows = schedule_runs(days, self.plan, DEFAULT_PERIOD, 999)
        edited = edit_run(rows[1], self.plan, km="1.5", pace="6:20", clock="19:12:34",
                          route_id="track", clockwise=True, random_start=False, locked=True)
        rows = (replace(rows[0], route_id="lake", plan=self.plan), edited)
        draft = BatchDraft("lake", self.plan, 999, "2026-11-02,2026-11-03", rows=rows)
        with TemporaryDirectory() as folder:
            store = BatchStore(Path(folder, "batches.sqlite3"))
            store.save_draft(draft)
            restored = BatchStore(store.path).load_draft()
            self.assertEqual(restored, draft)
            path = Path(folder, "test.zip")
            self.assertTrue(export_batch(path,CampusTrack("lake"),restored.plan,restored.rows,Event(),lambda *_:None))
            first = path.read_bytes()
            with ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                for name, row in zip(archive.namelist(),rows):
                    records = FitFile.from_bytes(archive.read(name)).records
                    session = next(r.message for r in records if not r.is_definition and isinstance(r.message,SessionMessage))
                    expected = row.effective_plan(self.plan)
                    self.assertEqual(session.total_distance, expected.meters)
                    self.assertEqual(session.total_timer_time, round(expected.duration))
                    self.assertEqual(session.start_time, row.start.timestamp()*1000)
                    self.assertEqual(session.timestamp, row.end.timestamp()*1000)
            self.assertFalse(store.history())
            store.record_export(path,"保存 ZIP",str(path),draft,rows)
            store.record_export(path,"保存 ZIP",str(path),draft,rows)
            self.assertEqual(len(store.history()),1)
            self.assertEqual(store.exported_dates(),{"2026-11-02","2026-11-03"})
            recovered = store.restore_export(store.history()[0]["id"])
            export_batch(path,CampusTrack(recovered.route_id),recovered.plan,recovered.rows,Event(),lambda *_:None)
            self.assertEqual(path.read_bytes(),first)

    def test_invalid_edit_or_mismatched_preview_does_not_publish(self):
        run = schedule_runs([date(2026,11,1)],self.plan,"下午",5)[0]
        with self.assertRaisesRegex(ValueError,"超过当天"):
            edit_run(run,self.plan,km="3",pace="6:00",clock="23:50",route_id="lake",
                     clockwise=False,random_start=True,locked=True)
        with TemporaryDirectory() as folder:
            dest = Path(folder,"prior.zip")
            dest.write_bytes(b"prior file")
            bad = replace(run,end=run.end+timedelta(seconds=1))
            with self.assertRaisesRegex(ValueError,"不一致"):
                export_batch(dest,CampusTrack("lake"),self.plan,(bad,),Event(),lambda *_:None)
            self.assertEqual(dest.read_bytes(),b"prior file")

    def test_locked_rows_and_filter_restore_in_dialog(self):
        import tkinter as tk
        from batch_dialog import BatchDialog
        root = tk.Tk()
        root.withdraw()
        with TemporaryDirectory() as folder:
            store = BatchStore(Path(folder,"batches.sqlite3"))
            dialog = BatchDialog(root,CampusTrack("lake"),self.plan,None,lambda *_:None,lambda:None,store)
            def flush():
                if dialog.pending is not None:
                    dialog.window.after_cancel(dialog.pending)
                dialog.refresh()
                root.update_idletasks()
            try:
                dialog.set_dates("2026-11-02 至 2026-11-05")
                flush()
                original = dialog.runs
                dialog.tree.selection_set("2026-11-02")
                dialog.toggle_lock()
                flush()
                locked = dialog.rows["2026-11-02"]
                dialog.redraw()
                flush()
                self.assertEqual(dialog.runs[0],locked)
                self.assertNotEqual(dialog.runs[1].start,original[1].start)
                dialog.exclusions.set("2026-11-03")
                flush()
                self.assertEqual(len(dialog.runs),3)
                dialog.period.set("上午")
                flush()
                confirmed = dialog.runs
                dialog.close()
                dialog = BatchDialog(root,CampusTrack("track"),RunPlan(2000,400,True,987),None,
                                     lambda *_:None,lambda:None,store)
                self.assertEqual(dialog.runs,confirmed)
                self.assertEqual(dialog.plan,self.plan)
                self.assertEqual(dialog.track.route_id,"lake")
                dialog.exclusions.set("")
                flush()
                self.assertLess(dialog.rows["2026-11-03"].start.hour,12)
                self.assertEqual(dialog.runs[0],locked)
            finally:
                if dialog.window.winfo_exists():
                    dialog.close()
                root.destroy()


if __name__ == "__main__":
    unittest.main()
