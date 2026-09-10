"""Face export safety tests: synthetic pixels and saved fake detections only."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageOps

from platinum_sorter.contracts import ImageResult, ScanReport, SortOptions
from platinum_sorter import engine, face_crops
from platinum_sorter.face_crops import FaceCropService, _bounds


def detection(label="FACE_FEMALE", score=0.9, box=None):
    return {"class": label, "score": score, "box": box or [2, 3, 8, 6]}


class FaceCropTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="platinum-face-test-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.sorted = self.root / "sorted"
        self.output = self.root / "face-crops"
        self.report = ScanReport("synthetic-face-test", SortOptions(str(self.source), str(self.sorted)),
                                 analysis_complete=True)

    def tearDown(self):
        self.temporary.cleanup()

    def image(self, name="fixture.png", detections=None, size=(24, 20), orientation=None):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", size)
        image.putdata([((x * 13) % 256, (y * 17) % 256, (x + y) % 256)
                       for y in range(size[1]) for x in range(size[0])])
        options = {}
        if orientation is not None:
            exif = image.getexif()
            exif[274] = orientation
            options["exif"] = exif
        image.save(path, **options)
        stats = path.stat()
        row = ImageResult(str(path), str(path.relative_to(self.source)), stats.st_size, stats.st_mtime_ns,
                          detections=[detection()] if detections is None else detections,
                          sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        self.report.results.append(row)
        return path, row

    def test_only_qualifying_faces_and_sort_filter_is_independent(self):
        self.report.options.selected_classes = ["FEET_EXPOSED"]
        self.image(detections=[detection(), detection("FACE_MALE", .62),
                               detection("FACE_MALE", .61), detection("FEET_EXPOSED", .99),
                               detection("ANUS_EXPOSED", .99), detection("FACE", .99),
                               detection("FACE_MALE", .99, [1, 2, -2, 4]),
                               detection("FACE_MALE", float("nan"))])
        candidates = FaceCropService(self.report).candidates()
        self.assertEqual(["FACE_FEMALE", "FACE_MALE"], [item["class"] for item in candidates])
        self.assertEqual([1, 2], [item["face_index"] for item in candidates])

    def test_incomplete_analysis_rejected(self):
        self.report.analysis_complete = False
        with self.assertRaisesRegex(ValueError, "Finish"):
            FaceCropService(self.report)

    def test_candidate_geometry_cannot_be_replaced_by_caller(self):
        self.image()
        service = FaceCropService(self.report)
        changed = service.candidates()[0]
        changed["box"] = [0, 0, 24, 20]
        with self.assertRaisesRegex(ValueError, "differs"):
            service.preview(changed)
        with self.assertRaisesRegex(ValueError, "differs"):
            service.export([changed], self.output)
        self.assertFalse(self.output.exists())

    def test_bounds_padding_clamps_and_rejects_empty_intersection(self):
        self.assertEqual((0, 0, 24, 20), _bounds([-2, -3, 30, 30], 24, 20, .3))
        self.assertEqual((1, 2, 11, 10), _bounds([2, 3, 8, 6], 24, 20, .1))
        with self.assertRaisesRegex(ValueError, "outside"):
            _bounds([30, 30, 2, 2], 24, 20, .1)

    def test_preview_pixels_dimensions_and_source_preserved(self):
        path, _ = self.image()
        before = path.read_bytes()
        service = FaceCropService(self.report)
        preview = service.preview(service.candidates()[0], padding=0)
        self.assertEqual((8, 6), (preview["width"], preview["height"]))
        with Image.open(io.BytesIO(preview["png"])) as actual, Image.open(path) as original:
            expected = original.crop((2, 3, 10, 9))
            self.assertEqual(expected.tobytes(), actual.tobytes())
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(preview["info"]["device"], preview["info"]["crop_device"])
        self.assertEqual(preview["info"]["cuda_available"], preview["info"]["cuda_verified"])

    def test_preview_resize_stays_on_selected_device(self):
        self.image(size=(1000, 600), detections=[detection(box=[0, 0, 1000, 600])])
        service = FaceCropService(self.report)
        preview = service.preview(service.candidates()[0], padding=0)
        self.assertEqual((1000, 600), (preview["width"], preview["height"]))
        with Image.open(io.BytesIO(preview["png"])) as image:
            self.assertEqual((480, 288), image.size)
        self.assertEqual(preview["info"]["device"], preview["info"]["resize_device"])

    def test_exif_coordinates_use_normalized_image(self):
        path, _ = self.image("rotated.jpg", size=(24, 20), orientation=6,
                             detections=[detection(box=[1, 2, 8, 10])])
        service = FaceCropService(self.report)
        preview = service.preview(service.candidates()[0], padding=0)
        with Image.open(io.BytesIO(preview["png"])) as actual, Image.open(path) as original:
            normalized = ImageOps.exif_transpose(original).convert("RGB")
            expected = normalized.crop((1, 2, 9, 12))
            self.assertEqual(expected.tobytes(), actual.tobytes())
        self.assertEqual((8, 10), (preview["width"], preview["height"]))

    def test_stale_source_rejected_before_decode(self):
        path, row = self.image()
        service = FaceCropService(self.report)
        changed = bytearray(path.read_bytes())
        changed[-1] ^= 1
        path.write_bytes(changed)
        os.utime(path, ns=(row.mtime_ns, row.mtime_ns))
        with patch.object(service, "_decode") as decode:
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                service.preview(service.candidates()[0])
            result = service.export(service.candidates(), self.output)
            decode.assert_not_called()
        self.assertEqual(0, result["exported"])
        self.assertEqual(1, len(result["errors"]))

    def test_export_collisions_relative_folders_and_durable_manifest(self):
        path, _ = self.image("sub/fixture.png", detections=[detection(), detection("FACE_MALE", box=[11, 4, 5, 8])])
        before = path.read_bytes()
        (self.output / "sub").mkdir(parents=True)
        collision = self.output / "sub/fixture__face_01.png"
        collision.write_bytes(b"keep existing file")
        service = FaceCropService(self.report)
        with patch.object(service, "_read_source", wraps=service._read_source) as read, \
             patch.object(service, "_decode", wraps=service._decode) as decode, \
             patch.object(face_crops, "_atomic_json", wraps=face_crops._atomic_json) as snapshot:
            result = service.export(service.candidates(), self.output, padding=0)
            self.assertEqual(1, read.call_count)
            self.assertEqual(1, decode.call_count)
            self.assertEqual(2, snapshot.call_count, "Full snapshots happen only at start/end; crop updates use WAL.")
        self.assertEqual(2, result["exported"])
        self.assertEqual([], result["errors"])
        self.assertEqual(b"keep existing file", collision.read_bytes())
        self.assertTrue((self.output / "sub/fixture__face_01__2.png").is_file())
        self.assertEqual(before, path.read_bytes())
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result, manifest)
        self.assertTrue(all(entry["output_sha256"] for entry in manifest["entries"]))
        events = [json.loads(line) for line in Path(result["journal_path"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["reserved", "result", "reserved", "result", "finished"], [event["event"] for event in events])

    def test_reparse_checks_reject_source_and_destination_without_os_privileges(self):
        path, _ = self.image()
        service = FaceCropService(self.report)
        self.output.mkdir()
        real_is_link = engine._is_link
        with patch.object(engine, "_is_link", side_effect=lambda item: item == self.output or real_is_link(item)):
            with self.assertRaisesRegex(ValueError, "links|junctions"):
                service.export(service.candidates(), self.output)
        with patch.object(engine, "_is_link", side_effect=lambda item: item == path or real_is_link(item)):
            with self.assertRaisesRegex(ValueError, "links|junctions"):
                service.preview(service.candidates()[0])

    def test_moved_image_uses_verified_recorded_full_image(self):
        path, row = self.image()
        self.report.options.operation = "move"
        row.status, row.categories = "moved", ["FACE_FEMALE"]
        moved = self.sorted / "FACE_FEMALE" / path.name
        moved.parent.mkdir(parents=True)
        path.rename(moved)
        row.destinations = [str(moved)]
        row.output_details = [{"status": "verified", "path": str(moved), "category": "FACE_FEMALE"}]
        service = FaceCropService(self.report)
        preview = service.preview(service.candidates()[0])
        self.assertEqual(str(moved), preview["read_source"])
        self.assertFalse(path.exists())
        self.assertTrue(moved.exists())

    def test_moved_image_escape_and_changed_hash_rejected(self):
        path, row = self.image()
        self.report.options.operation = "move"
        row.status, row.categories = "moved", ["FACE_FEMALE"]
        moved = self.root / "unrelated.png"
        path.rename(moved)
        row.destinations = [str(moved)]
        row.output_details = [{"status": "verified", "path": str(moved), "category": "FACE_FEMALE"}]
        service = FaceCropService(self.report)
        with self.assertRaisesRegex(ValueError, "outside"):
            service.preview(service.candidates()[0])
        proper = self.sorted / "FACE_FEMALE" / path.name
        proper.parent.mkdir(parents=True)
        moved.rename(proper)
        proper.write_bytes(proper.read_bytes() + b"changed")
        row.destinations = [str(proper)]
        row.output_details[0]["path"] = str(proper)
        service = FaceCropService(self.report)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            service.preview(service.candidates()[0])

    def test_source_and_destination_links_or_overlap_rejected(self):
        path, _ = self.image()
        service = FaceCropService(self.report)
        for output in (self.source, self.source / "crops", self.root):
            with self.subTest(output=str(output)), self.assertRaises(ValueError):
                service.export(service.candidates(), output)
        link = self.root / "linked-output"
        self.output.mkdir()
        try:
            os.symlink(self.output, link, target_is_directory=True)
        except OSError:
            self.skipTest("This Windows session cannot create symbolic links.")
        with self.assertRaisesRegex(ValueError, "links|junctions"):
            service.export(service.candidates(), link)
        replacement = self.root / "replacement.png"
        path.rename(replacement)
        os.symlink(replacement, path)
        with self.assertRaisesRegex(ValueError, "links|junctions"):
            service.preview(service.candidates()[0])

    def test_partial_failure_keeps_good_crops_and_originals(self):
        good, _ = self.image("good.png")
        bad, _ = self.image("bad.png")
        bad.write_bytes(b"stale image")
        service = FaceCropService(self.report)
        result = service.export(service.candidates(), self.output)
        self.assertEqual(1, result["exported"])
        self.assertEqual(1, len(result["errors"]))
        self.assertEqual(str(bad), result["errors"][0]["source"])
        self.assertTrue(good.exists())
        self.assertEqual(b"stale image", bad.read_bytes())

    def test_cancel_between_faces_keeps_completed_crop_and_manifest(self):
        path, _ = self.image(detections=[detection(), detection("FACE_MALE", box=[12, 4, 5, 8])])
        service = FaceCropService(self.report)
        cancel = threading.Event()
        events = []
        def emit(event):
            events.append(event)
            if event["type"] == "progress":
                cancel.set()
        result = service.export(service.candidates(), self.output, emit=emit, cancel=cancel)
        self.assertEqual(1, result["exported"])
        self.assertTrue(result["cancelled"])
        self.assertTrue(path.exists())
        saved = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertTrue(saved["cancelled"])
        self.assertEqual(1, saved["exported"])

    def test_precancel_and_bad_padding_do_not_decode(self):
        self.image()
        service = FaceCropService(self.report)
        cancel = threading.Event()
        cancel.set()
        with patch.object(service, "_decode") as decode:
            result = service.export(service.candidates(), self.output, cancel=cancel)
            self.assertTrue(result["cancelled"])
            self.assertEqual(0, result["exported"])
            decode.assert_not_called()
        for padding in (-.01, .31, float("nan"), True):
            with self.subTest(padding=padding), self.assertRaises(ValueError):
                service.preview(service.candidates()[0], padding=padding)

    def test_cpu_fallback_only_when_cuda_unavailable(self):
        import torch
        self.image()
        service = FaceCropService(self.report)
        with patch.object(torch.cuda, "is_available", return_value=False):
            preview = service.preview(service.candidates()[0])
        self.assertEqual("cpu", preview["info"]["crop_device"])
        self.assertFalse(preview["info"]["cuda_available"])


if __name__ == "__main__":
    unittest.main()
