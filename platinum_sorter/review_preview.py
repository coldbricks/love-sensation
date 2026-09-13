"""On-demand, memory-only previews bound to the analyzed file's SHA-256."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import io
import json
import math
import os
from pathlib import Path
import re
import threading

from .engine import _Cancelled, _assert_no_links, _hash_stream, _open_regular
from .video_engine import _run_media

PREVIEW_LIMIT = (268, 180)
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_RUNTIME = None


def prepare_preview_runtime() -> None:
    """Import dependencies on the GUI/main thread, without loading a model.

    PySide's feature import hook can crash during cold torch imports on a
    QThread on Windows/Python 3.14. Cache the modules before workers start;
    importing them does not initialize CUDA or perform image/model work.
    """
    global _RUNTIME
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Prepare preview dependencies on the main thread before starting a preview worker.")
    if _RUNTIME is None:
        import torch
        import numpy as np
        from PIL import Image, ImageOps
        import torchvision.io as image_io
        _RUNTIME = {"torch": torch, "numpy": np, "Image": Image, "ImageOps": ImageOps,
                    "image_io": image_io}


def _runtime():
    if _RUNTIME is None:
        prepare_preview_runtime()
    return _RUNTIME


def _source_candidates(result) -> list[Path]:
    source = Path(result.source)
    if not source.is_absolute():
        raise ValueError("Preview source must be an absolute recorded path.")
    _assert_no_links(source, allow_missing=True)
    if source.exists():
        return [source]
    candidates = []
    for item in result.output_details:
        path = Path(item.get("path", ""))
        if (item.get("status") == "verified" and path.is_absolute()
                and str(path) in result.destinations and path not in candidates):
            if item.get("sha256") and item["sha256"] != result.sha256:
                continue
            candidates.append(path)
    if not candidates:
        raise FileNotFoundError("The original and its recorded verified outputs are missing.")
    return candidates


@contextmanager
def _verified_source(result, cancel_event=None):
    """Stream the hash; keep a regular-file handle open through decoding."""
    if not isinstance(result.sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", result.sha256):
        raise ValueError("This review has no valid source hash. Analyze the file again.")
    failures = []
    for path in _source_candidates(result):
        stack = ExitStack()
        try:
            stream = stack.enter_context(_open_regular(path))
            before = os.fstat(stream.fileno())
            if before.st_size != result.size:
                raise ValueError("File size differs from the saved analysis.")
            if _hash_stream(stream, cancel_event) != result.sha256.lower():
                raise ValueError("File SHA-256 differs from the saved analysis. Analyze it again.")
            stream.seek(0)
        except _Cancelled as error:
            stack.close()
            raise InterruptedError("Preview cancelled") from error
        except (OSError, ValueError) as error:
            stack.close()
            failures.append(str(error))
            continue
        try:
            yield path, stream, before
            _assert_no_links(path)
            after, current = os.fstat(stream.fileno()), path.lstat()
            signature = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            if signature(before) != signature(after) or signature(after) != signature(current):
                raise ValueError("Preview source changed while decoding. Analyze it again.")
        finally:
            stack.close()
        return
    raise ValueError("Cannot verify the preview source: " + "; ".join(failures))


def surviving_path(result) -> Path:
    """Return a surviving source/output only after verifying its analyzed bytes."""
    with _verified_source(result) as (path, _stream, _stat):
        return path


def _fit(width, height, limit=PREVIEW_LIMIT, *, even=False):
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in (width, height, *limit)):
        raise ValueError("Invalid preview dimensions.")
    factor = min(limit[0] / width, limit[1] / height, 1.0)
    result = (max(1, int(width * factor)), max(1, int(height * factor)))
    return tuple(max(2, value // 2 * 2) for value in result) if even else result


def _video_display_size(stream):
    width, height = int(stream["width"]), int(stream["height"])
    if width <= 0 or height <= 0:
        raise ValueError("Video has invalid frame dimensions.")
    ratio = 1.0
    try:
        numerator, denominator = map(float, str(stream.get("sample_aspect_ratio", "1:1")).split(":"))
        ratio = numerator / denominator
        if not math.isfinite(ratio) or ratio <= 0:
            ratio = 1.0
    except (ValueError, ZeroDivisionError):
        pass
    display_width, display_height = width * ratio, height
    rotation = float(stream.get("tags", {}).get("rotate", 0) or 0)
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            rotation = float(side["rotation"])
            break
    if not math.isfinite(rotation):
        raise ValueError("Invalid video display rotation.")
    if round(rotation / 90) % 2:
        display_width, display_height = display_height, display_width
    return display_width, display_height


def _jpeg_codec_unavailable(error):
    if isinstance(error, (NotImplementedError, AttributeError)):
        return True
    return any(marker in str(error).lower() for marker in (
        "not compiled", "no such operator", "couldn't load custom c++ ops",
        "not available in this build", "unsupported jpeg", "jpeg format is not supported",
        "only supports 1 or 3 channels", "nvjpeg_status_jpeg_not_supported",
    ))


def _decode_image(data, torch, device, info):
    runtime = _runtime()
    np, Image, ImageOps, image_io = (runtime[key] for key in ("numpy", "Image", "ImageOps", "image_io"))
    tensor = None
    cuda = device.type == "cuda"
    with Image.open(io.BytesIO(data)) as image:
        if getattr(image, "n_frames", 1) > 1:
            raise ValueError("Animated or multi-page images need explicit frame selection.")
        orientation = image.getexif().get(274, 1)
        if cuda and image.format == "JPEG" and orientation == 1:
            try:
                tensor = image_io.decode_jpeg(torch.frombuffer(bytearray(data), dtype=torch.uint8),
                                              mode=image_io.ImageReadMode.RGB, device=device)
                info["decode"] = "CUDA JPEG"
            except torch.cuda.OutOfMemoryError:
                raise
            except (RuntimeError, NotImplementedError, OSError, AttributeError) as error:
                if not _jpeg_codec_unavailable(error):
                    raise
                info["decode"] = "CPU JPEG codec (CUDA codec unavailable)"
        if tensor is None:
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            tensor = torch.from_numpy(np.asarray(normalized).copy()).permute(2, 0, 1).to(device)
            info.setdefault("decode", "CPU image codec/EXIF normalization")
    if cuda and tensor.device.type != "cuda":
        raise RuntimeError("Preview decode did not reach the required CUDA device.")
    info["image_device"] = str(tensor.device)
    return tensor


def render_preview(result, cancel_event=None) -> dict:
    """Return PNG bytes and observed device metadata; never write a media cache."""
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Preview cancelled")
    runtime = _runtime()
    torch, Image = runtime["torch"], runtime["Image"]
    cuda = torch.cuda.is_available()
    device = torch.device("cuda:0" if cuda else "cpu")
    info = {"device": str(device), "cuda_available": cuda, "cuda_verified": False,
            "device_name": torch.cuda.get_device_name(0) if cuda else "CPU (CUDA unavailable)",
            "encode": "CPU PNG codec"}
    with _verified_source(result, cancel_event) as (path, stream, stat):
        info["source"] = str(path)
        is_video = result.media_type == "video" or path.suffix.lower() in _VIDEO_EXTENSIONS
        if is_video:
            probe = _run_media(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                "-show_entries", "stream=width,height,sample_aspect_ratio,duration:stream_tags=rotate:stream_side_data=rotation:format=duration",
                                "-of", "json", str(path)], timeout=15, cancel_event=cancel_event)
            if probe.returncode:
                raise ValueError("Video metadata could not be read.")
            document = json.loads(probe.stdout)
            source_stream = document["streams"][0]
            display_width, display_height = _video_display_size(source_stream)
            width, height = _fit(display_width, display_height, even=True)
            timestamp = float(getattr(result, "best_timestamp_s", 0) or 0)
            if not math.isfinite(timestamp):
                raise ValueError("Saved video preview timestamp is invalid.")
            duration = 0.0
            for value in (source_stream.get("duration"), document.get("format", {}).get("duration"), result.duration_s):
                try:
                    candidate = float(value)
                    if math.isfinite(candidate) and candidate > 0:
                        duration = candidate
                        break
                except (TypeError, ValueError):
                    pass
            timestamp = max(0.0, timestamp)
            if duration > 0:
                timestamp = min(timestamp, max(0.0, duration - 0.1))
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
            if cuda:
                command += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
            command += ["-ss", str(timestamp), "-i", str(path), "-map", "0:v:0", "-an"]
            resize = (f"scale_cuda={width}:{height}:format=yuv420p,hwdownload,format=yuv420p"
                      if cuda else f"scale={width}:{height}")
            command += ["-vf", resize + ",setsar=1", "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "pipe:1"]
            output = _run_media(command, timeout=20, cancel_event=cancel_event)
            if output.returncode or not output.stdout:
                raise RuntimeError("Video preview could not decode a frame. " + output.stderr.decode(errors="replace")[-500:])
            tensor = _decode_image(output.stdout, torch, device, info)
            info.update(decode="CUDA video" if cuda else "CPU video (CUDA unavailable)",
                        timestamp_s=timestamp, duration_s=duration,
                        width=source_stream["width"], height=source_stream["height"],
                        display_width=display_width, display_height=display_height)
        else:
            if stat.st_size > 256 * 1024 * 1024:
                raise ValueError("This image is too large for an interactive preview.")
            tensor = _decode_image(stream.read(), torch, device, info)
            info.update(width=tensor.shape[-1], height=tensor.shape[-2])
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Preview cancelled")
        height, width = tensor.shape[-2:]
        preview_width, preview_height = _fit(width, height)
        with torch.inference_mode():
            resized = torch.nn.functional.interpolate(tensor[None].float(), size=(preview_height, preview_width),
                                                       mode="bilinear", align_corners=False)[0]
            if cuda and resized.device.type != "cuda":
                raise RuntimeError("Preview resize did not execute on CUDA.")
            info.update(resize_device=str(resized.device), cuda_verified=cuda and resized.device.type == "cuda",
                        preview_width=preview_width, preview_height=preview_height)
            array = resized.round().clamp(0, 255).byte().permute(1, 2, 0).contiguous().cpu().numpy()
        output = io.BytesIO()
        Image.fromarray(array).save(output, format="PNG")
        return {"png": output.getvalue(), "info": info}
