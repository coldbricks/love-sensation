import os
import tempfile
import unittest
from pathlib import Path

from platinum_sorter.contracts import ImageResult, ScanReport, SortOptions
from platinum_sorter.engine import SorterEngine, _signature


class FakeDetector:
    fingerprint = "fake-hardlink-detector-v1"
    info = {"provider": "test"}

    def detect_batch(self, paths):
        return [[{"class": "ANATOMY", "score": 0.95, "box": [10, 10, 200, 200]}]] * len(paths)


class HardlinkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="test_hardlink_")
        self.root = Path(self.temporary.name).resolve()
        self.source_dir = self.root / "src"
        self.source_dir.mkdir()
        self.output_dir = self.root / "dst"
        self.output_dir.mkdir()
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()

        self.sample_file = self.source_dir / "photo.jpg"
        self.sample_file.write_bytes(b"EXACT_SAME_BYTES_FOR_HARDLINK_TEST_12345678")
        self.engine = SorterEngine(self.data_dir, lambda: FakeDetector())

    def tearDown(self):
        self.temporary.cleanup()

    def test_hardlink_sorting(self):
        opts = SortOptions(
            source=str(self.source_dir),
            destination=str(self.output_dir),
            operation="hardlink",
            threshold=0.5,
        )

        import threading
        cancel = threading.Event()
        events = []
        report = self.engine.analyze(opts, events.append, cancel)
        self.assertEqual(len(report.results), 1)
        res = report.results[0]
        self.assertEqual(res.status, "ready")
        self.assertIn("ANATOMY", res.categories)

        # Apply hardlink
        applied_report = self.engine.execute(report, events.append, cancel)
        applied_res = applied_report.results[0]
        self.assertEqual(applied_res.status, "hardlinked")
        self.assertGreaterEqual(len(applied_res.destinations), 1)

        dest_path = Path(applied_res.destinations[0])
        self.assertTrue(dest_path.is_file())
        self.assertEqual(dest_path.read_bytes(), self.sample_file.read_bytes())

        # Check signature matches
        sig = _signature(dest_path)
        self.assertEqual(sig[0], applied_res.size)
        self.assertEqual(sig[2], applied_res.sha256)

    def test_hardlink_fallback_to_copy(self):
        from unittest.mock import patch
        opts = SortOptions(
            source=str(self.source_dir),
            destination=str(self.output_dir),
            operation="hardlink",
            threshold=0.5,
        )

        import threading
        cancel = threading.Event()
        events = []
        report = self.engine.analyze(opts, events.append, cancel)
        self.assertEqual(len(report.results), 1)

        # Mock _create_hardlink_win32 to return False (simulating cross-drive or unsupported FS)
        with patch("platinum_sorter.engine._create_hardlink_win32", return_value=False):
            applied_report = self.engine.execute(report, events.append, cancel)
            applied_res = applied_report.results[0]
            # Must fall back to copied
            self.assertEqual(applied_res.status, "copied")
            self.assertGreaterEqual(len(applied_res.destinations), 1)
            dest_path = Path(applied_res.destinations[0])
            self.assertTrue(dest_path.is_file())
            self.assertEqual(dest_path.read_bytes(), self.sample_file.read_bytes())
            self.assertEqual(applied_res.output_details[0]["method"], "copy")


if __name__ == "__main__":
    unittest.main()
