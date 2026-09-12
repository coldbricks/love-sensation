import tempfile
import unittest
from pathlib import Path

from platinum_sorter.flight_report import generate_flight_report


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


if __name__ == "__main__":
    unittest.main()
