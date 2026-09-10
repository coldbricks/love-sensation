"""File-safety tests use synthetic bytes and a fake detector only."""
from __future__ import annotations

import csv
import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from platinum_sorter import engine as module
from platinum_sorter.contracts import SortOptions
from platinum_sorter.engine import SorterEngine, export_report, load_report


def detection(label="HAND", score=0.9):
    return {"class": label, "score": score, "box": [1, 2, 3, 4]}


class FakeDetector:
    def __init__(self, outputs=None, fingerprint="fake-detector-v1"):
        self.outputs = outputs or {}
        self.fingerprint = fingerprint
        self.info = {"provider": "synthetic-test", "gpu": False}
        self.calls = []

    def detect_batch(self, paths):
        self.calls.append(list(paths))
        return [self.outputs.get(path.name, [detection()]) for path in paths]


class SimulatedCrash(BaseException):
    """Bypass normal operation error handling to exercise durable recovery."""


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="platinum-engine-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "input"
        self.source.mkdir()
        self.destination = self.root / "output"
        self.detector = FakeDetector()
        self.engine = SorterEngine(self.root / "data", lambda: self.detector)
        self.cancel = threading.Event()
        self.events = []

    def tearDown(self):
        self.temporary.cleanup()

    def image(self, name="sample.jpg", contents=b"synthetic image bytes"):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def options(self, **changes):
        return SortOptions(str(self.source), str(self.destination), **changes)

    def analyze(self, **changes):
        return self.engine.analyze(self.options(**changes), self.events.append, self.cancel)

    def apply(self, report):
        return self.engine.execute(report, self.events.append, self.cancel)

    def test_analysis_is_read_only_and_manifest_is_written(self):
        original = self.image("UPPER.JPEG")
        self.image("other.TIFF", b"second image")
        (self.source / "ignored.txt").write_text("not an image")
        before = original.stat()
        report = self.analyze()
        self.assertEqual(2, len(report.results))
        self.assertEqual(before.st_mtime_ns, original.stat().st_mtime_ns)
        self.assertEqual(b"synthetic image bytes", original.read_bytes())
        self.assertFalse(self.destination.exists())
        self.assertTrue(all(len(result.sha256) == 64 for result in report.results))
        journal = json.loads(Path(report.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(report.run_id, journal["run_id"])
        self.assertTrue(any(event["type"] == "device" for event in self.events))

    def test_unique_labels_modes_threshold_and_selected_classes(self):
        self.image()
        self.detector.outputs["sample.jpg"] = [
            detection("HAND", 0.8), detection("HAND", 0.99),
            detection("FACE", 0.9), detection("FOOT", 0.7),
            detection("ELBOW", 0.62), detection("LOW", 0.61),
        ]
        all_result = self.analyze(mode="all").results[0]
        self.assertEqual(["HAND", "FACE", "FOOT", "ELBOW"], all_result.categories)
        self.assertEqual(["HAND", "FACE", "FOOT"], self.analyze(mode="top3").results[0].categories)
        self.assertEqual(["HAND"], self.analyze(mode="best").results[0].categories)
        self.assertEqual(["ELBOW"], self.analyze(selected_classes=["ELBOW"]).results[0].categories)
        self.assertEqual(1, len(self.detector.calls), "threshold/mode/class changes must reuse raw cached detections")

    def test_unmatched_or_skip(self):
        self.image()
        self.detector.outputs["sample.jpg"] = []
        self.assertEqual(["_Unmatched"], self.analyze().results[0].categories)
        report = self.analyze(include_unmatched=False)
        self.assertEqual("skipped", report.results[0].status)
        self.apply(report)
        self.assertFalse(self.destination.exists())

    def test_source_output_overlap_and_missing_source_are_rejected(self):
        self.image()
        invalid = [self.source, self.source / "nested", self.root]
        for output in invalid:
            with self.subTest(output=str(output)):
                with self.assertRaises(ValueError):
                    self.engine.analyze(SortOptions(str(self.source), str(output)), self.events.append, self.cancel)
        missing = self.root / "mistyped-source"
        with self.assertRaises((FileNotFoundError, ValueError)):
            self.engine.analyze(SortOptions(str(missing), str(self.destination)), self.events.append, self.cancel)
        self.assertFalse(missing.exists())
        self.assertFalse((self.source / "nested").exists())

    def test_relative_paths_and_existing_files_never_overwrite(self):
        first = self.image("one/same.jpg", b"first")
        second = self.image("two/same.jpg", b"second")
        existing = self.destination / "HAND" / "one" / "same.jpg"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"existing output")
        report = self.apply(self.analyze())
        self.assertEqual(b"existing output", existing.read_bytes())
        self.assertEqual(b"first", (existing.parent / "same__2.jpg").read_bytes())
        self.assertEqual(b"second", (self.destination / "HAND" / "two" / "same.jpg").read_bytes())
        self.assertTrue(first.exists() and second.exists())
        self.assertTrue(all(result.status == "copied" for result in report.results))
        self.apply(self.analyze())
        self.assertEqual(b"first", (existing.parent / "same__3.jpg").read_bytes())

    def test_concurrent_exclusive_reservations_are_unique(self):
        desired = self.destination / "HAND" / "same.jpg"

        def create(index):
            path, descriptor = module._reserve_destination(desired)
            with os.fdopen(descriptor, "wb") as output:
                output.write(str(index).encode())
            return path

        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(create, range(24)))
        self.assertEqual(24, len(set(paths)))
        self.assertEqual(set(map(str, range(24))), {path.read_text() for path in paths})

    def test_move_verifies_all_categories_before_deleting_original(self):
        original = self.image()
        self.detector.outputs["sample.jpg"] = [detection("HAND"), detection("FACE", 0.8)]
        report = self.analyze(operation="move", mode="all")
        original_delete = module._delete_verified_source
        deletions = []

        def checked_delete(path, expected, cancel):
            self.assertTrue(original.exists())
            result = report.results[0]
            self.assertEqual(2, len(result.output_details))
            self.assertTrue(all(detail["status"] == "verified" for detail in result.output_details))
            self.assertTrue(all(Path(name).read_bytes() == b"synthetic image bytes" for name in result.destinations))
            deletions.append(path)
            return original_delete(path, expected, cancel)

        with patch.object(module, "_delete_verified_source", side_effect=checked_delete):
            self.apply(report)
        self.assertEqual([original], deletions)
        self.assertFalse(original.exists())
        self.assertEqual("moved", report.results[0].status)
        self.apply(report)
        self.assertEqual(2, len(report.results[0].destinations), "resume must skip an already-applied move")

    def test_partial_multicategory_failure_preserves_original_and_resumes(self):
        original = self.image()
        self.detector.outputs["sample.jpg"] = [detection("HAND"), detection("FACE", 0.8)]
        report = self.analyze(operation="move", mode="all")
        real_copy = self.engine._copy_source
        calls = 0

        def fail_second(source, target, cancel):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write(b"partial")
                raise OSError("simulated disk-full on second category")
            return real_copy(source, target, cancel)

        with patch.object(self.engine, "_copy_source", side_effect=fail_second):
            self.apply(report)
        result = report.results[0]
        self.assertTrue(original.exists())
        self.assertEqual("error", result.status)
        self.assertIn("disk-full", result.error)
        self.assertEqual(["verified", "error"], [detail["status"] for detail in result.output_details])
        self.assertEqual(2, len(result.destinations))
        journal = json.loads(Path(report.manifest_path).read_text())
        self.assertIn("disk-full", journal["results"][0]["output_details"][1]["error"])
        first_verified = result.destinations[0]
        self.apply(report)
        self.assertFalse(original.exists())
        self.assertEqual("moved", result.status)
        self.assertEqual(3, len(result.destinations), "resume reuses verified output and retains the recorded partial")
        self.assertEqual(first_verified, result.destinations[0])
        self.assertEqual(b"partial", Path(result.destinations[1]).read_bytes())
        self.assertEqual(b"synthetic image bytes", Path(result.destinations[2]).read_bytes())

    def test_modified_source_is_rejected_even_with_same_size_and_mtime(self):
        original = self.image(contents=b"before")
        report = self.analyze(operation="move")
        before = original.stat()
        original.write_bytes(b"after!")
        os.utime(original, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.apply(report)
        self.assertEqual("error", report.results[0].status)
        self.assertIn("changed since analysis", report.results[0].error)
        self.assertEqual(b"after!", original.read_bytes())
        self.assertFalse(self.destination.exists())

    def test_source_change_during_copy_cannot_be_deleted(self):
        original = self.image(contents=b"before")
        report = self.analyze(operation="move")
        real_copy = self.engine._copy_source

        def change_after_copy(source, target, cancel):
            real_copy(source, target, cancel)
            before = source.stat()
            source.write_bytes(b"after!")
            os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))

        with patch.object(self.engine, "_copy_source", side_effect=change_after_copy):
            self.apply(report)
        self.assertEqual("error", report.results[0].status)
        self.assertIn("changed during copying", report.results[0].error)
        self.assertEqual(b"after!", original.read_bytes())
        self.assertEqual(b"before", Path(report.results[0].destinations[0]).read_bytes())

    def test_cache_reuses_raw_detections_and_invalidates_content_or_model(self):
        original = self.image(contents=b"before")
        self.analyze()
        cached = self.analyze(threshold=0.95)
        self.assertTrue(cached.results[0].cached)
        self.assertEqual(["_Unmatched"], cached.results[0].categories)
        self.assertEqual(1, len(self.detector.calls))
        before = original.stat()
        original.write_bytes(b"after!")
        os.utime(original, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertFalse(self.analyze().results[0].cached)
        self.assertEqual(2, len(self.detector.calls))
        original.write_bytes(b"new image with different size")
        self.assertFalse(self.analyze().results[0].cached)
        self.assertEqual(3, len(self.detector.calls))
        next_model = FakeDetector(fingerprint="fake-detector-v2")
        next_engine = SorterEngine(self.root / "data", lambda: next_model)
        new_report = next_engine.analyze(self.options(), self.events.append, self.cancel)
        self.assertFalse(new_report.results[0].cached)
        self.assertEqual(1, len(next_model.calls))

    def test_corrupt_image_error_does_not_stop_other_images(self):
        self.image("bad.jpg")
        good = self.image("good.png")
        self.detector.outputs["bad.jpg"] = ValueError("cannot decode synthetic image")
        report = self.analyze()
        self.assertEqual(["error", "ready"], [result.status for result in report.results])
        self.apply(report)
        self.assertTrue(good.exists())
        self.assertEqual("copied", report.results[1].status)
        self.assertIn("cannot decode", report.results[0].error)

    def test_batch_failure_isolated_to_individual_image(self):
        self.image("bad.jpg")
        self.image("good.jpg")
        original_batch = self.detector.detect_batch

        def raises_for_bad(paths):
            if any(path.name == "bad.jpg" for path in paths):
                raise ValueError("bad decode")
            return original_batch(paths)

        with patch.object(self.detector, "detect_batch", side_effect=raises_for_bad):
            report = self.analyze()
        self.assertEqual(["error", "ready"], [result.status for result in report.results])

    def test_cancel_before_analysis_does_not_load_detector(self):
        self.image()
        self.cancel.set()
        with patch.object(self.engine, "detector_factory") as factory:
            report = self.analyze()
        factory.assert_not_called()
        self.assertTrue(report.cancelled)
        self.assertEqual([], report.results)
        self.assertFalse(self.destination.exists())

    def test_cancel_during_move_preserves_original_and_journals_partial(self):
        original = self.image()
        report = self.analyze(operation="move")
        real_copy = self.engine._copy_source

        def cancel_after_copy(source, target, cancel):
            real_copy(source, target, cancel)
            cancel.set()

        with patch.object(self.engine, "_copy_source", side_effect=cancel_after_copy):
            self.apply(report)
        self.assertTrue(report.cancelled)
        self.assertTrue(original.exists())
        self.assertEqual("cancelled", report.results[0].status)
        journal = json.loads(Path(report.manifest_path).read_text())
        self.assertTrue(journal["cancelled"])
        self.assertEqual("partial", journal["results"][0]["output_details"][0]["status"])
        self.cancel.clear()
        self.apply(report)
        self.assertEqual("moved", report.results[0].status)
        self.assertFalse(original.exists())

    def test_cancel_between_results_resumes_without_duplicate_completed_copy(self):
        self.image("one.jpg")
        self.image("two.jpg")
        report = self.analyze()

        def stop_after_one(event):
            if event["type"] == "result" and event["result"].status == "copied":
                self.cancel.set()

        self.engine.execute(report, stop_after_one, self.cancel)
        self.assertTrue(report.cancelled)
        self.assertEqual(["copied", "ready"], [result.status for result in report.results])
        first_destination = report.results[0].destinations[:]
        self.cancel.clear()
        self.apply(report)
        self.assertEqual(["copied", "copied"], [result.status for result in report.results])
        self.assertEqual(first_destination, report.results[0].destinations)

    def test_wal_recovers_partial_output_after_process_crash(self):
        original = self.image()
        report = self.analyze(operation="move")

        def crash_during_copy(source, target, cancel):
            target.write(b"incomplete")
            target.flush()
            raise SimulatedCrash("process interrupted while writing output")

        with patch.object(self.engine, "_copy_source", side_effect=crash_during_copy):
            with self.assertRaises(SimulatedCrash):
                self.apply(report)
        # Full snapshot intentionally remains at the start of the apply stage;
        # newer reserved output state exists only in the append-only WAL.
        snapshot = json.loads(Path(report.manifest_path).read_text())
        self.assertEqual([], snapshot["results"][0]["destinations"])
        recovered = load_report(Path(report.manifest_path))
        self.assertEqual("reserved", recovered.results[0].output_details[0]["status"])
        partial_path = Path(recovered.results[0].destinations[0])
        self.assertEqual(b"incomplete", partial_path.read_bytes())
        self.assertTrue(original.exists())
        self.apply(recovered)
        self.assertEqual("moved", recovered.results[0].status)
        self.assertEqual(2, len(recovered.results[0].destinations))
        self.assertEqual(b"incomplete", partial_path.read_bytes())
        self.assertFalse(original.exists())

    def test_wal_recovers_move_completed_just_before_process_crash(self):
        original = self.image()
        report = self.analyze(operation="move")
        real_delete = module._delete_verified_source

        def crash_after_delete(path, expected, cancel):
            real_delete(path, expected, cancel)
            raise SimulatedCrash("source deletion completed, result not yet appended")

        with patch.object(module, "_delete_verified_source", side_effect=crash_after_delete):
            with self.assertRaises(SimulatedCrash):
                self.apply(report)
        self.assertFalse(original.exists())
        recovered = load_report(Path(report.manifest_path))
        self.assertEqual("removing_source", recovered.results[0].status)
        destinations = recovered.results[0].destinations[:]
        self.apply(recovered)
        self.assertEqual("moved", recovered.results[0].status)
        self.assertEqual("", recovered.results[0].error)
        self.assertEqual(destinations, recovered.results[0].destinations)

    def test_wal_truncated_final_append_is_ignored_and_can_resume(self):
        self.image()
        report = self.analyze()
        wal = Path(report.manifest_path).with_suffix(".wal.jsonl")
        with wal.open("ab") as file:
            file.write(b'{"sequence": 123, "resu')
        recovered = load_report(Path(report.manifest_path))
        self.assertEqual("ready", recovered.results[0].status)
        self.apply(recovered)
        self.assertEqual("copied", recovered.results[0].status)
        self.assertEqual("copied", load_report(Path(report.manifest_path)).results[0].status)

    def test_final_snapshot_is_not_reverted_by_older_wal_entries(self):
        self.image()
        report = self.apply(self.analyze())
        restored = load_report(Path(report.manifest_path))
        self.assertEqual("copied", restored.results[0].status)
        self.assertEqual(report.elapsed_seconds, restored.elapsed_seconds)
        self.assertEqual(report.results[0].output_details, restored.results[0].output_details)

    def test_report_phase_and_completed_analysis_survive_reopen(self):
        self.image()
        report = self.analyze()
        self.assertTrue(report.analysis_complete)
        self.assertEqual("analyze", report.phase)
        self.cancel.set()
        self.apply(report)
        restored = load_report(Path(report.manifest_path))
        self.assertTrue(restored.analysis_complete)
        self.assertEqual("execute", restored.phase)
        self.assertTrue(restored.cancelled)
        self.cancel.clear()
        self.apply(restored)
        self.assertEqual("copied", restored.results[0].status)

    def test_unfinished_analysis_cannot_be_applied(self):
        original = self.image()
        report = self.analyze()
        report.analysis_complete = False
        with self.assertRaisesRegex(ValueError, "did not finish"):
            self.apply(report)
        self.assertTrue(original.exists())
        self.assertFalse(self.destination.exists())

    @unittest.skipUnless(os.name == "nt", "Windows share-mode safety test")
    def test_verified_outputs_are_locked_until_source_removal(self):
        original = self.image()
        report = self.analyze(operation="move")
        real_delete = module._delete_verified_source

        def checked_delete(path, expected, cancel):
            output = Path(report.results[0].destinations[0])
            with self.assertRaises(OSError):
                output.write_bytes(b"unexpected modification")
            return real_delete(path, expected, cancel)

        with patch.object(module, "_delete_verified_source", side_effect=checked_delete):
            self.apply(report)
        self.assertEqual("moved", report.results[0].status)
        self.assertFalse(original.exists())

    def test_symlink_files_and_folders_are_not_scanned(self):
        self.image("regular.jpg")
        external = self.root / "external"
        external.mkdir()
        (external / "hidden.jpg").write_bytes(b"outside source")
        try:
            (self.source / "linked.jpg").symlink_to(external / "hidden.jpg")
            (self.source / "linked-folder").symlink_to(external, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"Symlink creation unavailable: {error}")
        report = self.analyze()
        self.assertEqual(["regular.jpg"], [result.relative_path for result in report.results])

    def test_export_json_and_csv_preserve_review_details(self):
        self.image()
        report = self.apply(self.analyze())
        json_path = self.root / "exports" / "review.json"
        csv_path = self.root / "exports" / "review.csv"
        export_report(report, json_path)
        export_report(report, csv_path)
        exported = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(report.results[0].sha256, exported["results"][0]["sha256"])
        with csv_path.open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual("copied", rows[0]["status"])
        self.assertEqual("verified", json.loads(rows[0]["output_details"])[0]["status"])
        with self.assertRaises(ValueError):
            export_report(report, self.root / "bad.txt")


if __name__ == "__main__":
    unittest.main()
