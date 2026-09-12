"""Hardware-accelerated video probing, scene-cut harvesting, and frame sampling.

Leverages FFmpeg / NVDEC where available for fast frame extraction and lossless compilation splitting.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Iterator

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})
ANIMATED_IMAGE_EXTENSIONS = frozenset({".gif"})


def is_video_path(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_animated_path(path: Path) -> bool:
    return path.suffix.lower() in ANIMATED_IMAGE_EXTENSIONS


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        f = float(val)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def probe_media_file(path: Path) -> dict:
    """Extract resolution, fps, duration, frame count, and audio properties via ffprobe."""
    path = Path(path)
    if not path.is_file():
        return {
            "error": "File not found",
            "width": 0,
            "height": 0,
            "fps": 0.0,
            "duration_s": 0.0,
            "frame_count": 0,
            "codec": "unknown",
            "has_audio": False,
            "audio_channels": 0,
            "audio_sample_rate": 0,
        }

    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=width,height,r_frame_rate,duration,nb_frames,codec_name,codec_type,channels,sample_rate:format=duration",
            "-of", "json",
            str(path),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            streams = data.get("streams", [])
            fmt = data.get("format", {})
            v_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
            a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

            if v_stream:
                width = int(v_stream.get("width", 0) or 0)
                height = int(v_stream.get("height", 0) or 0)
                r_fps = v_stream.get("r_frame_rate", "30/1")
                try:
                    num, den = map(float, r_fps.split("/"))
                    fps = num / den if den != 0 and num > 0 else 30.0
                except Exception:
                    fps = 30.0
                duration = _safe_float(v_stream.get("duration"), default=0.0)
                if duration <= 0.0:
                    duration = _safe_float(fmt.get("duration"), default=0.0)
                nb_f = v_stream.get("nb_frames")
                try:
                    frames = int(nb_f) if nb_f and nb_f != "N/A" else max(1, int(round(duration * fps)))
                except (TypeError, ValueError):
                    frames = max(1, int(round(duration * fps)))
                return {
                    "width": width,
                    "height": height,
                    "fps": round(fps, 3),
                    "duration_s": round(duration, 3),
                    "frame_count": frames,
                    "codec": v_stream.get("codec_name", "unknown"),
                    "has_audio": a_stream is not None,
                    "audio_channels": int(a_stream.get("channels", 0) or 0) if a_stream else 0,
                    "audio_sample_rate": int(a_stream.get("sample_rate", 0) or 0) if a_stream else 0,
                }
    except Exception as exc:
        logging.warning("ffprobe failed for %s: %s", path, exc)

    # Fallback to OpenCV if available
    try:
        import cv2
        cap = cv2.VideoCapture(str(path))
        if cap.isOpened():
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1)
            duration = frames / fps if fps > 0 else 0.0
            cap.release()
            return {
                "width": width,
                "height": height,
                "fps": round(fps, 3),
                "duration_s": round(duration, 3),
                "frame_count": frames,
                "codec": "opencv",
                "has_audio": False,
                "audio_channels": 0,
                "audio_sample_rate": 0,
            }
    except Exception:
        pass

    return {
        "width": 0, "height": 0, "fps": 0.0, "duration_s": 0.0,
        "frame_count": 0, "codec": "unknown",
        "has_audio": False, "audio_channels": 0, "audio_sample_rate": 0,
    }


def detect_scene_cuts(
    video_path: Path,
    threshold: float = 0.35,
    adaptive: bool = True,
    min_cut_interval: float = 0.4,
) -> list[float]:
    """Detect hard cut timestamps in a video using the ffmpeg scene filter with adaptive sensitivity."""
    video_path = Path(video_path)

    def _run_detect(thr: float) -> list[float]:
        cmd = [
            "ffmpeg", "-hide_banner", "-nostats",
            "-hwaccel", "auto",
            "-i", str(video_path),
            "-filter:v", f"select='gt(scene,{thr})',showinfo",
            "-f", "null", "-",
        ]
        detected = [0.0]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120
            )
            pattern = re.compile(r"pts_time:([0-9.]+)")
            for line in proc.stderr.splitlines():
                match = pattern.search(line)
                if match:
                    t = float(match.group(1))
                    if t - detected[-1] >= min_cut_interval:
                        detected.append(round(t, 3))
        except Exception as exc:
            logging.warning("Scene detection failed for %s: %s", video_path, exc)
        return detected

    cuts = _run_detect(threshold)

    # Adaptive refinement if requested
    if adaptive and len(cuts) <= 1:
        # If no cuts were detected on a video longer than 20s, try a more sensitive pass
        info = probe_media_file(video_path)
        if info.get("duration_s", 0.0) > 20.0 and threshold > 0.22:
            softer_thr = max(0.20, threshold - 0.12)
            cuts_soft = _run_detect(softer_thr)
            if len(cuts_soft) > len(cuts):
                cuts = cuts_soft

    return cuts


def split_compilation(
    video_path: Path,
    output_dir: Path,
    min_take_duration: float = 1.0,
    max_take_duration: float = 45.0,
    scene_threshold: float = 0.35,
    adaptive_threshold: bool = True,
    emit=lambda event: None,
    cancel_event=None,
) -> list[Path]:
    """Slice a long compilation into standalone scene takes using lossless stream copy.

    Preserves source metadata, timestamps, and writes an interactive takes_manifest.json.
    """
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    info = probe_media_file(video_path)
    total_duration = info.get("duration_s", 0.0)
    if total_duration <= 0.0:
        return []

    cuts = detect_scene_cuts(
        video_path, threshold=scene_threshold, adaptive=adaptive_threshold
    )
    cuts.append(total_duration)
    # Deduplicate and sort
    cuts = sorted(set(cuts))

    takes = []
    manifest_records = []
    base_stem = video_path.stem
    ext = video_path.suffix

    for i in range(len(cuts) - 1):
        if cancel_event is not None and cancel_event.is_set():
            break

        start = cuts[i]
        end = cuts[i + 1]
        span = end - start

        if span < min_take_duration:
            continue
        if span > max_take_duration:
            end = start + max_take_duration
            span = max_take_duration

        take_filename = f"{base_stem}_take_{i + 1:03d}_{start:.2f}s{ext}"
        take_path = output_dir / take_filename
        tmp_path = take_path.with_name(f".{take_path.stem}.{uuid.uuid4().hex}.tmp{ext}")

        # Stream copy slice (zero re-encoding) with metadata preservation and clean stream mapping
        slice_cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{start:.3f}",
            "-to", f"{end:.3f}",
            "-i", str(video_path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-map_metadata", "0",
            "-c", "copy",
            "-avoid_negative_ts", "1",
            str(tmp_path),
        ]
        try:
            res = subprocess.run(slice_cmd, capture_output=True, timeout=60)
            if res.returncode == 0 and tmp_path.is_file() and tmp_path.stat().st_size > 1024:
                try:
                    src_stat = video_path.stat()
                    os.utime(tmp_path, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
                except OSError:
                    pass
                os.replace(tmp_path, take_path)
                takes.append(take_path)
                take_record = {
                    "take_file": take_filename,
                    "path": str(take_path),
                    "start_s": round(start, 3),
                    "end_s": round(end, 3),
                    "duration_s": round(span, 3),
                    "size_bytes": take_path.stat().st_size,
                }
                manifest_records.append(take_record)
                emit({"type": "take_extracted", "path": str(take_path), "duration": span, "take_index": len(takes)})
        except Exception as exc:
            logging.warning("Slice extraction failed for %s [%.2fs - %.2fs]: %s", video_path, start, end, exc)
        finally:
            tmp_path.unlink(missing_ok=True)

    # Write takes manifest JSON for downstream PMV assembly and audits
    if manifest_records:
        manifest_path = output_dir / "takes_manifest.json"
        manifest_data = {
            "source_video": str(video_path),
            "source_duration_s": total_duration,
            "scene_threshold": scene_threshold,
            "take_count": len(takes),
            "takes": manifest_records,
        }
        manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

    return takes


def sample_video_frames(
    video_path: Path,
    sample_fps: float = 2.0,
    max_frames: int = 40,
) -> list[tuple[float, bytes]]:
    """Sample video frames evenly across the entire duration, returning (timestamp_s, jpeg_bytes)."""
    video_path = Path(video_path)
    info = probe_media_file(video_path)
    duration = float(info.get("duration_s", 0.0) or 0.0)

    if duration <= 0:
        duration = 1.0

    # Distribute sampling frames across the entire file duration
    if duration * sample_fps <= max_frames:
        effective_fps = max(sample_fps, 0.1)
        step = 1.0 / effective_fps
        count = max(1, int(round(duration * effective_fps)))
    else:
        count = max(1, max_frames)
        step = duration / float(count)
        effective_fps = float(count) / max(duration, 0.1)

    timestamps = [round(i * step, 3) for i in range(count)]
    if not timestamps:
        timestamps = [0.0]

    samples: list[tuple[float, bytes]] = []

    # Fast batch extraction via ffmpeg fps filter to stdout with hardware acceleration
    try:
        cmd = [
            "ffmpeg", "-v", "error",
            "-hwaccel", "auto",
            "-i", str(video_path),
            "-vf", f"fps={effective_fps:.4f}",
            "-vframes", str(len(timestamps)),
            "-q:v", "4",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        if proc.returncode == 0 and proc.stdout:
            # Parse multipart MJPEG byte stream
            raw = proc.stdout
            soi = b"\xff\xd8"
            eoi = b"\xff\xd9"
            start_idx = 0
            frame_idx = 0
            while True:
                soi_pos = raw.find(soi, start_idx)
                if soi_pos == -1:
                    break
                eoi_pos = raw.find(eoi, soi_pos + 2)
                if eoi_pos == -1:
                    break
                jpeg_bytes = raw[soi_pos : eoi_pos + 2]
                ts = timestamps[frame_idx] if frame_idx < len(timestamps) else duration
                samples.append((ts, jpeg_bytes))
                frame_idx += 1
                start_idx = eoi_pos + 2
            if samples:
                return samples
    except Exception as exc:
        logging.warning("ffmpeg pipe frame extraction failed for %s: %s", video_path, exc)

    # Secondary fallback using OpenCV
    try:
        import cv2
        cap = cv2.VideoCapture(str(video_path))
        if cap.isOpened():
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 30.0)
            for ts in timestamps:
                frame_num = int(ts * fps)
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
                    samples.append((ts, buf.tobytes()))
            cap.release()
    except Exception:
        pass

    return samples
