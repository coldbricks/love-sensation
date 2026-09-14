"""Editor workflow checks using synthetic paths and mocked media operations."""
import copy
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


class MusicVideoWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication
            from platinum_sorter.pmv_dialog import PmvForgeDialog
        except ImportError as error:
            raise unittest.SkipTest(f"Editor dependencies unavailable: {error}")
        cls.app = QApplication.instance() or QApplication([])
        cls.dialog_type = PmvForgeDialog

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test_music_video_workflow_")
        self.root = Path(self.temp.name)
        self.footage = self.root / "footage"
        self.footage.mkdir()
        self.audio = self.root / "music.wav"
        self.audio.write_bytes(b"synthetic soundtrack identity only")
        self.dialogs = []
        self.gates = []
        self.patches = []
        self.grid = {
            "reliable": True, "bpm": 120.0, "beat_period": 0.5,
            "total_duration_s": 4.0, "bars": [0.0, 2.0],
            "beats": [index * 0.5 for index in range(8)],
            "drop_bars": [], "breakdown_bars": [],
        }
        self.prepare = self.mock("platinum_sorter.pmv_dialog.prepare_candidate_clips", side_effect=self.metadata)
        self.library_prepare = self.mock("platinum_sorter.clip_library.prepare_candidate_clips", side_effect=self.metadata)
        self.detect = self.mock("platinum_sorter.pmv_dialog.detect_tempo_and_grid", return_value=self.grid)
        self.export = self.mock("platinum_sorter.pmv_dialog.export_fcp7_xml")
        self.report = self.mock("platinum_sorter.pmv_dialog.generate_flight_report")
        self.save_picker = self.mock("platinum_sorter.pmv_dialog.QFileDialog.getSaveFileName",
                                     return_value=(str(self.root / "timeline.xml"), ""))
        self.mock("platinum_sorter.pmv_dialog.QMessageBox.warning")
        self.mock("platinum_sorter.pmv_dialog.QMessageBox.critical")
        self.open_url = self.mock("platinum_sorter.pmv_dialog.QDesktopServices.openUrl")

    def tearDown(self):
        # Always release a gated worker before destroying Qt objects, including on failure.
        for gate in self.gates:
            gate.set()
        for dialog in self.dialogs:
            if dialog.is_busy():
                dialog.request_stop()
                self.wait_until(lambda: not dialog.is_busy())
            dialog.close()
            dialog.deleteLater()
        self.app.processEvents()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def mock(self, name, **kwargs):
        item = patch(name, **kwargs)
        self.patches.append(item)
        return item.start()

    @staticmethod
    def metadata(clips):
        return [{**clip, "source": clip["path"], "duration_s": 8.0,
                 "width": 320, "height": 240, "fps": 30.0, "fps_ratio": "30/1",
                 "frame_count": 240, "media_type": "video"} for clip in clips]

    def video(self, name="clip.mp4"):
        path = self.footage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic footage identity only")
        return path

    def dialog(self, **kwargs):
        dialog = self.dialog_type(settings_path=self.root / f"editor-{len(self.dialogs)}.json", **kwargs)
        self.dialogs.append(dialog)
        return dialog

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def wait_until(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(condition(), "Timed out waiting for editor worker/UI state")

    def wait_idle(self, dialog):
        self.wait_until(lambda: not dialog.is_busy())

    def analyze_music(self, dialog):
        dialog.audio_edit.setText(str(self.audio))
        dialog._start_beat_analysis()
        self.wait_idle(dialog)
        self.assertIsNotNone(dialog.audio_grid)

    def ready(self):
        source = self.video()
        dialog = self.dialog()
        dialog.footage_edit.setText(str(self.footage))
        dialog._start_footage_load()
        self.wait_idle(dialog)
        self.analyze_music(dialog)
        self.assertTrue(dialog.assemble_btn.isEnabled())
        return dialog, source

    def build(self, dialog):
        dialog._assemble_and_export()
        self.wait_idle(dialog)
        self.assertGreater(len(dialog.cuts), 0)
        self.assertEqual(dialog.timeline_table.rowCount(), len(dialog.cuts))
        self.assertTrue(dialog.export_btn.isEnabled())

    def test_initial_picker_visible_and_audio_alone_cannot_build(self):
        dialog = self.dialog()
        dialog.show()
        self.app.processEvents()
        self.assertTrue(dialog.footage_edit.isVisibleTo(dialog))
        self.assertTrue(dialog.footage_browse.isVisibleTo(dialog))
        self.assertTrue(dialog.footage_browse.isEnabled())
        self.assertFalse(dialog.assemble_btn.isEnabled())
        self.assertFalse(dialog.export_btn.isEnabled())
        self.analyze_music(dialog)
        self.assertFalse(dialog.assemble_btn.isEnabled())
        dialog._assemble_and_export()
        self.assertFalse(dialog.is_busy())
        self.assertEqual(dialog.cuts, [])
        self.assertFalse(dialog.export_btn.isEnabled())

    def test_footage_loading_is_async_counts_skips_and_new_folder_invalidates(self):
        self.video("a.mp4")
        self.video("broken.mp4")
        self.video("nested/b.MOV")
        started = threading.Event()
        release = self.gate()
        worker_threads = []

        def prepare(clips):
            worker_threads.append(threading.get_ident())
            started.set()
            release.wait(4)
            if Path(clips[0]["path"]).name == "broken.mp4":
                raise ValueError("Unreadable test video")
            return self.metadata(clips)

        self.library_prepare.side_effect = prepare
        dialog = self.dialog()
        dialog.footage_edit.setText(str(self.footage))
        dialog._start_footage_load()
        self.wait_until(started.is_set)
        self.assertTrue(dialog.is_busy())
        self.assertFalse(dialog.load_clips_btn.isEnabled())
        self.assertFalse(dialog.footage_edit.isEnabled())
        self.assertTrue(all(ident != threading.get_ident() for ident in worker_threads))
        release.set()
        self.wait_idle(dialog)
        self.assertEqual(len(dialog.candidate_clips), 2)
        self.assertIn("2 clips ready", dialog.clip_count_lbl.text())
        self.assertIn("1 skipped", dialog.clip_count_lbl.text())
        self.assertIn("broken.mp4", dialog.clip_count_lbl.toolTip())
        self.analyze_music(dialog)
        self.build(dialog)
        dialog.footage_edit.setText(str(self.root / "another-folder"))
        self.assertEqual(dialog.candidate_clips, [])
        self.assertEqual(dialog.cuts, [])
        self.assertEqual(dialog.timeline_table.rowCount(), 0)
        self.assertFalse(dialog.assemble_btn.isEnabled())
        self.assertFalse(dialog.export_btn.isEnabled())
        self.assertIsNotNone(dialog.audio_grid)

    def test_path_progress_from_scanner_reaches_status_label(self):
        source = self.video()
        reported = threading.Event()
        release = self.gate()

        def scan(folder, recursive, cancel_event, progress):
            progress(1, 2, source)
            reported.set()
            release.wait(4)
            return {"clips": self.metadata([{"path": str(source)}]), "skipped": [], "cancelled": False}

        self.mock("platinum_sorter.pmv_dialog.scan_clip_folder", side_effect=scan)
        dialog = self.dialog()
        dialog.footage_edit.setText(str(self.footage))
        dialog._start_footage_load()
        self.wait_until(reported.is_set)
        self.app.processEvents()
        self.assertIn("1/2", dialog.job_status.text())
        self.assertIn(source.name, dialog.job_status.text())
        release.set()
        self.wait_idle(dialog)

    def test_stale_footage_job_does_not_restore_old_candidates(self):
        source = self.video()
        started = threading.Event()
        release = self.gate()

        def scan(*args, **kwargs):
            started.set()
            release.wait(4)
            return {"clips": self.metadata([{"path": str(source)}]), "skipped": [], "cancelled": False}

        self.mock("platinum_sorter.pmv_dialog.scan_clip_folder", side_effect=scan)
        dialog = self.dialog()
        dialog.footage_edit.setText(str(self.footage))
        dialog._start_footage_load()
        self.wait_until(started.is_set)
        dialog.footage_edit.setText(str(self.root / "new-choice"))
        release.set()
        self.wait_idle(dialog)
        self.assertEqual(dialog.candidate_clips, [])
        self.assertFalse(dialog.assemble_btn.isEnabled())
        self.assertIn("Inputs changed", dialog.job_status.text())

    def test_edit_controls_invalidate_preview_and_export(self):
        dialog, _ = self.ready()
        for change in (
            lambda: dialog.chaos_slider.setValue(70),
            lambda: dialog.seed_spin.setValue(12),
            lambda: dialog.fps_combo.setCurrentIndex(1),
        ):
            with self.subTest(change=change):
                self.build(dialog)
                change()
                self.assertEqual(dialog.cuts, [])
                self.assertEqual(dialog.timeline_table.rowCount(), 0)
                self.assertFalse(dialog.export_btn.isEnabled())
                self.assertTrue(dialog.assemble_btn.isEnabled())
        self.build(dialog)
        dialog.bpm_spin.setValue(121)
        self.assertIsNone(dialog.audio_grid)
        self.assertEqual(dialog.cuts, [])
        self.assertFalse(dialog.assemble_btn.isEnabled())
        self.assertFalse(dialog.export_btn.isEnabled())

    def test_stale_timeline_job_does_not_enable_export(self):
        from platinum_sorter.pmv_forge import assemble_pmv_timeline
        dialog, _ = self.ready()
        started = threading.Event()
        release = self.gate()

        def assemble(*args, **kwargs):
            started.set()
            release.wait(4)
            return assemble_pmv_timeline(*args, **kwargs)

        self.mock("platinum_sorter.pmv_dialog.assemble_pmv_timeline", side_effect=assemble)
        dialog._assemble_and_export()
        self.wait_until(started.is_set)
        dialog.seed_spin.setValue(dialog.seed_spin.value() + 1)
        release.set()
        self.wait_idle(dialog)
        self.assertEqual(dialog.cuts, [])
        self.assertFalse(dialog.export_btn.isEnabled())
        self.assertIn("Inputs changed", dialog.job_status.text())

    def test_preview_exports_identical_cuts_without_reassembling(self):
        from platinum_sorter.pmv_forge import assemble_pmv_timeline
        assemble = self.mock("platinum_sorter.pmv_dialog.assemble_pmv_timeline", wraps=assemble_pmv_timeline)
        dialog, _ = self.ready()
        self.build(dialog)
        preview = copy.deepcopy(dialog.cuts)
        self.export.assert_not_called()
        self.save_picker.assert_not_called()
        dialog._export_timeline()
        self.wait_idle(dialog)
        self.export.assert_called_once()
        self.assertEqual(self.export.call_args.args[0], preview)
        self.assertEqual(assemble.call_count, 1)
        self.assertEqual(dialog.cuts, preview)
        self.assertEqual(self.report.call_args.kwargs["cuts"], [
            {"clip_name": cut.clip_name, "timeline_start_s": cut.timeline_start_s,
             "timeline_end_s": cut.timeline_end_s, "tag": cut.tag} for cut in preview])
        self.assertIn("Saved", dialog.job_status.text())
        self.assertTrue(dialog.export_btn.isEnabled())
        self.open_url.assert_not_called()

    def test_source_changed_after_preview_blocks_export(self):
        dialog, source = self.ready()
        self.build(dialog)
        source.write_bytes(b"different file content and length after the timeline preview")
        dialog._export_timeline()
        self.wait_idle(dialog)
        self.export.assert_not_called()
        self.report.assert_not_called()
        self.assertIn("Footage changed since the preview", dialog.job_status.text())

    def test_safe_close_waits_for_worker_and_discards_its_result(self):
        source = self.video()
        started = threading.Event()
        release = self.gate()

        def scan(*args, **kwargs):
            started.set()
            release.wait(4)
            return {"clips": self.metadata([{"path": str(source)}]), "skipped": [], "cancelled": False}

        self.mock("platinum_sorter.pmv_dialog.scan_clip_folder", side_effect=scan)
        dialog = self.dialog()
        dialog.show()
        dialog.footage_edit.setText(str(self.footage))
        completed = []
        dialog.work_finished.connect(lambda: completed.append(True))
        dialog._start_footage_load()
        self.wait_until(started.is_set)
        dialog.close()
        self.assertTrue(dialog._close_when_finished)
        self.assertTrue(dialog.is_busy())
        self.assertTrue(dialog._worker.cancel.is_set())
        self.assertTrue(dialog.isVisible())
        release.set()
        self.wait_idle(dialog)
        self.assertFalse(dialog.isVisible())
        self.assertEqual(dialog.candidate_clips, [])
        self.assertEqual(completed, [True])
        self.assertTrue(dialog.settings_path.is_file())


if __name__ == "__main__":
    unittest.main()
