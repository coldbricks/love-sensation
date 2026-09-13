"""Real offscreen Qt review actions using temporary synthetic images only."""
from __future__ import annotations

import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout

from platinum_sorter import ui
from platinum_sorter.contracts import SortOptions
from platinum_sorter.engine import SorterEngine, load_report


class FakeDetector:
    fingerprint = "ui-review-synthetic-v1"
    info = {"provider": "synthetic-test", "gpu": False}

    def detect_batch(self, paths):
        return [[{"class": "PORTRAIT", "score": 0.95, "box": [1, 1, 10, 10]}] for _ in paths]


class ReviewUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="love-review-ui-")
        self.root = Path(self.temporary.name).resolve()
        self.source, self.output = self.root / "source", self.root / "output"
        self.source.mkdir()
        # Small Qt-created fixture images exercise preview display without any
        # detector/model, user media, external process or download.
        for name, color in [("keep.png", "#406080"), ("exclude.png", "#804060"), ("hidden.png", "#608040")]:
            image = QImage(24, 24, QImage.Format.Format_RGB32)
            image.fill(QColor(color))
            self.assertTrue(image.save(str(self.source / name)))
        self.errors = []
        self.patches = [
            patch.object(ui, "DATA_DIR", self.root / "data"),
            patch.object(ui, "SETTINGS_PATH", self.root / "data/settings.json"),
            patch.object(QMessageBox, "warning", side_effect=lambda *a, **k: self.errors.append(str(a[-1]))),
            patch.object(QMessageBox, "critical", side_effect=lambda *a, **k: self.errors.append(str(a[-1]))),
        ]
        for context in self.patches:
            context.start()
        self.engine = SorterEngine(self.root / "data", lambda: FakeDetector())
        self.report = self.engine.analyze(SortOptions(str(self.source), str(self.output)), lambda event: None, threading.Event())
        self.window = ui.create_window()
        self.window._engine = self.engine
        self.window.source_edit.setText(str(self.source))
        self.window.destination_edit.setText(str(self.output))
        self.window._phase = "analyze"
        self.window._on_report(self.report)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        try:
            self.wait_until(lambda: not self.window._busy)
            # Preview has its own worker and cold CUDA/torch startup can outlive
            # the filing task. Never delete a parent while its QThread is live.
            self.window._hide_preview()
            self.wait_until(lambda: self.window._preview_worker is None, timeout=45.0)
            self.window.close()
            self.window.deleteLater()
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        finally:
            for context in reversed(self.patches):
                context.stop()
            self.temporary.cleanup()

    def wait_until(self, predicate, timeout=10.0):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            # Yield the GIL explicitly: QTest.qWait can starve the Python
            # QThread worker between its filesystem calls on this Qt build.
            time.sleep(0.005)
        self.app.processEvents()
        self.assertTrue(predicate(), "Qt action did not complete")

    def select_filtered(self, query):
        self.window.search_edit.setText(query)
        self.app.processEvents()
        self.assertEqual(1, self.window.proxy.rowCount())
        self.window.table.selectRow(0)
        self.window.table.setFocus()
        self.app.processEvents()

    def test_exclude_filtered_selection_preserves_hidden_rows_and_persists_review(self):
        self.select_filtered("exclude.png")
        self.window.exclude_button.click()
        included = {Path(r.source).name: r.included for r in self.report.results}
        self.assertEqual({"exclude.png": False, "hidden.png": True, "keep.png": True}, included)
        self.assertEqual(Qt.CheckState.Unchecked, self.window.proxy.data(self.window.proxy.index(0, 0), Qt.ItemDataRole.CheckStateRole))
        reopened = load_report(Path(self.report.manifest_path))
        self.assertEqual(included, {Path(r.source).name: r.included for r in reopened.results})
        self.window.include_button.click()
        self.assertTrue(all(r.included for r in load_report(Path(self.report.manifest_path)).results))
        self.assertFalse(self.errors, self.errors)

    def test_apply_uses_included_review_scope_without_changing_originals(self):
        original = {path.name: path.read_bytes() for path in self.source.iterdir()}
        self.select_filtered("exclude.png")
        self.window.exclude_button.click()
        self.window.search_edit.clear()
        self.app.processEvents()
        self.assertTrue(self.window.apply_button.isEnabled())
        self.window.apply_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertFalse(self.errors, self.errors)
        outputs = {path.name for path in self.output.rglob("*.png")}
        self.assertEqual({"keep.png", "hidden.png"}, outputs)
        self.assertEqual(original, {path.name: path.read_bytes() for path in self.source.iterdir()})
        reopened = load_report(Path(self.report.manifest_path))
        excluded = next(r for r in reopened.results if Path(r.source).name == "exclude.png")
        self.assertFalse(excluded.included)
        self.assertEqual([], excluded.destinations)
        # A skipped item remains reviewable after the first copy completes.
        self.select_filtered("exclude.png")
        self.assertTrue(self.window.include_button.isEnabled())
        self.window.include_button.click()
        self.assertTrue(self.window.apply_button.isEnabled())
        self.window.apply_button.click()
        self.wait_until(lambda: not self.window._busy)
        self.assertFalse(self.errors, self.errors)
        self.assertEqual({"keep.png", "hidden.png", "exclude.png"}, {path.name for path in self.output.rglob("*.png")})
        self.assertEqual(original, {path.name: path.read_bytes() for path in self.source.iterdir()})
        final = load_report(Path(self.report.manifest_path))
        self.assertTrue(all(r.included and len(r.destinations) == 1 for r in final.results))

    def test_space_with_table_focus_opens_preview_and_can_close_it(self):
        self.select_filtered("keep.png")
        QTest.keyClick(self.window.table, Qt.Key.Key_Space)
        self.wait_until(lambda: self.window.preview_button.isChecked())
        self.assertTrue(self.window.inspector.isVisible())
        self.wait_until(lambda: self.window.preview_image.pixmap() is not None and not self.window.preview_image.pixmap().isNull(), timeout=45.0)
        QTest.keyClick(self.window.table, Qt.Key.Key_Space)
        self.wait_until(lambda: not self.window.preview_button.isChecked())
        self.assertFalse(self.errors, self.errors)

    def test_escape_from_owned_dialog_hides_workspace_and_dialog_metadata_until_resume(self):
        dialog = QDialog(self.window)
        layout = QVBoxLayout(dialog)
        private_label = QLabel("private-file-name.png", dialog)
        private_edit = QLineEdit(str(self.source), dialog)
        layout.addWidget(private_label)
        layout.addWidget(private_edit)
        dialog.show()
        private_edit.setFocus()
        self.app.processEvents()
        self.assertTrue(private_label.isVisible())
        QTest.keyClick(private_edit, Qt.Key.Key_Escape)
        self.wait_until(lambda: self.window._privacy_active)
        self.assertIs(self.window.privacy_page, self.window.workspace_stack.currentWidget())
        self.assertFalse(self.window.table.isVisible())
        self.assertFalse(self.window.source_edit.isVisible())
        self.assertFalse(private_label.isVisible())
        self.assertFalse(private_edit.isVisible())
        self.window.resume_button.click()
        self.wait_until(lambda: not self.window._privacy_active)
        self.assertTrue(self.window.table.isVisible())
        self.assertTrue(private_label.isVisible())
        self.assertEqual(str(self.source), private_edit.text())
        dialog.close()

    def test_dialog_worker_cannot_reveal_new_metadata_while_workspace_is_hidden(self):
        dialog = QDialog(self.window)
        layout = QVBoxLayout(dialog)
        original = QLabel("original private metadata", dialog)
        always_hidden = QLabel("optional hidden metadata", dialog)
        layout.addWidget(original)
        layout.addWidget(always_hidden)
        always_hidden.hide()
        dialog.show()
        self.app.processEvents()
        self.window._set_privacy(True)
        # Mimics a running dialog worker adding its completed-result label.
        late_result = QLabel("new private file result.png", dialog)
        layout.addWidget(late_result)
        late_result.show()
        self.app.processEvents()
        self.assertFalse(original.isVisible())
        self.assertFalse(late_result.isVisible(), "Late worker results must stay behind the privacy mask")
        dialog.setWindowTitle("worker finished private-result.png")
        self.assertEqual("Love Sensation — Workspace hidden", dialog.windowTitle())
        self.window._set_privacy(False)
        self.assertTrue(original.isVisible())
        self.assertTrue(late_result.isVisible())
        self.assertFalse(always_hidden.isVisible())
        self.assertEqual("worker finished private-result.png", dialog.windowTitle())
        dialog.close()

    def test_new_owned_dialog_is_masked_before_its_first_event_loop_paint(self):
        self.window._set_privacy(True)
        dialog = QDialog(self.window)
        dialog.setWindowTitle("private source path")
        layout = QVBoxLayout(dialog)
        private_label = QLabel("private-media.png", dialog)
        layout.addWidget(private_label)
        dialog.show()
        # Check synchronously after show, before a queued timer can hide content.
        self.assertFalse(private_label.isVisible())
        self.assertNotEqual("private source path", dialog.windowTitle())
        resume = next(button for button in dialog.findChildren(QPushButton) if button.text() == "Resume workspace")
        self.assertTrue(resume.isVisible(), "The mask controls must remain usable")
        resume.click()
        self.assertFalse(self.window._privacy_active)
        self.assertTrue(private_label.isVisible())
        self.assertEqual("private source path", dialog.windowTitle())
        dialog.close()


if __name__ == "__main__":
    unittest.main()
