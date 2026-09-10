"""Offscreen desktop integration test; all filing uses temporary sample copies."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog
from platinum_sorter import ui


def main():
    app = QApplication.instance() or QApplication([])
    errors = []
    QMessageBox.critical = lambda *arguments: errors.append(str(arguments[-1]))
    QMessageBox.warning = lambda *arguments: errors.append(str(arguments[-1]))
    with tempfile.TemporaryDirectory(prefix="platinum-desktop-") as temporary:
        root = Path(temporary)
        ui.DATA_DIR, ui.SETTINGS_PATH = root / "data", root / "data" / "settings.json"
        source, output = root / "source", root / "output"
        source.mkdir()
        import ultralytics
        assets = Path(ultralytics.__file__).parent / "assets"
        for name in ("zidane.jpg", "bus.jpg"):
            shutil.copy2(assets / name, source / name)
        shutil.copy2(assets / "zidane.jpg", source / "portrait_\u00e9.JPG")
        original = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()}
        window = ui.create_window()
        window.show()
        app.processEvents()
        preview = ROOT / "reports" / "desktop-ready.png"
        window.grab().save(str(preview))
        window.source_edit.setText(str(source))
        window.destination_edit.setText(str(output))

        def wait_task():
            deadline = time.monotonic() + 90
            while window._busy and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.01)
            app.processEvents()
            assert not window._busy, "Desktop worker did not finish within 90 seconds"
            assert not errors, errors

        window.analyze_button.click()
        wait_task()
        assert window.report and len(window.report.results) == 3
        assert window.report.device["cuda_verified"]
        assert all(result.status == "ready" for result in window.report.results), window.report.as_dict()
        assert not output.exists(), "Analysis unexpectedly created output images"
        assert window.apply_button.isEnabled(), "Reviewed plan cannot be applied"
        window.apply_button.click()
        wait_task()
        assert all(result.status == "copied" for result in window.report.results), window.report.as_dict()
        for result in window.report.results:
            assert result.destinations
            for destination in result.destinations:
                assert hashlib.sha256(Path(destination).read_bytes()).hexdigest() == original[Path(result.source).name]
        assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in source.iterdir()} == original
        first_copies = sum(len(result.destinations) for result in window.report.results)
        window.analyze_button.click()
        wait_task()
        assert all(result.cached for result in window.report.results), "Second run did not use detection cache"
        assert window.apply_button.isEnabled()
        saved = root / "saved-review.json"
        from platinum_sorter.engine import export_report
        export_report(window.report, saved)
        QFileDialog.getOpenFileName = lambda *args, **kwargs: (str(saved), "JSON report (*.json)")
        window.open_run_button.click()
        wait_task()
        assert window.report.analysis_complete
        assert window.report.phase == "analyze"
        assert window.apply_button.isEnabled(), "Reopened reviewed plan cannot be applied"
        window.confidence_spin.setValue(0.70)
        assert not window.apply_button.isEnabled(), "Changed settings did not invalidate the plan"
        window.close()
        app.processEvents()
        (ROOT / "reports" / "desktop-verification.json").write_text(json.dumps({
            "passed": True, "source_images": 3, "verified_copies": first_copies,
            "originals_unchanged": True, "cache_reused": True, "plan_invalidation": True,
            "actual_qthread_and_gpu": True, "saved_run_reopened": True, "unicode_jpeg": True, "errors": errors,
        }, indent=2), encoding="utf-8")
        print(f"Desktop integration passed: {first_copies} verified copies, cache reused, originals unchanged.")


if __name__ == "__main__":
    main()
