"""Engine-level analysis regressions with tiny synthetic frames and no model."""
from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from platinum_sorter.contracts import DetectionResult, SortOptions
from platinum_sorter.engine import SorterEngine, load_report


def encoded_image(width=640, height=640, orientation=None):
    stream = io.BytesIO()
    image = Image.new("RGB", (width, height), (48, 64, 80))
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
    image.save(stream, format="JPEG", exif=exif)
    return stream.getvalue()


def detection(label="FACE", box=(0, 0, 160, 160), score=0.9):
    return {"class": label, "score": score, "box": list(box)}


class FrameDetector:
    fingerprint = "synthetic-dimensions-v1"
    info = {"device": "synthetic"}

    def __init__(self):
        self.calls = []
        self.outputs = None
        self.after_detect = None
        self.with_dimensions = True

    def detect_encoded_batch(self, contents):
        self.calls.append(list(contents))
        results = []
        for index, content in enumerate(contents):
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if image.getexif().get(274, 1) in {5, 6, 7, 8}:
                    width, height = height, width
            value = self.outputs[index] if self.outputs is not None else [
                detection(box=(0, 0, width / 4, height / 4))]
            if isinstance(value, Exception):
                results.append(value)
            elif self.with_dimensions:
                results.append(DetectionResult(value, width=width, height=height))
            else:
                results.append(value)
        if self.after_detect:
            self.after_detect()
        return results


class VideoAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="love-analysis-test-")
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "input"
        self.source.mkdir()
        self.destination = self.root / "output"
        self.detector = FrameDetector()
        self.engine = SorterEngine(self.root / "data", lambda: self.detector)
        self.cancel = threading.Event()
        self.events = []
        self.frames = [(0.0, encoded_image()), (5.0, encoded_image())]
        self.probe_patch = patch("platinum_sorter.video_engine.probe_media_file", return_value={
            "width": 640, "height": 640, "duration_s": 10.0, "fps": 30.0, "frame_count": 300})
        self.sampler_patch = patch("platinum_sorter.video_engine.sample_video_frames", side_effect=lambda *a, **k: self.frames)
        self.probe = self.probe_patch.start()
        self.sampler = self.sampler_patch.start()

    def tearDown(self):
        self.sampler_patch.stop()
        self.probe_patch.stop()
        self.tmp.cleanup()

    def analyze(self, **changes):
        options = SortOptions(str(self.source), str(self.destination), **changes)
        return self.engine.analyze(options, self.events.append, self.cancel)

    def video(self):
        path = self.source / "synthetic.mp4"
        path.write_bytes(b"synthetic video source, decoding mocked")
        return path

    def test_still_prominence_uses_source_dimensions_and_cached_dimensions(self):
        (self.source / "small.jpg").write_bytes(encoded_image(640, 640))
        (self.source / "large.jpg").write_bytes(encoded_image(2560, 2560))
        report = self.analyze()
        self.assertEqual([0.225, 0.225], [r.prominence for r in report.results])
        self.assertEqual([(2560, 2560), (640, 640)], [(r.width, r.height) for r in report.results])
        again = self.analyze()
        self.assertTrue(all(r.cached for r in again.results))
        self.assertEqual([0.225, 0.225], [r.prominence for r in again.results])
        self.assertEqual(1, len(self.detector.calls))

    def test_legacy_detector_header_fallback_uses_exif_oriented_dimensions(self):
        self.detector.with_dimensions = False
        (self.source / "rotated.jpg").write_bytes(encoded_image(64, 32, orientation=6))
        result = self.analyze().results[0]
        self.assertEqual((32, 64), (result.width, result.height))
        self.assertEqual(0.225, result.prominence)

    def test_video_uses_each_decoded_frame_size(self):
        self.video()
        self.frames = [(0.0, encoded_image(640, 640)), (5.0, encoded_image(2560, 2560))]
        result = self.analyze().results[0]
        self.assertEqual(0.225, result.prominence)
        self.assertEqual(0.225, result.sustained_wow)
        self.assertEqual([0.225, 0.225], [d["prominence"] for d in result.detections])
        self.assertEqual(2, result.sampled_frames)
        self.assertEqual([0.0, 5.0], result.sampled_timestamps_s)

    def test_cached_video_recomputes_selected_class_and_threshold_metrics(self):
        self.video()
        self.detector.outputs = [[detection("FACE", (0, 0, 64, 64), 0.95),
                                  detection("HAND", (0, 0, 320, 320), 0.9)]] * 2
        first = self.analyze(selected_classes=["FACE"]).results[0]
        second = self.analyze(selected_classes=["HAND"]).results[0]
        third = self.analyze(selected_classes=["HAND"], threshold=0.95).results[0]
        self.assertEqual(0.095, first.prominence)
        self.assertEqual(0.45, second.prominence)
        self.assertEqual(0.0, third.prominence)
        self.assertEqual(["HAND"], second.categories)
        self.assertEqual(["_Unmatched"], third.categories)
        self.assertTrue(second.cached and third.cached)
        self.assertEqual(1, len(self.detector.calls))
        self.assertEqual(1, self.sampler.call_count)

    def test_changed_sampling_rate_invalidates_video_cache(self):
        self.video()
        self.analyze(video_sample_fps=2.0)
        again = self.analyze(video_sample_fps=4.0).results[0]
        self.assertFalse(again.cached)
        self.assertEqual(2, self.sampler.call_count)
        self.assertEqual(4.0, self.sampler.call_args.kwargs["sample_fps"])

    def test_video_metrics_survive_cleaning_and_filter_correctly(self):
        self.video()
        self.detector.outputs = [[detection(box=(0, 0, 128, 64))]] * 2
        aspect = self.analyze(min_aspect_ratio=1.5).results[0]
        prominence = self.analyze(min_prominence=0.2).results[0]
        self.assertEqual(["FACE"], aspect.categories)
        self.assertEqual(2.0, aspect.detections[0]["aspect"])
        self.assertEqual(["_Unmatched"], prominence.categories)
        self.assertEqual(0.1273, prominence.detections[0]["prominence"])

    def test_zero_decoded_frames_is_error_and_never_filed(self):
        source = self.video()
        self.frames = []
        report = self.analyze()
        result = report.results[0]
        self.assertEqual("error", result.status)
        self.assertEqual([], result.categories)
        self.assertIn("No video frames", result.error)
        self.engine.execute(report, self.events.append, self.cancel)
        self.assertTrue(source.exists())
        self.assertFalse(self.destination.exists())

    def test_all_detection_failures_are_errors_not_cached_no_matches(self):
        self.video()
        self.detector.outputs = [RuntimeError("decode failed"), RuntimeError("GPU exhausted")]
        first = self.analyze().results[0]
        self.assertEqual("error", first.status)
        self.assertEqual(2, first.failed_frames)
        self.assertEqual([], first.categories)
        self.detector.outputs = None
        second = self.analyze().results[0]
        self.assertFalse(second.cached)
        self.assertEqual("ready", second.status)
        self.assertEqual(2, len(self.detector.calls))

    def test_partial_analysis_retains_review_data_but_does_not_file_or_cache(self):
        source = self.video()
        self.detector.outputs = [[detection()], RuntimeError("one frame failed")]
        report = self.analyze(operation="move")
        result = report.results[0]
        self.assertEqual("partial", result.status)
        self.assertEqual((1, 1), (result.sampled_frames, result.failed_frames))
        self.assertEqual(["FACE"], result.categories)
        self.engine.execute(report, self.events.append, self.cancel)
        self.assertEqual("partial", result.status)
        self.assertTrue(source.exists())
        self.assertFalse(self.destination.exists())
        self.detector.outputs = None
        again = self.analyze().results[0]
        self.assertFalse(again.cached)

    def test_successful_empty_detections_are_distinct_from_failures(self):
        self.video()
        self.detector.outputs = [[], []]
        result = self.analyze().results[0]
        self.assertEqual("ready", result.status)
        self.assertEqual(["_Unmatched"], result.categories)
        self.assertEqual((2, 0), (result.sampled_frames, result.failed_frames))

    def test_video_changed_during_detection_is_rejected(self):
        source = self.video()
        self.detector.after_detect = lambda: source.write_bytes(b"changed video content")
        result = self.analyze().results[0]
        self.assertEqual("error", result.status)
        self.assertIn("changed during analysis", result.error)
        self.assertEqual([], result.categories)

    def test_video_cancellation_is_not_cached_or_analysis_complete(self):
        source = self.video()
        self.sampler.side_effect = InterruptedError("cancelled sampling")
        report = self.analyze()
        self.assertTrue(report.cancelled)
        self.assertFalse(report.analysis_complete)
        self.assertEqual("cancelled", report.results[0].status)
        self.assertEqual([], report.results[0].categories)
        self.assertTrue(source.exists())
        self.assertEqual([], self.detector.calls)

    def test_review_exclusion_and_category_correction_survive_wal_reopen(self):
        (self.source / "one.jpg").write_bytes(encoded_image())
        (self.source / "two.jpg").write_bytes(encoded_image())
        report = self.analyze(operation="move")
        first, second = report.results
        first.included = False
        second.original_categories = list(second.categories)
        second.categories = ["HAND"]
        # A persisted snapshot must supersede existing WAL entries, not replay
        # them over the newer review decisions on the next launch.
        sequence = report._journal_sequence
        self.engine.save_review(report)
        restored = load_report(Path(report.manifest_path))
        self.assertEqual(sequence, restored._journal_sequence)
        self.assertFalse(restored.results[0].included)
        self.assertEqual(["FACE"], restored.results[1].original_categories)
        self.assertEqual(["HAND"], restored.results[1].categories)
        self.assertEqual(first.detections, restored.results[0].detections)
        self.engine.execute(restored, self.events.append, self.cancel)
        self.assertEqual("ready", restored.results[0].status)
        self.assertTrue((self.source / "one.jpg").exists())
        self.assertFalse((self.source / "two.jpg").exists())
        self.assertFalse((self.destination / "FACE" / "one.jpg").exists())
        self.assertTrue((self.destination / "HAND" / "two.jpg").exists())
        restored.results[0].included = True
        self.engine.save_review(restored)
        resumed = load_report(Path(restored.manifest_path))
        self.engine.execute(resumed, self.events.append, self.cancel)
        self.assertEqual("moved", resumed.results[0].status)
        self.assertFalse((self.source / "one.jpg").exists())

    def test_legacy_report_defaults_to_included(self):
        (self.source / "image.jpg").write_bytes(encoded_image())
        report = self.analyze()
        manifest = Path(report.manifest_path)
        doc = json.loads(manifest.read_text())
        for result in doc["results"]:
            for field in ("included", "original_categories", "width", "height", "sampled_frames",
                          "failed_frames", "sampled_timestamps_s"):
                result.pop(field)
        manifest.write_text(json.dumps(doc))
        restored = load_report(manifest)
        self.assertTrue(restored.results[0].included)
        self.assertIsNone(restored.results[0].original_categories)


if __name__ == "__main__":
    unittest.main()
