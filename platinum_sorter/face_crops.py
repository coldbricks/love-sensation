"""Copy-only face crops from a completed, hash-verified analysis.

The service accepts only the two face labels already present in saved detector
results. Caller-supplied categories or rectangles are never crop instructions.
Torch and CUDA are initialized lazily, away from the GUI thread.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .contracts import ScanReport
from .engine import (
    _Cancelled, _append_jsonl, _assert_no_links, _atomic_json, _open_regular,
    _reserve_destination, _safe_category, _signature, _within,
)

_FACE_LABELS = frozenset({"FACE_FEMALE", "FACE_MALE"})


def _padding(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 0.30:
        raise ValueError("Face padding must be between 0 and 0.30.")
    return float(value)


def _bounds(box: list[float], width: int, height: int, padding: float) -> tuple[int, int, int, int]:
    x, y, w, h = box
    left = max(0, min(width, math.floor(x - w * padding)))
    top = max(0, min(height, math.floor(y - h * padding)))
    right = max(0, min(width, math.ceil(x + w + w * padding)))
    bottom = max(0, min(height, math.ceil(y + h + h * padding)))
    if left >= right or top >= bottom:
        raise ValueError("Saved face box lies outside the image.")
    return left, top, right, bottom


class FaceCropService:
    def __init__(self, report: ScanReport):
        if not report.analysis_complete:
            raise ValueError("Finish image analysis before exporting face crops.")
        threshold = report.options.threshold
        if not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("The saved confidence threshold is invalid.")
        self.report = copy.deepcopy(report)
        self.source_root = Path(os.path.abspath(report.options.source))
        self.output_root = Path(os.path.abspath(report.options.destination))
        self._records: dict[str, dict] = {}
        self._rows: dict[str, object] = {}
        self._torch = None
        self._cuda_jpeg = None
        self.info = {"device": "not initialized", "backend": "PyTorch", "model_reloaded": False,
                     "decode": "not initialized", "encode": "CPU PNG codec"}
        for row_index, row in enumerate(self.report.results):
            if not row.sha256:
                continue
            face_index = 0
            for detection_index, detection in enumerate(row.detections):
                try:
                    score = float(detection["score"])
                    box = [float(number) for number in detection.get("box", [])]
                    if (detection.get("class") not in _FACE_LABELS or not math.isfinite(score)
                            or not threshold <= score <= 1 or len(box) != 4
                            or not all(math.isfinite(number) for number in box) or box[2] <= 0 or box[3] <= 0):
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
                face_index += 1
                identity = f"{self.report.run_id}:{row_index}:{detection_index}"
                candidate = {
                    "id": identity, "source": row.source, "relative_path": row.relative_path,
                    "face_index": face_index, "class": detection["class"], "score": score, "box": box,
                    "sha256": row.sha256, "size": row.size, "mtime_ns": row.mtime_ns,
                }
                self._records[identity] = candidate
                self._rows[identity] = row

    def candidates(self) -> list[dict]:
        return copy.deepcopy(list(self._records.values()))

    def _candidate(self, candidate: dict) -> dict:
        if not isinstance(candidate, dict):
            raise ValueError("Select a face from this analysis.")
        expected = self._records.get(candidate.get("id"))
        if expected is None or candidate != expected:
            raise ValueError("Face candidate differs from the saved analysis; refresh the face list.")
        return expected

    def _initialize(self) -> None:
        if self._torch is not None:
            return
        import torch
        self._torch = torch
        self._cuda = torch.cuda.is_available()
        self._device = torch.device("cuda:0" if self._cuda else "cpu")
        self.info.update(device=str(self._device),
                         device_name=torch.cuda.get_device_name(0) if self._cuda else "CPU (CUDA unavailable)",
                         cuda_available=self._cuda, cuda_verified=False,
                         torch_version=torch.__version__, crop_device=str(self._device),
                         resize_device=str(self._device),
                         decode="CUDA JPEG when supported; CPU other codecs/EXIF rotation")
        print("Face crop runtime: " + json.dumps(self.info), flush=True)

    def _source_paths(self, candidate: dict) -> list[Path]:
        row = self._rows[candidate["id"]]
        relative = Path(row.relative_path)
        source = Path(os.path.abspath(row.source))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts or relative.drive:
            raise ValueError("Analysis contains an unsafe relative source path.")
        if source != self.source_root / relative or not _within(source, self.source_root):
            raise ValueError("Analysis source is outside the original source folder.")
        _assert_no_links(source, allow_missing=True)
        if source.exists():
            return [source]
        if self.report.options.operation != "move" or row.status not in {"moved", "removing_source"}:
            raise FileNotFoundError("Original image is missing; analyze its current folder again.")
        _assert_no_links(self.output_root, allow_missing=True)
        destinations = []
        for detail in row.output_details:
            if detail.get("status") != "verified" or detail.get("category") not in row.categories:
                continue
            destination = Path(os.path.abspath(detail.get("path", "")))
            expected_parent = (self.output_root / _safe_category(detail["category"]) / relative).parent
            if (str(destination) not in row.destinations or not _within(destination, self.output_root)
                    or destination.parent != expected_parent):
                raise ValueError("Moved-image record points outside its expected output folder.")
            _assert_no_links(destination, allow_missing=True)
            destinations.append(destination)
        if not destinations:
            raise FileNotFoundError("Moved image has no verified full-image copy in this analysis.")
        return destinations

    def _read_source(self, candidate: dict, cancel: threading.Event) -> tuple[bytes, str]:
        failures = []
        for path in self._source_paths(candidate):
            try:
                digest = hashlib.sha256()
                data = bytearray()
                with _open_regular(path) as stream:
                    before = os.fstat(stream.fileno())
                    while True:
                        if cancel.is_set():
                            raise _Cancelled("Face export cancelled while reading an image.")
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        data.extend(chunk)
                        digest.update(chunk)
                    after = os.fstat(stream.fileno())
                current = path.lstat()
                signature = lambda value: (value.st_size, value.st_mtime_ns, value.st_ino)
                if signature(before) != signature(after) or signature(after) != signature(current):
                    raise ValueError("Image changed while reading; analyze again.")
                if len(data) != candidate["size"] or digest.hexdigest() != candidate["sha256"]:
                    raise ValueError("Image SHA-256 differs from the saved analysis; analyze again.")
                return bytes(data), str(path)
            except _Cancelled:
                raise
            except Exception as error:
                failures.append(f"{path}: {error}")
        raise ValueError("; ".join(failures))

    def _decode(self, data: bytes):
        self._initialize()
        import numpy as np
        from PIL import Image, ImageOps
        torch = self._torch
        tensor = None
        with Image.open(io.BytesIO(data)) as image:
            if getattr(image, "n_frames", 1) > 1:
                raise ValueError("Animated or multi-page images require frame selection; skipped.")
            orientation = image.getexif().get(274, 1)
            if self._cuda and self._cuda_jpeg is not False and image.format == "JPEG" and orientation == 1:
                try:
                    from torchvision.io import ImageReadMode, decode_jpeg
                    encoded = torch.frombuffer(bytearray(data), dtype=torch.uint8)
                    tensor = decode_jpeg(encoded, mode=ImageReadMode.RGB, device=self._device)
                    self._cuda_jpeg = True
                    self.info["decode"] = "CUDA JPEG"
                except torch.cuda.OutOfMemoryError:
                    raise
                except (RuntimeError, NotImplementedError, OSError, AttributeError) as error:
                    if isinstance(error, (NotImplementedError, AttributeError)) or any(
                        phrase in str(error).lower() for phrase in
                        ("not compiled", "no such operator", "couldn't load custom c++ ops", "not available in this build")
                    ):
                        self._cuda_jpeg = False
                    self.info["decode"] = "CPU JPEG codec (CUDA codec unavailable for this image)"
            if tensor is None:
                rgb = ImageOps.exif_transpose(image).convert("RGB")
                tensor = torch.from_numpy(np.asarray(rgb).copy()).permute(2, 0, 1).to(self._device)
                if not self._cuda or image.format != "JPEG" or orientation != 1:
                    self.info["decode"] = "CPU image codec/EXIF normalization"
        if self._cuda and tensor.device.type != "cuda":
            raise RuntimeError("Face crop image failed to reach CUDA.")
        self.info["image_device"] = str(tensor.device)
        self.info["cuda_verified"] = self._cuda and tensor.device.type == "cuda"
        return tensor

    def _crop(self, tensor, candidate: dict, padding: float):
        height, width = tensor.shape[-2:]
        bounds = _bounds(candidate["box"], width, height, padding)
        left, top, right, bottom = bounds
        with self._torch.inference_mode():
            crop = tensor[:, top:bottom, left:right].contiguous()
        self.info["crop_device"] = str(crop.device)
        return crop, bounds

    def _png(self, tensor, *, preview: bool = False) -> bytes:
        from PIL import Image
        torch = self._torch
        with torch.inference_mode():
            if preview and max(tensor.shape[-2:]) > 480:
                import torch.nn.functional as functional
                height, width = tensor.shape[-2:]
                scale = 480 / max(height, width)
                dimensions = (max(1, round(height * scale)), max(1, round(width * scale)))
                tensor = functional.interpolate(
                    tensor.unsqueeze(0).to(torch.float16 if self._cuda else torch.float32),
                    size=dimensions, mode="bilinear", align_corners=False,
                ).squeeze(0).round().clamp_(0, 255).to(torch.uint8)
                self.info["resize_device"] = str(tensor.device)
            array = tensor.permute(1, 2, 0).contiguous().cpu().numpy()
        output = io.BytesIO()
        Image.fromarray(array).save(output, format="PNG")
        return output.getvalue()

    def preview(self, candidate: dict, padding: float = 0.10) -> dict:
        candidate = self._candidate(candidate)
        padding = _padding(padding)
        data, actual_source = self._read_source(candidate, threading.Event())
        tensor = self._decode(data)
        crop, bounds = self._crop(tensor, candidate, padding)
        png = self._png(crop, preview=True)
        return {"png": png, "info": dict(self.info), "width": bounds[2] - bounds[0],
                "height": bounds[3] - bounds[1], "bounds": list(bounds), "read_source": actual_source}

    def export(self, candidates: list[dict], destination: str | Path, padding: float = 0.10,
               emit=lambda event: None, cancel: threading.Event | None = None) -> dict:
        padding = _padding(padding)
        cancel = cancel if cancel is not None else threading.Event()
        selected = []
        seen = set()
        for candidate in candidates:
            record = self._candidate(candidate)
            if record["id"] not in seen:
                selected.append(record)
                seen.add(record["id"])
        if not str(destination).strip():
            raise ValueError("Choose a face crop output folder.")
        destination = Path(os.path.abspath(Path(destination).expanduser()))
        _assert_no_links(destination, allow_missing=True)
        _assert_no_links(self.source_root, allow_missing=True)
        if _within(destination, self.source_root) or _within(self.source_root, destination):
            raise ValueError("Face crop output and original source must be separate folders.")
        if destination.exists() and not destination.is_dir():
            raise ValueError("Face crop output must be a folder.")
        export_id = uuid.uuid4().hex
        manifest = destination / f"face-crops-{export_id}.json"
        journal = manifest.with_suffix(".events.jsonl")
        state = {"version": 1, "kind": "face-crops", "run_id": self.report.run_id,
                 "export_id": export_id, "padding": padding, "destination": str(destination),
                 "started_at": datetime.now(timezone.utc).isoformat(), "completed_at": None,
                 "exported": 0, "errors": [], "outputs": [], "cancelled": False,
                 "manifest_path": str(manifest), "journal_path": str(journal),
                 "info": dict(self.info), "entries": []}
        _atomic_json(manifest, state)
        grouped = {}
        for candidate in selected:
            group_key = (candidate["source"], candidate["relative_path"], candidate["sha256"], candidate["size"])
            grouped.setdefault(group_key, []).append(candidate)
        completed = 0
        try:
            for group in grouped.values():
                if cancel.is_set():
                    break
                tensor = None
                read_source = ""
                source_error = None
                try:
                    data, read_source = self._read_source(group[0], cancel)
                    tensor = self._decode(data)
                    del data
                    emit({"type": "status", "message": f"Exporting face crops on {self.info['device_name']}"})
                except _Cancelled:
                    break
                except Exception as error:
                    source_error = error
                for candidate in group:
                    if cancel.is_set():
                        break
                    entry = {**copy.deepcopy(candidate), "read_source": read_source, "padding": padding,
                             "status": "pending", "output": "", "error": ""}
                    state["entries"].append(entry)
                    try:
                        if source_error is not None:
                            raise source_error
                        crop, bounds = self._crop(tensor, candidate, padding)
                        entry.update(bounds=list(bounds), width=bounds[2] - bounds[0], height=bounds[3] - bounds[1])
                        png = self._png(crop)
                        if cancel.is_set():
                            raise _Cancelled("Face export cancelled before writing the crop.")
                        relative = Path(candidate["relative_path"])
                        desired = destination / relative.parent / f"{relative.stem}__face_{candidate['face_index']:02d}.png"
                        if not _within(desired, destination):
                            raise ValueError("Face crop path lies outside its output folder.")
                        # Source validation above covers relative traversal. Recheck
                        # the destination tree for links immediately before creation.
                        _assert_no_links(desired, allow_missing=True)
                        path, descriptor = _reserve_destination(desired)
                        try:
                            entry.update(status="reserved", output=str(path))
                            state["info"] = dict(self.info)
                            _append_jsonl(journal, [{"event": "reserved", "entry": entry}])
                            with os.fdopen(descriptor, "wb") as stream:
                                descriptor = -1
                                stream.write(png)
                                stream.flush()
                                os.fsync(stream.fileno())
                        finally:
                            if descriptor != -1:
                                os.close(descriptor)
                        size, _, digest = _signature(path)
                        if size != len(png) or digest != hashlib.sha256(png).hexdigest():
                            raise ValueError("Face crop output failed SHA-256 verification.")
                        entry.update(status="exported", output_sha256=digest, output_bytes=size)
                        state["outputs"].append(str(path))
                        state["exported"] += 1
                    except _Cancelled as error:
                        entry.update(status="cancelled", error=str(error))
                        break
                    except Exception as error:
                        entry.update(status="error", error=str(error))
                        state["errors"].append({"id": candidate["id"], "source": candidate["source"],
                                                "face_index": candidate["face_index"], "error": str(error),
                                                "output": entry["output"]})
                    completed += 1
                    state["info"] = dict(self.info)
                    # Append-only transitions make large exports linear in the
                    # number of crops while retaining a durable crash audit.
                    _append_jsonl(journal, [{"event": "result", "entry": entry,
                                            "completed": completed, "exported": state["exported"]}])
                    emit({"type": "progress", "completed": completed, "total": len(selected), "phase": "face_export"})
                    emit({"type": "face_result", "result": copy.deepcopy(entry)})
                del tensor
        finally:
            state["cancelled"] = cancel.is_set()
            state["completed_at"] = datetime.now(timezone.utc).isoformat()
            state["info"] = dict(self.info)
            _append_jsonl(journal, [{"event": "finished", "cancelled": state["cancelled"],
                                    "exported": state["exported"], "errors": len(state["errors"]),
                                    "completed_at": state["completed_at"]}])
            _atomic_json(manifest, state)
        return state
