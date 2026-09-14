"""Local object detections plus covered-rear evidence; numerical work defaults to CUDA."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import time
import urllib.request

from .contracts import DetectionResult
from .covered_semantic import CoveredSemantic, ensure_semantic_model

LABELS = [
    "FEMALE_GENITALIA_COVERED", "FACE_FEMALE", "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED", "MALE_BREAST_EXPOSED",
    "ANUS_EXPOSED", "FEET_EXPOSED", "BELLY_COVERED", "FEET_COVERED",
    "ARMPITS_COVERED", "ARMPITS_EXPOSED", "FACE_MALE", "BELLY_EXPOSED",
    "MALE_GENITALIA_EXPOSED", "ANUS_COVERED", "FEMALE_BREAST_COVERED",
    "BUTTOCKS_COVERED",
]
MODEL_SHA256 = "e6d7cddecc4417ff62db5b92c1c9f5d0d7b0f92e6cfd562120aea59a6ec3af3f"
MODEL_URL = "https://api.github.com/repos/notAI-tech/NudeNet/releases/assets/176832117"
MODEL_SIZE = 52023681
INFERENCE_GROUP_SIZE = 2


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ensure_model(model_dir: Path, emit=lambda event: None) -> Path:
    model_dir.mkdir(parents=True, exist_ok=True)
    path = model_dir / "640m.pt"
    if path.is_file() and file_hash(path) == MODEL_SHA256:
        return path
    emit({"type": "status", "message": "Downloading the official 640m model (52 MB)..."})
    request = urllib.request.Request(MODEL_URL, headers={
        "Accept": "application/octet-stream", "User-Agent": "LoveSensation/1.0",
    })
    temporary = path.with_suffix(".download")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if "octet-stream" not in response.headers.get("Content-Type", ""):
                raise RuntimeError("Model server returned a web page instead of weights.")
            with temporary.open("wb") as destination:
                shutil.copyfileobj(response, destination)
        if temporary.stat().st_size != MODEL_SIZE or file_hash(temporary) != MODEL_SHA256:
            raise RuntimeError("Downloaded model failed its size/SHA-256 check.")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


class GpuDetector:
    def __init__(self, model_dir: Path, emit=lambda event: None):
        self.emit = emit
        self.model_dir = Path(model_dir)
        os.environ.setdefault("YOLO_CONFIG_DIR", str(self.model_dir.parent / "data" / "ultralytics"))
        os.environ["YOLO_OFFLINE"] = "true"
        os.environ["YOLO_AUTOINSTALL"] = "false"
        # CUDA imports stay out of the GUI startup path.
        import torch
        import torch.nn.functional as functional
        from ultralytics import YOLO, settings
        from ultralytics.utils.nms import non_max_suppression

        settings.update({"sync": False})

        self.torch, self.functional, self.nms = torch, functional, non_max_suppression
        self.cuda = torch.cuda.is_available()
        self.device = torch.device("cuda:0" if self.cuda else "cpu")
        self.dtype = torch.float16 if self.cuda else torch.float32
        self._cuda_jpeg = None
        # Complete public asset provisioning before decoding any private input.
        path = ensure_model(self.model_dir, emit)
        semantic_directory = ensure_semantic_model(self.model_dir, emit)
        emit({"type": "status", "message": "Loading 640m and verifying the compute device..."})
        wrapper = YOLO(str(path), task="detect")
        actual_labels = [wrapper.names[index] for index in range(len(wrapper.names))]
        if actual_labels != LABELS:
            raise RuntimeError("Model category mapping differs from the verified NudeNet model.")
        self.model = wrapper.model.fuse(verbose=False).to(self.device).eval()
        self.model = self.model.half() if self.cuda else self.model.float()
        self.info = {
            "device": str(self.device),
            "device_name": torch.cuda.get_device_name(0) if self.cuda else "CPU (CUDA unavailable)",
            "model": "NudeNet 640m", "precision": "FP16" if self.cuda else "FP32",
            "backend": "PyTorch", "torch_version": torch.__version__,
            "cuda_available": self.cuda, "cuda_verified": False,
            "model_sha256": MODEL_SHA256,
            "parameters": sum(parameter.numel() for parameter in self.model.parameters()),
            "preprocessing": str(self.device), "postprocessing": str(self.device),
            "decode": "CUDA JPEG when available; CPU codecs otherwise",
        }
        # Observe a real convolution output, not just a CUDA availability flag.
        convolution = next(module for module in self.model.modules() if isinstance(module, torch.nn.Conv2d))
        observed = {}
        def record_device(module, arguments, output):
            observed.update(device=str(output.device), dtype=str(output.dtype))
        hook = convolution.register_forward_hook(record_device)
        try:
            with torch.inference_mode():
                self._predict(torch.zeros(1, 3, 640, 640, device=self.device, dtype=self.dtype))
            if self.cuda:
                torch.cuda.synchronize(self.device)
        finally:
            hook.remove()
        if self.cuda and observed.get("device") != "cuda:0":
            raise RuntimeError("GPU was requested but the warm-up convolution did not run on CUDA.")
        self.info["cuda_verified"] = self.cuda and observed.get("device") == "cuda:0"
        self.info["verified_convolution"] = observed
        self.semantic = CoveredSemantic(semantic_directory, self.device, emit)
        self.info["model"] = "NudeNet 640m + SigLIP2 covered-body evidence"
        self.info["covered_semantic"] = dict(self.semantic.info)
        self.info["inference_group_size"] = INFERENCE_GROUP_SIZE
        self.fingerprint = f"640m:{MODEL_SHA256}:torch-square-v1:{self.info['precision']}|{self.semantic.fingerprint}"
        self.emit({"type": "device", "info": dict(self.info)})
        logging.info("Detector verified: %s", self.info)
        print(json.dumps(self.info), flush=True)

    def _predict(self, batch):
        prediction = self.model(batch)
        return self.nms(
            prediction, conf_thres=0.05, iou_thres=0.45, nc=len(LABELS),
            agnostic=False, max_det=300, max_time_img=2.0,
        )

    def _prepare(self, source: Path | bytes):
        return self._resize(self._decode(source))

    def _decode(self, source: Path | bytes):
        import numpy as np
        from PIL import Image, ImageOps
        torch = self.torch
        tensor = None
        encoded_source = source if isinstance(source, bytes) else None
        with Image.open(io.BytesIO(encoded_source) if encoded_source is not None else source) as image:
            if getattr(image, "n_frames", 1) > 1:
                raise ValueError("Animated or multi-page images require frame selection; skipped.")
            orientation = image.getexif().get(274, 1)
            if self.cuda and self._cuda_jpeg is not False and image.format == "JPEG" and orientation == 1:
                try:
                    from torchvision.io import decode_jpeg, ImageReadMode
                    encoded = torch.frombuffer(bytearray(encoded_source if encoded_source is not None else source.read_bytes()), dtype=torch.uint8)
                    tensor = decode_jpeg(encoded, mode=ImageReadMode.RGB, device=self.device)
                    self._cuda_jpeg = True
                    self.info["decode"] = "CUDA JPEG; CPU for other codecs/EXIF rotation"
                except (RuntimeError, NotImplementedError, OSError, AttributeError) as error:
                    backend_missing = isinstance(error, (NotImplementedError, AttributeError)) or any(
                        phrase in str(error).lower() for phrase in
                        ("not compiled", "no such operator", "couldn't load custom c++ ops", "not available in this build")
                    )
                    if backend_missing:
                        self._cuda_jpeg = False
                        self.info["decode"] = "CPU codecs (CUDA JPEG decoder unavailable in this build)"
                        self.emit({"type": "status", "message": "Using CPU image decoding; resizing and inference remain on the GPU."})
                    # An unsupported/corrupt individual JPEG must not disable CUDA
                    # decoding for every subsequent image in the library.
            if tensor is None:
                rgb = ImageOps.exif_transpose(image).convert("RGB")
                array = np.asarray(rgb).copy()
                tensor = torch.from_numpy(array).permute(2, 0, 1).to(self.device)
        return tensor

    def _resize(self, tensor):
        height, width = tensor.shape[-2:]
        side = max(height, width)
        # Preserve NudeNet's top-left square padding. Resize and normalization stay on device.
        padded = self.functional.pad(tensor, (0, side - width, 0, side - height))
        resized = self.functional.interpolate(
            padded.unsqueeze(0).float(), size=(640, 640), mode="bilinear", align_corners=False,
        )
        return resized.squeeze(0).to(self.dtype).div_(255.0), (width, height, side / 640.0)

    def detect_batch(self, paths: list[Path]) -> list[list[dict] | Exception]:
        return self._detect_batch([Path(path) for path in paths])

    def detect_encoded_batch(self, images: list[bytes]) -> list[list[dict] | Exception]:
        """Decode the exact verified source snapshot without reopening its path."""
        return self._detect_batch(images)

    def _detect_batch(self, sources: list[Path | bytes]) -> list[list[dict] | Exception]:
        # Keep decoded full-resolution tensors bounded as well as model batches.
        outputs = []
        for start in range(0, len(sources), INFERENCE_GROUP_SIZE):
            outputs.extend(self._detect_with_retry(sources[start:start + INFERENCE_GROUP_SIZE]))
        return outputs

    def _detect_with_retry(self, sources: list[Path | bytes]) -> list[list[dict] | Exception]:
        # Catch outside _detect_group so its intermediate tensors are released
        # before retrying. A failed semantic stage never becomes a false empty result.
        try:
            return self._detect_group(sources)
        except self.torch.cuda.OutOfMemoryError:
            message = "GPU memory exhausted during combined image inference."
            memory_error = True
        except Exception as error:
            message = str(error)
            memory_error = False
        if memory_error and self.cuda:
            self.torch.cuda.empty_cache()
        if len(sources) == 1:
            return [RuntimeError(message)]
        middle = len(sources) // 2
        return self._detect_with_retry(sources[:middle]) + self._detect_with_retry(sources[middle:])

    def _detect_group(self, sources: list[Path | bytes]) -> list[list[dict] | Exception]:
        torch = self.torch
        outputs: list[list[dict] | Exception] = [RuntimeError("Image not processed") for _ in sources]
        valid, prepared, metadata, decoded = [], [], [], []
        with torch.inference_mode():
            for index, source in enumerate(sources):
                try:
                    original = self._decode(source)
                    tensor, dimensions = self._resize(original)
                    valid.append(index)
                    prepared.append(tensor)
                    metadata.append(dimensions)
                    decoded.append(original)
                except torch.cuda.OutOfMemoryError:
                    raise
                except Exception as error:
                    outputs[index] = error
            if not prepared:
                return outputs
            predictions = self._predict(torch.stack(prepared))
            try:
                semantic_records = self.semantic.predict(decoded)
            except torch.cuda.OutOfMemoryError:
                raise
            except Exception as error:
                raise RuntimeError(f"Covered-body inference failed: {error}") from error
            if len(predictions) != len(valid) or len(semantic_records) != len(valid):
                raise RuntimeError("Combined inference returned an incomplete image batch.")
            for position, detections in enumerate(predictions):
                width, height, scale = metadata[position]
                # Preserve NudeNet's categories, confidence values, and real boxes.
                detections = detections.float()
                detections[:, :4] *= scale
                detections[:, [0, 2]] = detections[:, [0, 2]].clamp(0, width)
                detections[:, [1, 3]] = detections[:, [1, 3]].clamp(0, height)
                records = []
                for x1, y1, x2, y2, score, category in detections.cpu().tolist():
                    if x2 > x1 and y2 > y1:
                        records.append({"class": LABELS[int(category)], "score": float(score),
                                        "box": [round(x1), round(y1), round(x2-x1), round(y2-y1)]})
                records.append(semantic_records[position])
                outputs[valid[position]] = DetectionResult(records, width=width, height=height)
            if self.cuda:
                torch.cuda.synchronize(self.device)
        return outputs
