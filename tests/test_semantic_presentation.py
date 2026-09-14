"""Semantic evidence is never displayed as detector confidence or measured geometry."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import asdict
from pathlib import Path
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from platinum_sorter.contracts import ImageResult
from platinum_sorter.flight_report import generate_flight_report
from platinum_sorter.ui import ResultsFilter, ResultsModel, _evidence_text, _match_text, _unlocalized_match


def semantic_result():
    return ImageResult(source="synthetic.mp4", relative_path="synthetic.mp4", size=100,
                       mtime_ns=0, media_type="video", best_match_timestamp_s=7.5,
                       categories=["BUTTOCKS_COVERED"], detections=[{
                           "class": "BUTTOCKS_COVERED", "source": "siglip2",
                           "scope": "image", "score_kind": "logit_margin",
                           "raw_margin": 3.25, "box": [], "accepted": True,
                       }])


def test_image_match_text_keeps_margin_separate_from_confidence():
    result = semantic_result()
    assert _match_text(result) == "Image match (+3.25)"
    assert _unlocalized_match(result)
    assert "Region not localized" in _evidence_text(result.detections[0])
    assert "%" not in _evidence_text(result.detections[0])
    result.categories = ["_Unmatched"]
    assert _match_text(result) == "—"
    result.detections[0]["raw_margin"] = -3.0
    assert _evidence_text(result.detections[0]) == "No covered-body image match · margin -3.00"


def test_table_sorts_within_evidence_source_without_treating_margin_as_confidence():
    app = QApplication.instance() or QApplication([])
    model = ResultsModel()
    first, second = semantic_result(), semantic_result()
    first.detections.append({"class": "FACE_MALE", "score": .06, "box": [1, 1, 4, 4]})
    second.detections[0]["raw_margin"] = 5.0
    third = ImageResult(source="located.png", relative_path="located.png", size=1, mtime_ns=0,
                        detections=[{"class": "BUTTOCKS_COVERED", "score": .8, "box": [1, 1, 4, 4]}])
    model.replace([first, third, second])
    proxy = ResultsFilter()
    proxy.setSourceModel(model)
    proxy.sort(2, Qt.SortOrder.DescendingOrder)
    assert proxy.index(0, 2).data() == "80%"
    assert proxy.index(1, 2).data() == "Image match (+5.00)"
    assert proxy.index(2, 2).data() == "Image match (+3.25)"
    assert "Region not localized" in model.index(0, 2).data(Qt.ItemDataRole.ToolTipRole)


def test_report_labels_image_evidence_and_actual_sample_without_fake_wow():
    with tempfile.TemporaryDirectory() as folder:
        page = generate_flight_report("Synthetic", Path(folder) / "report.html",
                                      [asdict(semantic_result())]).read_text(encoding="utf-8")
    assert "Region not localized" in page
    assert "margin +3.25 (not a probability)" in page
    assert "matching sample 7.50s" in page
    assert "0.00 WOW" not in page
    assert "325%" not in page


def test_geometry_filtered_semantic_evidence_is_not_presented_as_a_filing_match():
    result = semantic_result()
    result.detections[0]["accepted"] = False
    result.detections.append({"class": "BUTTOCKS_COVERED", "score": .9, "box": [1, 1, 8, 8]})
    result.geometry_available = True
    result.best_box = [1, 1, 8, 8]
    assert _match_text(result) == "90%"
    assert not _unlocalized_match(result)
    with tempfile.TemporaryDirectory() as folder:
        page = generate_flight_report("Synthetic", Path(folder) / "report.html",
                                      [asdict(result)]).read_text(encoding="utf-8")
    assert "matching sample" not in page
    assert "Image-level covered-body match" not in page


def test_report_keeps_tolerating_absent_or_invalid_optional_detection_metadata():
    with tempfile.TemporaryDirectory() as folder:
        page = generate_flight_report("Synthetic", Path(folder) / "report.html", [
            {"source": "a.jpg", "detections": None},
            {"source": "b.jpg", "detections": [None, "invalid", {
                "source": "siglip2", "accepted": True, "raw_margin": float("nan")} ]},
        ]).read_text(encoding="utf-8")
    assert "a.jpg" in page and "b.jpg" in page
    assert "Image-level covered-body match" not in page
