"""Bounded pipeline tests use synthetic files, never a user's photo library."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from contextlib import contextmanager
import io
from unittest.mock import patch

from platinum_sorter import engine as module
from platinum_sorter.contracts import SortOptions
from platinum_sorter.engine import SorterEngine, load_report


def detection(label="HAND"):
    return {"class": label, "score": 0.95, "box": [1, 2, 3, 4]}


class SnapshotDetector:
    fingerprint = "synthetic-snapshot-v1"
    info = {"device": "synthetic-test"}

    def __init__(self):
        self.encoded_calls = []
        self.path_calls = []
        self.on_detect = None

    def detect_encoded_batch(self, contents):
        self.encoded_calls.append(list(contents))
        assert all(isinstance(content, bytes) for content in contents)
        if self.on_detect:
            return self.on_detect(contents)
        return [[detection()] for _ in contents]

    def detect_batch(self, paths):
        self.path_calls.append(list(paths))
        return [[detection()] for _ in paths]


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="platinum-pipeline-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "input"
        self.source.mkdir()
        self.destination = self.root / "output"
        self.detector = SnapshotDetector()
        self.engine = SorterEngine(self.root / "data", lambda: self.detector)
        self.events = []
        self.cancel = threading.Event()

    def tearDown(self):
        self.temporary.cleanup()

    def image(self, name, contents=b"synthetic image"):
        path = self.source / name
        path.write_bytes(contents)
        return path

    def analyze(self, **changes):
        options = SortOptions(str(self.source), str(self.destination), **changes)
        return self.engine.analyze(options, self.events.append, self.cancel)

    def test_single_source_read_produces_model_bytes_and_matching_hash(self):
        paths = [self.image(f"{index:02}.jpg", bytes([index]) * 97) for index in range(7)]
        opens = []
        original_open = module._open_regular

        @contextmanager
        def counted_open(path, **kwargs):
            opens.append(path)
            with original_open(path, **kwargs) as file:
                yield file

        with patch.object(module, "_open_regular", counted_open):
            report = self.analyze(batch_size=3)
        self.assertEqual(paths, opens, "Normal inputs are read only once during analysis.")
        self.assertEqual([], self.detector.path_calls)
        encoded = [content for call in self.detector.encoded_calls for content in call]
        self.assertEqual([path.read_bytes() for path in paths], encoded)
        self.assertEqual([hashlib.sha256(content).hexdigest() for content in encoded],
                         [row.sha256 for row in report.results])
        self.assertEqual(["ready"] * 7, [row.status for row in report.results])
        self.assertTrue(load_report(Path(report.manifest_path)).analysis_complete)

    def test_reader_overlaps_detection_but_never_reads_more_than_one_group_ahead(self):
        paths = [self.image(f"{index}.jpg", bytes([index])) for index in range(4)]
        next_read = threading.Event()
        reads = []
        original_read = module._read_snapshot

        def tracked_read(path, *args):
            result = original_read(path, *args)
            reads.append(path)
            if path == paths[1]:
                next_read.set()
            return result

        def detect(contents):
            if contents == [b"\x00"]:
                self.assertTrue(next_read.wait(3), "Read/hash must overlap current inference.")
                self.assertEqual(paths[:2], reads, "Prefetch is a single bounded group.")
            return [[detection()] for _ in contents]

        self.detector.on_detect = detect
        with patch.object(module, "_read_snapshot", tracked_read):
            report = self.analyze(batch_size=1)
        self.assertEqual(["ready"] * 4, [row.status for row in report.results])

    def test_encoded_byte_budget_and_oversized_streaming_fallback(self):
        small = [self.image("0.jpg", b"0" * 6), self.image("1.jpg", b"1" * 6)]
        big = self.image("2.jpg", b"2" * 12)
        small.append(self.image("3.jpg", b"3" * 4))
        with patch.object(module, "_SNAPSHOT_BATCH_BYTES", 8):
            report = self.analyze(batch_size=16)
        self.assertTrue(all(sum(map(len, call)) <= 8 for call in self.detector.encoded_calls))
        self.assertEqual([[big]], self.detector.path_calls)
        self.assertEqual(["ready"] * 4, [row.status for row in report.results])
        self.assertEqual(3, sum(map(len, self.detector.encoded_calls)))

    def test_cache_rehashes_content_even_when_size_and_timestamp_match(self):
        path = self.image("same.jpg", b"before")
        self.analyze()
        self.assertTrue(self.analyze().results[0].cached)
        self.assertEqual(1, len(self.detector.encoded_calls))
        before = path.stat()
        path.write_bytes(b"after!")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        updated = self.analyze()
        self.assertFalse(updated.results[0].cached)
        self.assertEqual(b"after!", self.detector.encoded_calls[-1][0])
        self.assertEqual(hashlib.sha256(b"after!").hexdigest(), updated.results[0].sha256)

    def test_snapshot_matches_its_hash_and_changed_original_cannot_be_applied(self):
        path = self.image("same.jpg", b"before")

        def change_original(contents):
            self.assertEqual([b"before"], contents)
            before = path.stat()
            path.write_bytes(b"after!")
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            return [[detection()]]

        self.detector.on_detect = change_original
        report = self.analyze(operation="move")
        self.assertEqual(hashlib.sha256(b"before").hexdigest(), report.results[0].sha256)
        self.engine.execute(report, self.events.append, self.cancel)
        self.assertIn("changed since analysis", report.results[0].error)
        self.assertEqual(b"after!", path.read_bytes())
        self.assertFalse(self.destination.exists())

    def test_visible_source_change_during_detection_is_reported(self):
        path = self.image("same.jpg", b"before")

        def change_original(contents):
            path.write_bytes(b"different size")
            return [[detection()]]

        self.detector.on_detect = change_original
        report = self.analyze()
        self.assertEqual("error", report.results[0].status)
        self.assertIn("changed during analysis", report.results[0].error)

    def test_missing_file_and_bad_decode_are_isolated(self):
        vanished = self.image("0-missing.jpg")
        self.image("1-bad.jpg", b"bad")
        self.image("2-good.jpg", b"good")
        files = self.engine._files

        def remove_after_listing(*args):
            paths = files(*args)
            vanished.unlink()
            return paths

        def detect(contents):
            if b"bad" in contents:
                raise ValueError("invalid test image")
            return [[detection()] for _ in contents]

        self.detector.on_detect = detect
        with patch.object(self.engine, "_files", remove_after_listing):
            report = self.analyze()
        self.assertEqual(["error", "error", "ready"], [row.status for row in report.results])

    def test_cancel_stops_prefetch_and_leaves_originals_unchanged(self):
        paths = [self.image(f"{index}.jpg") for index in range(8)]

        def cancel_at_detection(contents):
            self.cancel.set()
            return [[detection()] for _ in contents]

        self.detector.on_detect = cancel_at_detection
        report = self.analyze(batch_size=1)
        self.assertTrue(report.cancelled)
        self.assertFalse(report.analysis_complete)
        self.assertEqual(1, len(self.detector.encoded_calls))
        self.assertTrue(all(path.read_bytes() == b"synthetic image" for path in paths))
        self.assertFalse(any(thread.name.startswith("image-reader") for thread in threading.enumerate()))

    def test_confirms_appledouble_magic_without_skipping_real_dotunderscore_images(self):
        sidecar = self.image("._photo.JPG", b"\x00\x05\x16\x07" + b"Mac OS X metadata")
        real = self.image("._real.JPG", b"\xff\xd8\xffactual photograph signature")
        normal = self.image("regular.jpg")
        report = self.analyze()
        self.assertEqual([real.name, normal.name], [row.relative_path for row in report.results])
        self.assertEqual(b"\x00\x05\x16\x07Mac OS X metadata", sidecar.read_bytes())
        self.assertTrue(any("Ignored 1 macOS metadata" in event.get("message", "") for event in self.events))

    def test_encoded_detector_preserves_pixels_and_exif_coordinates(self):
        # Exercise real decode/preprocessing without loading another model.
        import torch
        import torch.nn.functional as functional
        from PIL import Image
        from platinum_sorter.detector import GpuDetector

        detector = GpuDetector.__new__(GpuDetector)
        detector.torch, detector.functional = torch, functional
        detector.cuda = torch.cuda.is_available()
        detector.device = torch.device("cuda:0" if detector.cuda else "cpu")
        detector.dtype = torch.float16 if detector.cuda else torch.float32
        detector._cuda_jpeg = None
        detector.info = {}
        detector.emit = self.events.append
        pixels = torch.arange(18 * 30 * 3, device=detector.device).remainder(256).to(torch.uint8).reshape(18, 30, 3)
        image = Image.fromarray(pixels.cpu().numpy())
        for format, orientation, dimensions in (("PNG", 1, (30, 18)), ("JPEG", 1, (30, 18)), ("JPEG", 6, (18, 30))):
            with self.subTest(format=format, orientation=orientation):
                exif = Image.Exif()
                exif[274] = orientation
                encoded = io.BytesIO()
                image.save(encoded, format=format, exif=exif)
                content = encoded.getvalue()
                path = self.image(f"decode-{format}-{orientation}.{format.lower()}", content)
                with torch.inference_mode():
                    from_path, path_dimensions = detector._prepare(path)
                    from_bytes, byte_dimensions = detector._prepare(content)
                self.assertEqual(dimensions, byte_dimensions[:2])
                self.assertEqual(path_dimensions, byte_dimensions)
                self.assertEqual(str(detector.device), str(from_bytes.device))
                self.assertTrue(torch.equal(from_path, from_bytes))


if __name__ == "__main__":
    unittest.main()
