"""Semantic evidence regressions use fake models and synthetic bytes only."""
from __future__ import annotations

import csv
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from platinum_sorter.contracts import DetectionResult, ImageResult, SortOptions
from platinum_sorter.engine import SorterEngine, _clean_detections, _select_categories, export_report, load_report
from platinum_sorter.evidence import accepts_detection, evidence_sort_key, is_accepted_semantic
from platinum_sorter.metrics import evaluate_frame_detections, ken_burns_focal_point


def semantic(margin=2.5):
    return {"class": "BUTTOCKS_COVERED", "box": [], "source": "siglip2",
            "scope": "image", "score_kind": "logit_margin", "raw_margin": margin,
            "positive_logit": -5.0 + margin, "negative_logit": -5.0,
            "model_revision": "synthetic-model-v1", "prompt_revision": "synthetic-prompts-v1"}


def localized(score=0.9):
    return {"class": "FACE", "score": score, "box": [0, 0, 160, 160]}


class FakeSemanticDetector:
    info = {"provider": "synthetic-test"}

    def __init__(self):
        self.fingerprint = "nudenet-v1:siglip-model-v1:prompts-v1:preprocess-v1"
        self.outputs = [[semantic()]]
        self.calls = 0

    def detect_encoded_batch(self, contents):
        self.calls += 1
        return [value if isinstance(value, Exception) else DetectionResult(value, width=640, height=640)
                for value in self.outputs[:len(contents)]]


class SemanticEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="love-semantic-test-")
        self.root = Path(self.temporary.name).resolve()
        self.source, self.destination = self.root / "input", self.root / "output"
        self.source.mkdir()
        self.detector = FakeSemanticDetector()
        self.engine = SorterEngine(self.root / "data", lambda: self.detector)
        self.cancel = threading.Event()

    def tearDown(self):
        self.temporary.cleanup()

    def options(self, **changes):
        return SortOptions(str(self.source), str(self.destination), **changes)

    def analyze(self, **changes):
        return self.engine.analyze(self.options(**changes), lambda event: None, self.cancel)

    def source_file(self, extension="jpg"):
        path = self.source / f"synthetic.{extension}"
        path.write_bytes(b"synthetic source bytes; fake model only")
        return path

    def video_analysis(self, **changes):
        with patch("platinum_sorter.video_engine.probe_media_file", return_value={
            "width": 640, "height": 640, "duration_s": 10.0, "fps": 30.0, "frame_count": 300,
        }), patch("platinum_sorter.video_engine.sample_video_frames", return_value=[
            (0.0, b"synthetic first frame"), (5.0, b"synthetic second frame"),
        ]):
            return self.analyze(**changes)

    def test_clean_preserves_signed_raw_evidence_without_confidence(self):
        for margin in (-9.25, 0.0, 2.5):
            record = semantic(margin)
            self.assertEqual([record], _clean_detections([record]))
            self.assertNotIn("score", _clean_detections([record])[0])
        record = localized(0.6212345)
        self.assertEqual([record], _clean_detections([record]))
        self.assertEqual([semantic()], _clean_detections([{**semantic(), "accepted": False}]))

    def test_invalid_semantic_fields_are_rejected(self):
        changes = [{"raw_margin": float("nan")}, {"positive_logit": float("inf")},
                   {"negative_logit": float("-inf")}, {"box": [0, 0, 640, 640]},
                   {"score": 1.0}, {"scope": "region"}, {"score_kind": "confidence"},
                   {"class": "BUTTOCKS_EXPOSED"}, {"model_revision": ""}, {"prompt_revision": None}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                _clean_detections([{**semantic(), **change}])

    def test_acceptance_keeps_detector_threshold_and_semantic_boundary_separate(self):
        for threshold in (0.25, 0.62, 0.99):
            options = self.options(threshold=threshold)
            self.assertTrue(accepts_detection(semantic(0.00001), options))
            self.assertFalse(accepts_detection(semantic(0.0), options))
            self.assertFalse(accepts_detection(semantic(-1.0), options))
        self.assertTrue(accepts_detection(localized(0.62), self.options()))
        self.assertFalse(accepts_detection(localized(0.6199), self.options()))
        self.assertEqual(0.62, self.options().threshold)

    def test_class_and_geometry_filters_apply_to_semantic_evidence(self):
        for options in (self.options(selected_classes=["FACE"]), self.options(min_prominence=0.01),
                        self.options(min_aspect_ratio=0.01)):
            self.assertFalse(accepts_detection(semantic(), options))
        self.assertTrue(accepts_detection(semantic(), self.options(selected_classes=["BUTTOCKS_COVERED"])))

    def test_source_order_does_not_compare_margin_with_confidence(self):
        records = [semantic(100.0), localized(0.8)]
        self.assertEqual("FACE", sorted(records, key=evidence_sort_key, reverse=True)[0]["class"])
        self.assertEqual(["FACE", "BUTTOCKS_COVERED"], _select_categories(records, self.options(mode="all")))
        self.assertEqual(["FACE"], _select_categories(records, self.options(mode="best")))
        self.assertEqual(["BUTTOCKS_COVERED"], _select_categories(records, self.options(selected_classes=["BUTTOCKS_COVERED"])))

    def test_semantic_evidence_cannot_supply_geometry_or_a_focal_point(self):
        record = semantic(100.0)
        summary = evaluate_frame_detections([record], 640, 640)
        self.assertIsNone(summary["best_detection"])
        self.assertEqual(0.0, summary["max_prominence"])
        self.assertNotIn("prominence", record)
        self.assertNotIn("aspect", record)
        self.assertEqual((320, 320), ken_burns_focal_point([record], 640, 640))
        summary = evaluate_frame_detections([record, localized()], 640, 640)
        self.assertEqual("FACE", summary["best_detection"]["class"])
        self.assertEqual(0.225, summary["max_prominence"])
        self.assertEqual((80, 80), summary["focal_point"])

    def test_semantic_cache_reuses_raw_evidence_across_review_filters(self):
        path = self.source_file()
        before = path.read_bytes()
        self.detector.outputs = [[localized(0.7), semantic()]]
        first = self.analyze(mode="all").results[0]
        second = self.analyze(threshold=0.99).results[0]
        excluded = self.analyze(selected_classes=["FACE"], threshold=0.99).results[0]
        geometric_filter = self.analyze(threshold=0.99, min_prominence=0.01).results[0]
        self.assertEqual(["FACE", "BUTTOCKS_COVERED"], first.categories)
        self.assertEqual(["BUTTOCKS_COVERED"], second.categories)
        self.assertEqual(["_Unmatched"], excluded.categories)
        self.assertEqual(["_Unmatched"], geometric_filter.categories)
        self.assertTrue(second.cached and excluded.cached and geometric_filter.cached)
        self.assertEqual(1, self.detector.calls)
        self.assertEqual({**semantic(), "accepted": True}, second.detections[1])
        self.assertEqual(0.7, second.detections[0]["score"])
        self.assertTrue(first.detections[0]["accepted"])
        self.assertFalse(second.detections[0]["accepted"])
        self.assertFalse(geometric_filter.detections[1]["accepted"])
        self.assertFalse(is_accepted_semantic(geometric_filter.detections[1], ["BUTTOCKS_COVERED"]))
        with closing(sqlite3.connect(self.root / "data" / "detections.sqlite3")) as cache:
            payload = json.loads(cache.execute("SELECT payload FROM detections").fetchone()[0])
        self.assertTrue(all("accepted" not in record for record in payload["detections"]))
        self.assertFalse(second.geometry_available)
        self.assertEqual([], second.best_box)
        self.assertEqual((0.0, 0.0, 0.0), (second.prominence, second.aspect_ratio, second.sustained_wow))
        self.assertEqual(before, path.read_bytes())
        self.assertFalse(self.destination.exists())

    def test_model_prompt_and_preprocess_revision_invalidate_cache(self):
        self.source_file()
        self.analyze()
        for revision in ("model-v2", "prompts-v2", "preprocess-v2"):
            self.detector.fingerprint += ":" + revision
            self.assertFalse(self.analyze().results[0].cached)
        self.assertEqual(4, self.detector.calls)

    def test_raw_evidence_and_unknown_geometry_survive_reports(self):
        self.source_file()
        report = self.analyze()
        restored = load_report(Path(report.manifest_path)).results[0]
        self.assertEqual([{**semantic(), "accepted": True}], restored.detections)
        self.assertFalse(restored.geometry_available)
        for suffix in ("json", "csv"):
            destination = self.root / f"report.{suffix}"
            export_report(report, destination)
            if suffix == "json":
                row = json.loads(destination.read_text(encoding="utf-8"))["results"][0]
                self.assertEqual([{**semantic(), "accepted": True}], row["detections"])
            else:
                with destination.open(encoding="utf-8-sig", newline="") as stream:
                    row = next(csv.DictReader(stream))
                self.assertEqual([{**semantic(), "accepted": True}], json.loads(row["detections"]))
                self.assertEqual("False", row["geometry_available"])
                self.assertEqual("0.0", row["best_match_timestamp_s"])
        legacy = ImageResult(source="synthetic", relative_path="synthetic", size=0, mtime_ns=0)
        self.assertFalse(legacy.geometry_available)
        self.assertEqual(0.0, legacy.best_match_timestamp_s)

    def test_video_semantic_match_retains_actual_frame_timestamp(self):
        self.source_file("mp4")
        self.detector.outputs = [[semantic(-2.0)], [semantic(3.0)]]
        result = self.video_analysis().results[0]
        self.assertEqual(["BUTTOCKS_COVERED"], result.categories)
        self.assertEqual(5.0, result.best_match_timestamp_s)
        self.assertEqual([0.0, 5.0], [d["timestamp_s"] for d in result.detections])
        self.assertEqual(0.0, result.best_timestamp_s)
        self.assertFalse(result.geometry_available)
        self.assertEqual([], result.best_box)
        self.assertEqual((0.0, 0.0, 0.0), (result.prominence, result.aspect_ratio, result.sustained_wow))
        again = self.video_analysis(threshold=0.99).results[0]
        self.assertTrue(again.cached)
        self.assertEqual(result.detections, again.detections)
        self.assertEqual(5.0, again.best_match_timestamp_s)

    def test_video_geometry_peak_and_semantic_match_are_independent(self):
        self.source_file("mp4")
        self.detector.outputs = [[localized(), semantic(-2.0)], [semantic(3.0)]]
        result = self.video_analysis(mode="all").results[0]
        self.assertTrue(result.geometry_available)
        self.assertEqual([0, 0, 160, 160], result.best_box)
        self.assertEqual(0.225, result.prominence)
        self.assertEqual(0.0, result.best_timestamp_s)
        self.assertEqual(5.0, result.best_match_timestamp_s)

    def test_filtered_semantic_evidence_is_not_an_accepted_match_of_a_localized_category(self):
        self.source_file("mp4")
        region = {**localized(), "class": "BUTTOCKS_COVERED"}
        self.detector.outputs = [[region], [semantic(3.0)]]
        result = self.video_analysis(min_prominence=0.1).results[0]
        self.assertEqual(["BUTTOCKS_COVERED"], result.categories)
        self.assertTrue(result.geometry_available)
        self.assertTrue(result.detections[0]["accepted"])
        self.assertFalse(result.detections[1]["accepted"])
        self.assertFalse(is_accepted_semantic(result.detections[1], result.categories))
        self.assertEqual(0.0, result.best_match_timestamp_s)
        again = self.video_analysis().results[0]
        self.assertTrue(again.cached)
        self.assertTrue(is_accepted_semantic(again.detections[1], again.categories))
        self.assertEqual(5.0, again.best_match_timestamp_s)
        self.assertFalse(is_accepted_semantic(again.detections[1], ["FACE"]))

    def test_semantic_stage_failure_is_partial_not_cached_or_filed(self):
        source = self.source_file("mp4")
        self.detector.outputs = [[semantic()], RuntimeError("semantic stage failed")]
        report = self.video_analysis(operation="move")
        self.assertEqual("partial", report.results[0].status)
        self.assertEqual(1, report.results[0].failed_frames)
        self.engine.execute(report, lambda event: None, self.cancel)
        self.assertTrue(source.exists())
        self.assertFalse(self.destination.exists())
        self.detector.outputs = [[semantic()], [semantic()]]
        self.assertFalse(self.video_analysis().results[0].cached)

    def test_invalid_semantic_result_is_error_not_cached_no_match(self):
        self.source_file()
        self.detector.outputs = [[semantic(float("nan"))]]
        first = self.analyze().results[0]
        self.assertEqual("error", first.status)
        self.assertEqual([], first.categories)
        self.detector.outputs = [[semantic(-1.0)]]
        second = self.analyze().results[0]
        self.assertFalse(second.cached)
        self.assertEqual(["_Unmatched"], second.categories)


if __name__ == "__main__":
    unittest.main()
