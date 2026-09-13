import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from platinum_sorter.flight_report import generate_flight_report


class ParsedReport(HTMLParser):
    """Inspect the actual parsed attribute boundaries, not escaped substrings."""
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.records = []
        self.stack = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))
        record = {"tag": tag, "attrs": dict(attrs), "text": []}
        self.records.append(record)
        if tag not in {"meta", "input", "br", "hr", "img", "link"}:
            self.stack.append(record)

    def handle_data(self, data):
        for record in self.stack:
            record["text"].append(data)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                break

    def text_for_id(self, identifier):
        return "".join(next(r["text"] for r in self.records if r["attrs"].get("id") == identifier)).strip()

    def with_class(self, value):
        return [attrs for _, attrs in self.elements if value in attrs.get("class", "").split()]


class FlightReportTests(unittest.TestCase):
    def test_generate_flight_report(self):
        with tempfile.TemporaryDirectory(prefix="test_flight_report_") as tmp_dir:
            out_file = Path(tmp_dir) / "report.html"
            results = [
                {"source": "clip1.mp4", "media_type": "video", "prominence": 0.85, "categories": ["GLUTES", "DANCE"]},
                {"source": "photo1.jpg", "media_type": "still", "prominence": 0.60, "categories": ["PORTRAIT"]},
            ]
            audio_grid = {
                "bpm": 128.0,
                "total_duration_s": 30.0,
                "bars": [0.0, 1.875, 3.75, 5.625],
                "drop_bars": [2],
            }
            cuts = [
                {"clip_name": "clip1.mp4", "timeline_start_s": 0.0, "timeline_end_s": 1.875, "tag": "normal"},
                {"clip_name": "clip1.mp4", "timeline_start_s": 1.875, "timeline_end_s": 3.75, "tag": "peak"},
            ]
            res = generate_flight_report("Test Run Flight Report", out_file, results, audio_grid, cuts)
            self.assertTrue(res.is_file())
            content = res.read_text(encoding="utf-8")
            self.assertIn("Test Run Flight Report", content)
            self.assertIn("128.0 BPM", content)
            self.assertIn("clip1.mp4", content)
            # Emblem verification: Moon-and-spoon vector SVG present
            self.assertIn("<svg", content)
            self.assertIn("gold-grad", content)
            self.assertIn("silver-grad", content)
            # Cockpit badge and stats
            self.assertIn("CUDA ACCELERATED &bull; ZERO EXTERNAL LIBS", content)
            self.assertIn("Total Media Items", content)
            self.assertIn("Video Clips & Comps", content)
            self.assertIn("Still Photographs", content)
            # Radar scope cut blocks
            self.assertIn("cut-block", content)
            # Interactive search and filter script
            self.assertIn("filterByCategory", content)
            self.assertIn("filterCatalog", content)

    def test_generate_flight_report_empty(self):
        with tempfile.TemporaryDirectory(prefix="test_flight_report_empty_") as tmp_dir:
            out_file = Path(tmp_dir) / "empty_report.html"
            res = generate_flight_report("Empty Cockpit", out_file, [], audio_grid=None, cuts=None)
            self.assertTrue(res.is_file())
            content = res.read_text(encoding="utf-8")
            self.assertIn("Empty Cockpit", content)
            self.assertIn("Total Media Items", content)
            self.assertIn("Still Photographs", content)

    def test_generate_flight_report_none_and_malformed_values(self):
        with tempfile.TemporaryDirectory(prefix="test_flight_report_none_") as tmp_dir:
            out_file = Path(tmp_dir) / "none_report.html"
            results = [
                {
                    "source": None,
                    "media_type": None,
                    "prominence": None,
                    "sustained_wow": "invalid",
                    "duration_s": None,
                    "categories": None,
                },
                {
                    "source": "clip_corrupt.mp4",
                    "media_type": "video",
                    "prominence": "not_a_float",
                    "sustained_wow": None,
                    "duration_s": -5.0,
                    "categories": ["VALID", None, 123],
                },
            ]
            audio_grid = {
                "bpm": None,
                "total_duration_s": None,
                "bars": None,
                "drop_bars": None,
            }
            cuts = [
                {
                    "clip_name": None,
                    "timeline_start_s": None,
                    "timeline_end_s": None,
                    "tag": None,
                }
            ]
            res = generate_flight_report("Hardened Flight Report", out_file, results, audio_grid, cuts)
            self.assertTrue(res.is_file())
            content = res.read_text(encoding="utf-8")
            self.assertIn("Hardened Flight Report", content)
            self.assertIn("0.0 BPM", content)
            self.assertIn("unnamed", content)
            self.assertIn("clip_corrupt.mp4", content)
            self.assertIn("VALID", content)

    def test_clip_path_records_display_names_and_video_type_without_private_directories(self):
        with tempfile.TemporaryDirectory(prefix="test_report_schema_") as folder:
            content = generate_flight_report("Clips", Path(folder) / "report.html", [
                {"path": r"D:\private-library\take one.MP4", "duration_s": 7.5},
                {"source": "/private-library/portrait.jpg", "path": "wrong.mp4"},
            ]).read_text(encoding="utf-8")
            cards = ParsedReport(content).with_class("media-card")
            self.assertEqual(["take one.mp4", "portrait.jpg"], [r["data-name"] for r in cards])
            self.assertEqual(["video", "still"], [r["data-type"] for r in cards])
            self.assertNotIn("private-library", content)
            self.assertNotIn("wrong.mp4", content)
            self.assertFalse(any(tag in {"img", "video", "iframe"} for tag, _ in ParsedReport(content).elements))

    def test_quoted_categories_remain_data_in_html_and_filter_controls(self):
        category = 'Family\'s "album" <notes> & travel'
        malicious_category = 'x" onmouseover="unexpected()'
        with tempfile.TemporaryDirectory(prefix="test_report_quotes_") as folder:
            content = generate_flight_report("A < B", Path(folder) / "report.html", [
                {"source": 'portrait "one".jpg', "categories": [category, malicious_category]},
            ]).read_text(encoding="utf-8")
            parsed = ParsedReport(content)
            filters = parsed.with_class("cat-row")
            self.assertEqual([category, malicious_category], [r["data-category"] for r in filters])
            self.assertTrue(all("onclick" not in r and "onmouseover" not in r for r in filters))
            card = parsed.with_class("media-card")[0]
            self.assertEqual(f"{category}, {malicious_category}".lower(), card["data-cats"])
            self.assertEqual('portrait "one".jpg', card["data-name"])
            self.assertNotIn("onmouseover", card)
            self.assertEqual(1, sum(tag == "script" for tag, _ in parsed.elements))
            self.assertNotIn("<notes>", content)

    def test_timeline_metadata_cannot_create_html_attributes_or_svg_markup(self):
        tag = 'normal" onclick="unexpected()'
        waveform = '0,0"/><script>unexpected()</script><polygon points="'
        with tempfile.TemporaryDirectory(prefix="test_report_timeline_") as folder:
            content = generate_flight_report("Timeline", Path(folder) / "report.html", [],
                audio_grid={"total_duration_s": 10, "bpm": float("nan")},
                cuts=[{"clip_name": '<test> "one"', "tag": tag,
                       "timeline_start_s": float("inf"), "timeline_end_s": 5}],
                waveform_svg=waveform).read_text(encoding="utf-8")
            parsed = ParsedReport(content)
            cut = parsed.with_class("cut-block")[0]
            self.assertIn(tag, cut["title"])
            self.assertNotIn("onclick", cut)
            self.assertEqual(1, sum(t == "script" for t, _ in parsed.elements))
            polygon = next(attrs for name, attrs in parsed.elements if name == "polygon")
            self.assertEqual("", polygon["points"])
            self.assertIn("0.0 BPM", content)
            self.assertNotIn("inf%", content)

    def test_valid_waveform_and_single_category_string_are_preserved(self):
        with tempfile.TemporaryDirectory(prefix="test_report_points_") as folder:
            content = generate_flight_report("Timeline", Path(folder) / "report.html",
                [{"path": "clip.mov", "categories": "PORTRAIT", "prominence": float("inf")}],
                audio_grid={"total_duration_s": 2}, cuts=[{"timeline_end_s": 1}],
                waveform_svg="0,60 1.5,30 800,60").read_text(encoding="utf-8")
            parsed = ParsedReport(content)
            self.assertEqual(["PORTRAIT"], [r["data-category"] for r in parsed.with_class("cat-row")])
            self.assertEqual("0,60 1.5,30 800,60", next(a["points"] for t, a in parsed.elements if t == "polygon"))
            self.assertNotIn("inf WOW", content)

    def test_failed_replacement_keeps_previous_report_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory(prefix="test_report_atomic_") as folder:
            output = Path(folder) / "report.html"
            output.write_text("previous review", encoding="utf-8")
            with patch("platinum_sorter.flight_report.os.replace", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(OSError, "disk error"):
                    generate_flight_report("New review", output, [])
            self.assertEqual("previous review", output.read_text(encoding="utf-8"))
            self.assertEqual([output], list(Path(folder).iterdir()))

    def test_review_inclusion_keeps_skipped_history_and_counts_legacy_records_as_included(self):
        with tempfile.TemporaryDirectory(prefix="test_report_review_") as folder:
            content = generate_flight_report("Review", Path(folder) / "report.html", [
                {"source": "legacy.jpg"},
                {"source": "chosen.jpg", "included": True},
                {"source": "skipped.jpg", "included": False},
            ]).read_text(encoding="utf-8")
            parsed = ParsedReport(content)
            self.assertEqual("2", parsed.text_for_id("includedCount"))
            self.assertEqual("1", parsed.text_for_id("skippedCount"))
            self.assertEqual(["included", "included", "skipped"],
                             [r["data-review-state"] for r in parsed.with_class("media-card")])
            self.assertIn("skipped.jpg", content, "Skipped review records remain in the audit catalog")


if __name__ == "__main__":
    unittest.main()
