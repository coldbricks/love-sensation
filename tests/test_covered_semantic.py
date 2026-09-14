"""Public assets and synthetic tensors only; no model downloads or private fixtures."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from platinum_sorter import covered_semantic as semantic


class SemanticAssetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="covered-model-test-")
        self.root = Path(self.temporary.name)
        self.content = b"verified public test asset"
        self.assets = (("config.json", len(self.content), hashlib.sha256(self.content).hexdigest()),)

    def tearDown(self):
        self.temporary.cleanup()

    def test_verified_local_assets_never_need_network(self):
        directory = self.root / semantic.MODEL_SUBDIRECTORY
        directory.mkdir()
        (directory / "config.json").write_bytes(self.content)
        with patch.object(semantic, "ASSETS", self.assets), patch.object(semantic.urllib.request, "urlopen") as network:
            self.assertEqual(directory, semantic.ensure_semantic_model(self.root, download=False))
        network.assert_not_called()

    def test_offline_missing_asset_fails_without_downloading(self):
        with patch.object(semantic, "ASSETS", self.assets), patch.object(semantic.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(RuntimeError, "missing or invalid"):
                semantic.ensure_semantic_model(self.root, download=False)
        network.assert_not_called()

    def test_bad_download_cannot_replace_existing_asset(self):
        directory = self.root / semantic.MODEL_SUBDIRECTORY
        directory.mkdir()
        path = directory / "config.json"
        path.write_bytes(b"existing unverified content")
        with patch.object(semantic, "ASSETS", self.assets), patch.object(semantic.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong")):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                semantic.ensure_semantic_model(self.root)
        self.assertEqual(b"existing unverified content", path.read_bytes())
        self.assertEqual(["config.json"], [entry.name for entry in directory.iterdir()])

    def test_download_url_is_pinned_and_verified_before_install(self):
        with patch.object(semantic, "ASSETS", self.assets), patch.object(semantic.urllib.request, "urlopen", return_value=io.BytesIO(self.content)) as network:
            directory = semantic.ensure_semantic_model(self.root)
        request = network.call_args.args[0]
        self.assertEqual(f"https://huggingface.co/{semantic.MODEL_ID}/resolve/{semantic.MODEL_REVISION}/config.json", request.full_url)
        self.assertEqual(self.content, (directory / "config.json").read_bytes())

    def test_fingerprint_changes_for_inference_policy_changes(self):
        options = dict(precision="FP16", processor_config={"size": 512}, runtime_versions={"transformers": "5.7.0"})
        before = semantic.semantic_fingerprint(**options)
        for key, value in (("precision", "FP32"), ("processor_config", {"size": 384}), ("runtime_versions", {"transformers": "next"})):
            with self.subTest(key=key):
                self.assertNotEqual(before, semantic.semantic_fingerprint(**(options | {key: value})))
        with patch.object(semantic, "PROMPTS", semantic.PROMPTS + (("negative", "A different view."),)):
            self.assertNotEqual(before, semantic.semantic_fingerprint(**options))
        with patch.object(semantic, "ASSETS", self.assets):
            self.assertNotEqual(before, semantic.semantic_fingerprint(**options))


class SemanticRecordTests(unittest.TestCase):
    def test_negative_and_positive_evidence_have_no_confidence_or_geometry(self):
        for positive, negative in ((-8, -4), (2, -3), (0, 0)):
            with self.subTest(positive=positive, negative=negative):
                result = semantic.evidence_record(positive, negative)
                self.assertNotIn("score", result)
                self.assertEqual([], result["box"])
                self.assertEqual("image", result["scope"])
                self.assertEqual("logit_margin", result["score_kind"])
                self.assertEqual(positive - negative, result["raw_margin"])
                self.assertTrue(result["model_revision"])
                self.assertTrue(result["prompt_revision"])

    def test_nonfinite_evidence_is_an_error_not_an_empty_detection(self):
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                semantic.evidence_record(invalid, 0)


class CombinedDetectorTests(unittest.TestCase):
    def setUp(self):
        import torch
        import torch.nn.functional as functional
        from platinum_sorter.detector import GpuDetector
        self.torch = torch
        detector = GpuDetector.__new__(GpuDetector)
        detector.torch, detector.functional = torch, functional
        detector.cuda = torch.cuda.is_available()
        detector.device = torch.device("cuda:0" if detector.cuda else "cpu")
        detector.dtype = torch.float16 if detector.cuda else torch.float32
        self.reads, self.semantic_devices, self.semantic_groups = [], [], []

        def decode(source):
            self.reads.append(source)
            if source == b"bad codec":
                raise ValueError("Synthetic codec failure")
            return torch.full((3, 4, 4), source[0], device=detector.device, dtype=torch.uint8)

        def resize(image):
            return image.to(detector.dtype).div(255), (4, 4, 1.0)

        def predict(batch):
            return [torch.tensor([[1, 1, 3, 3, .75, 17]], device=detector.device) for _ in batch]

        class FakeSemantic:
            def predict(inner, images):
                self.semantic_groups.append(len(images))
                self.semantic_devices.extend(image.device.type for image in images)
                return [semantic.evidence_record(float(image[0, 0, 0].item()), 1) for image in images]

        detector._decode, detector._resize, detector._predict = decode, resize, predict
        detector.semantic = FakeSemantic()
        self.detector = detector

    def test_shared_snapshot_decode_preserves_detector_records_and_bounds_batches(self):
        contents = [b"\x00", b"\x02", b"\x03", b"\x04", b"\x05"]
        outputs = self.detector.detect_encoded_batch(contents)
        self.assertEqual(contents, self.reads)
        self.assertEqual([2, 2, 1], self.semantic_groups)
        self.assertEqual([self.detector.device.type] * 5, self.semantic_devices)
        for output in outputs:
            self.assertEqual({"class": "BUTTOCKS_COVERED", "score": .75, "box": [1, 1, 2, 2]}, output[0])
            self.assertNotIn("score", output[1])
            self.assertEqual((4, 4), (output.width, output.height))
        self.assertLess(outputs[0][1]["raw_margin"], 0)

    def test_decode_failure_does_not_omit_or_shift_other_images(self):
        outputs = self.detector.detect_encoded_batch([b"bad codec", b"\x02", b"\x03"])
        self.assertIsInstance(outputs[0], ValueError)
        self.assertEqual([1, 2], [output[1]["raw_margin"] for output in outputs[1:]])

    def test_semantic_failure_is_isolated_and_not_cached_as_success(self):
        original = self.detector.semantic.predict
        def sometimes_fails(images):
            if any(image[0, 0, 0].item() == 0 for image in images):
                raise ValueError("Synthetic semantic failure")
            return original(images)
        self.detector.semantic.predict = sometimes_fails
        outputs = self.detector.detect_encoded_batch([b"\x00", b"\x02"])
        self.assertIsInstance(outputs[0], RuntimeError)
        self.assertIn("Covered-body inference failed", str(outputs[0]))
        self.assertEqual(1, outputs[1][1]["raw_margin"])

    def test_oom_retries_smaller_batches_on_same_device(self):
        original = self.detector.semantic.predict
        def limited_memory(images):
            if len(images) > 1:
                raise self.torch.cuda.OutOfMemoryError("Synthetic allocation limit")
            return original(images)
        self.detector.semantic.predict = limited_memory
        outputs = self.detector.detect_encoded_batch([b"\x02", b"\x03"])
        self.assertTrue(all(isinstance(output, list) for output in outputs))
        self.assertEqual([self.detector.device.type] * 2, self.semantic_devices)


if __name__ == "__main__":
    unittest.main()
