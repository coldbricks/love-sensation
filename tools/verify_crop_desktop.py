"""Run the actual Qt/GPU face-crop workflow on a temporary bundled sample."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from platinum_sorter import ui


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    evidence_path = ROOT / "reports" / "crop-desktop-verification.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence = {
        "passed": False,
        "scope": "Actual Qt worker, face dialog, and CUDA crop export; temporary copy of Ultralytics bundled zidane.jpg only.",
        "backend_mocks": False,
        "sample_source": "Ultralytics bundled assets/zidane.jpg",
        "checks": [],
    }
    app = QApplication.instance() or QApplication([])
    messages = []
    QMessageBox.warning = lambda *args, **kwargs: messages.append(str(args[-1]))
    QMessageBox.critical = lambda *args, **kwargs: messages.append(str(args[-1]))

    def check(condition, description):
        if not condition:
            raise AssertionError(description)
        evidence["checks"].append(description)

    def wait_until(predicate, timeout=120):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        if not predicate():
            raise TimeoutError("Desktop operation did not finish within its time limit.")
        if messages:
            raise AssertionError("Desktop messages: " + "; ".join(messages))

    window = None
    second_window = None
    try:
        with tempfile.TemporaryDirectory(prefix="platinum-crop-desktop-") as temporary:
            temporary_root = Path(temporary)
            ui.DATA_DIR = temporary_root / "data"
            ui.SETTINGS_PATH = ui.DATA_DIR / "settings.json"
            source, sorted_output = temporary_root / "source", temporary_root / "sorted"
            crop_output = temporary_root / "face-crops"
            source.mkdir()
            import ultralytics
            bundled_sample = Path(ultralytics.__file__).parent / "assets" / "zidane.jpg"
            photo = source / "zidane.jpg"
            shutil.copy2(bundled_sample, photo)
            original_hash = digest(photo)
            evidence["sample_sha256"] = original_hash

            window = ui.create_window()
            window.show()
            app.processEvents()
            window.source_edit.setText(str(source))
            window.destination_edit.setText(str(sorted_output))
            window.select_all_button.click()
            check(window.analyze_button.isEnabled(), "Selected categories and folders enable analysis")
            window.analyze_button.click()
            check(isinstance(window._worker, QThread) and window._busy, "Analysis uses the actual QThread worker")
            wait_until(lambda: not window._busy)
            report = window.report
            check(report is not None and report.analysis_complete, "Completed analysis returns a report")
            check(len(report.results) == 1 and not report.results[0].error, "Bundled sample analyzed without errors")
            check(report.device.get("cuda_verified") is True, "Analysis confirms CUDA execution")
            check(window.face_crops_button.isEnabled(), "Actual face detections enable the Face crops button")
            check(not sorted_output.exists(), "Analysis does not sort or copy images")
            evidence["analysis_device"] = dict(report.device)
            report_before = json.dumps(report.as_dict(), sort_keys=True)
            report_path = Path(report.manifest_path)
            manifest_before = digest(report_path)
            crop_state = {"phase": "initial_preview", "errors": []}
            deadline = time.monotonic() + 120
            timer = QTimer()
            timer.setInterval(15)

            def drive_dialog():
                dialog = window._face_crop_dialog
                if dialog is None:
                    return
                try:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Face crop modal did not complete within 120 seconds.")
                    if dialog._pending_error:
                        raise AssertionError(dialog._pending_error)
                    if dialog._worker is not None:
                        return
                    phase = crop_state["phase"]
                    if phase == "initial_preview":
                        if dialog._preview_pixmap.isNull():
                            return
                        check(bool(dialog.candidates), "Modal loads actual eligible face candidates")
                        check(all(candidate["class"] in {"FACE_FEMALE", "FACE_MALE"} for candidate in dialog.candidates), "Crop candidates contain only face categories")
                        check(dialog.table.rowCount() == len(dialog.candidates), "Candidate table matches the loaded face count")
                        preview = dialog._pending_result
                        check(preview["info"].get("cuda_verified") is True, "Initial preview confirms CUDA image processing")
                        check(preview["info"].get("crop_device", "").startswith("cuda"), "Initial crop tensor is on CUDA")
                        crop_state["initial_size"] = (preview["width"], preview["height"])
                        evidence["initial_preview"] = {
                            "width": preview["width"], "height": preview["height"],
                            "padding": 0.10, "info": dict(preview["info"]),
                        }
                        crop_state["count"] = len(dialog.candidates)
                        dialog.padding_spin.setValue(20)
                        check(dialog._preview_pixmap.isNull(), "Padding changes invalidate the previous preview")
                        dialog.preview_button.click()
                        check(isinstance(dialog._worker, QThread), "Preview uses a separate QThread worker")
                        crop_state["phase"] = "padded_preview"
                    elif phase == "padded_preview":
                        preview = dialog._pending_result
                        check(not dialog._preview_pixmap.isNull(), "Changed padding produces a fresh visible preview")
                        before = crop_state["initial_size"]
                        check(preview["width"] >= before[0] and preview["height"] >= before[1]
                              and (preview["width"], preview["height"]) != before,
                              "Twenty percent padding expands the actual face crop")
                        check(preview["info"].get("cuda_verified") is True, "Padded preview remains on CUDA")
                        evidence["padded_preview"] = {
                            "width": preview["width"], "height": preview["height"],
                            "padding": 0.20, "info": dict(preview["info"]),
                        }
                        dialog.destination_edit.setText(str(crop_output))
                        dialog.export_button.click()
                        check(isinstance(dialog._worker, QThread), "Export uses a separate QThread worker")
                        check(not dialog.table.isEnabled() and not dialog.padding_spin.isEnabled(), "Export disables controls while the worker is active")
                        crop_state["phase"] = "export"
                    elif phase == "export":
                        result = dialog.last_export
                        check(result is not None and not result["errors"] and not result["cancelled"], "Actual crop export completes without errors or cancellation")
                        check(result["exported"] == crop_state["count"] == len(result["outputs"]), "Export counters match every eligible face")
                        check(result["info"].get("cuda_verified") is True, "Export confirms CUDA execution")
                        check(result["info"].get("model_reloaded") is False, "Face export reuses saved detections without reloading the model")
                        check(dialog.open_button.isEnabled() and dialog.export_button.isEnabled(), "Completed export restores controls and enables Open output")
                        entries = result["entries"]
                        check(all(entry["status"] == "exported" for entry in entries), "Every manifest entry records a successful export")
                        files = []
                        from PIL import Image
                        for entry in entries:
                            path = Path(entry["output"])
                            check(path.is_file() and path.is_relative_to(crop_output), "Export file exists inside the chosen crop folder")
                            check(path.suffix == ".png" and digest(path) == entry["output_sha256"], "Crop PNG matches the manifest SHA-256")
                            with Image.open(path) as image:
                                check(image.format == "PNG" and image.size == (entry["width"], entry["height"]), "PNG dimensions match the exported face bounds")
                            files.append({"name": path.name, "width": entry["width"], "height": entry["height"], "sha256": entry["output_sha256"]})
                        saved = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
                        check(saved["exported"] == result["exported"] and saved["padding"] == 0.20, "Saved crop manifest preserves counts and chosen padding")
                        check(Path(result["journal_path"]).is_file(), "Export creates its independent audit journal")
                        evidence["eligible_faces"] = crop_state["count"]
                        evidence["exported"] = result["exported"]
                        evidence["export_device"] = dict(result["info"])
                        evidence["outputs"] = files
                        evidence["modal_status"] = dialog.status_label.text()
                        crop_state["phase"] = "done"
                        timer.stop()
                        dialog.close_button.click()
                except Exception:
                    crop_state["errors"].append(traceback.format_exc())
                    timer.stop()
                    dialog.reject()

            timer.timeout.connect(drive_dialog)
            timer.start()
            # This opens the real modal loop; the timer above drives real controls.
            window.face_crops_button.click()
            timer.stop()
            if crop_state["errors"]:
                raise AssertionError("\n".join(crop_state["errors"]))
            check(crop_state["phase"] == "done", "Face crop modal closes after the complete workflow")
            check(digest(photo) == original_hash and list(source.iterdir()) == [photo], "Original sample bytes and source folder are unchanged")
            check(json.dumps(report.as_dict(), sort_keys=True) == report_before, "Face cropping leaves the sorting report object unchanged")
            check(digest(report_path) == manifest_before, "Face cropping leaves the saved sorting manifest unchanged")
            check(not sorted_output.exists(), "Face export does not execute the sorting plan")

            window.deselect_all_button.click()
            check(not any(box.isChecked() for box in window._categories.values()), "Deselect all clears every category")
            check(not window.analyze_button.isEnabled(), "Zero selected categories disable Analyze")
            check(not window.face_crops_button.isEnabled() and not window.apply_button.isEnabled(), "Changing category selection invalidates crop and sorting actions")
            window._save_settings()
            second_window = ui.create_window()
            check(not any(box.isChecked() for box in second_window._categories.values()), "Deselect all persists across a fresh desktop window")
            check(not second_window.analyze_button.isEnabled(), "Restored empty selection keeps Analyze disabled")
            second_window.select_all_button.click()
            check(all(box.isChecked() for box in second_window._categories.values()), "Select all restores every category")
            check(second_window.analyze_button.isEnabled(), "Select all re-enables Analyze with valid folders")
            second_window.close()
            second_window = None
            window.close()
            window = None
            app.processEvents()
            check(not messages, "No desktop warnings or errors occurred")
            evidence.update(passed=True, originals_unchanged=True, sorting_report_unchanged=True,
                            actual_qthread_and_gpu=True, isolated_settings=True, errors=[])
    except Exception:
        evidence["error"] = traceback.format_exc()
        raise
    finally:
        for remaining in (second_window, window):
            if remaining is not None:
                remaining.close()
        app.processEvents()
        evidence["checks_passed"] = len(evidence["checks"])
        evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(f"Crop desktop integration passed: {evidence['exported']} CUDA face crops; {evidence['checks_passed']} checks.")
    print(evidence_path)


if __name__ == "__main__":
    main()
