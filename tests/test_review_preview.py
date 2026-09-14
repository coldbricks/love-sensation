"""Memory-only preview safety/layout tests; tiny synthetic images, no model."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageOps
import torch

from platinum_sorter.contracts import ImageResult
from platinum_sorter import review_preview as module


def png_bytes(size=(24, 16)):
    out = io.BytesIO()
    Image.new("RGB", size, (40, 80, 120)).save(out, format="PNG")
    return out.getvalue()


class ReviewPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="love-preview-test-")
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def source(self, name="source.png", *, data=None, size=(24, 16), orientation=None):
        path = self.root / name
        if data is not None:
            path.write_bytes(data)
        else:
            image = Image.new("RGB", size)
            image.putdata([((x * 7) % 256, (y * 11) % 256, 96) for y in range(size[1]) for x in range(size[0])])
            kwargs = {}
            if orientation is not None:
                exif = Image.Exif()
                exif[274] = orientation
                kwargs["exif"] = exif
            image.save(path, **kwargs)
        info = path.stat()
        result = ImageResult(str(path), path.name, info.st_size, info.st_mtime_ns,
                             sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        return path, result

    def test_hash_mismatch_is_rejected_even_when_size_and_mtime_match(self):
        path, result = self.source()
        data = bytearray(path.read_bytes())
        data[-5] ^= 1
        path.write_bytes(data)
        os.utime(path, ns=(path.stat().st_atime_ns, result.mtime_ns))
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            module.render_preview(result)

    def test_missing_original_uses_hash_verified_recorded_output(self):
        path, result = self.source()
        data = path.read_bytes()
        output = self.root / "moved.png"
        output.write_bytes(data)
        result.status = "moved"
        result.destinations = [str(output)]
        result.output_details = [{"path": str(output), "status": "verified", "sha256": result.sha256}]
        path.unlink()
        preview = module.render_preview(result)
        self.assertEqual(str(output), preview["info"]["source"])
        self.assertEqual(data, output.read_bytes())
        self.assertFalse(path.exists())

    def test_changed_recorded_output_is_skipped_for_another_verified_copy(self):
        path, result = self.source()
        data = path.read_bytes()
        broken, good = self.root / "broken.png", self.root / "good.png"
        broken.write_bytes(b"X" * len(data))
        good.write_bytes(data)
        result.destinations = [str(broken), str(good)]
        result.output_details = [{"path": str(p), "status": "verified", "sha256": result.sha256}
                                 for p in (broken, good)]
        path.unlink()
        self.assertEqual(good, module.surviving_path(result))

    def test_unrecorded_or_unverified_fallback_is_rejected(self):
        path, result = self.source()
        output = self.root / "unverified.png"
        output.write_bytes(path.read_bytes())
        result.output_details = [{"path": str(output), "status": "verified", "sha256": result.sha256}]
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            module.render_preview(result)

    def test_missing_hash_is_not_a_preview_permission(self):
        _, result = self.source()
        result.sha256 = ""
        with self.assertRaisesRegex(ValueError, "hash"):
            module.render_preview(result)

    def test_exif_orientation_matches_displayed_pixel_coordinates(self):
        path, result = self.source("rotated.jpg", size=(40, 20), orientation=6)
        before = path.read_bytes()
        preview = module.render_preview(result)
        self.assertEqual((20, 40), (preview["info"]["width"], preview["info"]["height"]))
        with Image.open(io.BytesIO(preview["png"])) as actual, Image.open(io.BytesIO(before)) as original:
            expected = ImageOps.exif_transpose(original).convert("RGB")
            self.assertEqual(expected.size, actual.size)
            self.assertEqual(expected.tobytes(), actual.tobytes())
        self.assertEqual(before, path.read_bytes())

    def test_fit_preserves_aspect_and_does_not_enlarge_tiny_images(self):
        self.assertEqual((268, 134), module._fit(2000, 1000))
        self.assertEqual((45, 180), module._fit(100, 400))
        self.assertEqual((1, 1), module._fit(1, 1))
        with self.assertRaises(ValueError):
            module._fit(0, 180)
        with self.assertRaises(ValueError):
            module._fit(float("nan"), 180)

    def test_large_preview_is_fitted_and_does_not_write_a_cache(self):
        path, result = self.source(size=(600, 400))
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        preview = module.render_preview(result)
        with Image.open(io.BytesIO(preview["png"])) as image:
            self.assertLessEqual(image.width, 268)
            self.assertLessEqual(image.height, 180)
            self.assertAlmostEqual(image.width / image.height, 1.5, delta=0.01)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_video_hash_is_checked_before_any_decoder_is_started(self):
        path, result = self.source("source.mp4", data=b"synthetic video")
        result.media_type = "video"
        path.write_bytes(b"changed!!!video")
        os.utime(path, ns=(path.stat().st_atime_ns, result.mtime_ns))
        with patch.object(module, "_run_media") as run:
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                module.render_preview(result)
            run.assert_not_called()

    def test_video_layout_uses_rotation_sar_and_actual_duration(self):
        _, result = self.source("source.mp4", data=b"synthetic video")
        result.media_type, result.duration_s, result.best_timestamp_s = "video", 999.0, 80.0
        metadata = {"streams": [{"width": 1920, "height": 1080, "sample_aspect_ratio": "1:1",
                                  "side_data_list": [{"rotation": -90}]}], "format": {"duration": "2.0"}}
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            value = json.dumps(metadata).encode() if command[0] == "ffprobe" else png_bytes((100, 180))
            return subprocess.CompletedProcess(command, 0, value, b"")
        with patch.object(module, "_run_media", side_effect=run):
            preview = module.render_preview(result)
        self.assertEqual((1080, 1920), (preview["info"]["display_width"], preview["info"]["display_height"]))
        self.assertEqual(1.9, preview["info"]["timestamp_s"])
        self.assertEqual("1.9", calls[1][calls[1].index("-ss") + 1])
        self.assertIn("100:180", calls[1][calls[1].index("-vf") + 1])
        self.assertEqual((1440, 576), module._video_display_size({"width": 720, "height": 576, "sample_aspect_ratio": "2:1"}))

    def test_video_preview_uses_accepted_semantic_match_without_inventing_geometry(self):
        _, result = self.source("semantic.mp4", data=b"synthetic video")
        result.media_type, result.duration_s = "video", 60.0
        result.best_timestamp_s, result.best_match_timestamp_s = 7.0, 42.0
        result.categories = ["BUTTOCKS_COVERED"]
        result.detections = [{"class": "BUTTOCKS_COVERED", "source": "siglip2",
                              "scope": "image", "score_kind": "logit_margin",
                              "raw_margin": 3.25, "box": [], "accepted": True}]
        metadata = {"streams": [{"width": 24, "height": 16}], "format": {"duration": "60.0"}}
        cases = [
            (False, ["BUTTOCKS_COVERED"], 3.25, 42.0),
            (True, ["BUTTOCKS_COVERED"], 3.25, 7.0),
            (False, ["FACE_FEMALE"], 3.25, 7.0),
            (False, ["BUTTOCKS_COVERED"], 0.0, 7.0),
            (False, ["BUTTOCKS_COVERED"], -1.0, 7.0),
        ]
        for geometry, categories, margin, expected in cases:
            with self.subTest(geometry=geometry, categories=categories, margin=margin):
                result.geometry_available, result.categories = geometry, categories
                result.detections[0]["raw_margin"] = margin
                calls = []

                def run(command, **kwargs):
                    calls.append(command)
                    value = json.dumps(metadata).encode() if command[0] == "ffprobe" else png_bytes()
                    return subprocess.CompletedProcess(command, 0, value, b"")

                with patch.object(module, "_run_media", side_effect=run):
                    preview = module.render_preview(result)
                self.assertEqual(expected, preview["info"]["timestamp_s"])
                self.assertEqual(str(expected), calls[1][calls[1].index("-ss") + 1])
                self.assertEqual([], result.detections[0]["box"])
                self.assertNotIn("score", result.detections[0])
                self.assertEqual(geometry, result.geometry_available)

    def test_unexpected_cuda_failure_is_not_retried_on_cpu(self):
        _, result = self.source("source.jpg")
        with patch("torch.cuda.is_available", return_value=True), patch("torch.cuda.get_device_name", return_value="test CUDA"), \
                patch("torchvision.io.decode_jpeg", side_effect=RuntimeError("CUDA illegal memory access")) as decode:
            with self.assertRaisesRegex(RuntimeError, "illegal memory access"):
                module.render_preview(result)
            decode.assert_called_once()

    def test_cpu_path_is_explicit_when_cuda_is_absent(self):
        _, result = self.source()
        with patch("torch.cuda.is_available", return_value=False):
            preview = module.render_preview(result)
        self.assertEqual("cpu", preview["info"]["resize_device"])
        self.assertFalse(preview["info"]["cuda_verified"])

    @unittest.skipUnless(torch.cuda.is_available(), "CUDA unavailable on this test runner")
    def test_observed_cuda_jpeg_decode_and_resize_on_tiny_synthetic_image(self):
        _, result = self.source("gpu.jpg", size=(480, 240))
        preview = module.render_preview(result)
        self.assertEqual("CUDA JPEG", preview["info"]["decode"])
        self.assertEqual("cuda:0", preview["info"]["image_device"])
        self.assertEqual("cuda:0", preview["info"]["resize_device"])
        self.assertTrue(preview["info"]["cuda_verified"])
        self.assertEqual("CPU PNG codec", preview["info"]["encode"])

    def test_cancel_before_read_and_decode(self):
        _, result = self.source()
        cancel = threading.Event()
        cancel.set()
        with patch.object(module, "_open_regular") as opened:
            with self.assertRaises(InterruptedError):
                module.render_preview(result, cancel_event=cancel)
            opened.assert_not_called()

    def test_dependency_preparation_does_not_query_or_initialize_cuda(self):
        with patch.object(module, "_RUNTIME", None), \
                patch("torch.cuda.is_available", side_effect=AssertionError("Unexpected CUDA query")), \
                patch("torch.cuda.init", side_effect=AssertionError("Unexpected CUDA initialization")):
            module.prepare_preview_runtime()
            first = module._RUNTIME
            module.prepare_preview_runtime()
            self.assertIs(first, module._RUNTIME)
            self.assertIs(torch, first["torch"])

    def test_cold_background_preparation_fails_before_importing_dependencies(self):
        errors = []
        def prepare():
            try:
                module.prepare_preview_runtime()
            except Exception as error:
                errors.append(error)
        with patch.object(module, "_RUNTIME", None):
            worker = threading.Thread(target=prepare)
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertIsNone(module._RUNTIME)
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn("main thread", str(errors[0]))


if __name__ == "__main__":
    unittest.main()
