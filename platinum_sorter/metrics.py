"""Geometric, scale-invariant, and sustained temporal metrics for media scoring.

Borrowed and adapted from high-precision ratio and temporal algorithms.
"""
from __future__ import annotations

import math
from typing import Sequence
import numpy as np

# Focal priority weights for visual interest centering
FOCAL_TARGET_WEIGHTS = {
    "BUTTOCKS_EXPOSED": 1.00,
    "BUTTOCKS_COVERED": 0.90,
    "FEMALE_GENITALIA_EXPOSED": 0.95,
    "FEMALE_GENITALIA_COVERED": 0.70,
    "FEMALE_BREAST_EXPOSED": 0.85,
    "FEMALE_BREAST_COVERED": 0.60,
    "ANUS_EXPOSED": 0.95,
    "FACE_FEMALE": 0.65,
    "FACE_MALE": 0.35,
    "BELLY_EXPOSED": 0.40,
    "FEET_EXPOSED": 0.35,
}


def box_area(box: Sequence[int | float]) -> float:
    """Calculate area of [x, y, w, h] box."""
    if len(box) < 4:
        return 0.0
    try:
        coords = [float(v) for v in box[:4]]
        if not all(math.isfinite(v) for v in coords):
            return 0.0
        return max(0.0, coords[2]) * max(0.0, coords[3])
    except (TypeError, ValueError):
        return 0.0


def box_prominence(box: Sequence[int | float], frame_width: int, frame_height: int, confidence: float) -> float:
    """Scale-invariant subject prominence.

    prominence = confidence * sqrt(box_area / frame_area)
    A full-frame subject scores near confidence, while distant background clutter drops off.
    """
    if frame_width <= 0 or frame_height <= 0 or not math.isfinite(confidence) or confidence <= 0:
        return 0.0
    frame_area = float(frame_width * frame_height)
    area = box_area(box)
    if area <= 0.0:
        return 0.0
    ratio = min(max(area / frame_area, 0.0), 1.0)
    return float(confidence * math.sqrt(ratio))


def box_aspect(box: Sequence[int | float]) -> float:
    """Calculate width-to-height aspect ratio."""
    if len(box) < 4:
        return 0.0
    try:
        coords = [float(v) for v in box[:4]]
        if not all(math.isfinite(v) for v in coords) or coords[3] <= 0:
            return 0.0
        return max(0.0, coords[2]) / coords[3]
    except (TypeError, ValueError):
        return 0.0


def subject_to_face_ratio(subject_box: Sequence[int | float], face_box: Sequence[int | float]) -> float:
    """Scale-invariant proportion: subject width relative to face width in the same frame."""
    if len(subject_box) < 3 or len(face_box) < 3:
        return 0.0
    try:
        s_coords = [float(v) for v in subject_box[:min(len(subject_box), 4)]]
        f_coords = [float(v) for v in face_box[:min(len(face_box), 4)]]
        if not (all(math.isfinite(v) for v in s_coords) and all(math.isfinite(v) for v in f_coords)):
            return 0.0
        sw, fw = s_coords[2], f_coords[2]
        if fw <= 0:
            return 0.0
        return max(0.0, sw) / fw
    except (TypeError, ValueError):
        return 0.0


def evaluate_frame_detections(
    detections: list[dict],
    frame_width: int,
    frame_height: int,
    selected_classes: set[str] | None = None,
) -> dict:
    """Score all detections in a single frame and identify the peak focal object."""
    if not detections or frame_width <= 0 or frame_height <= 0:
        return {
            "max_prominence": 0.0,
            "max_aspect": 0.0,
            "max_confidence": 0.0,
            "best_detection": None,
            "focal_point": (frame_width // 2, frame_height // 2),
            "face_count": 0,
        }

    max_prominence = 0.0
    max_aspect = 0.0
    max_confidence = 0.0
    best_det = None
    faces = []

    for det in detections:
        cls_name = det.get("class", "")
        if selected_classes and cls_name not in selected_classes:
            continue
        conf = float(det.get("score", 0.0))
        box = det.get("box", [0, 0, 0, 0])
        prom = box_prominence(box, frame_width, frame_height, conf)
        aspect = box_aspect(box)

        det["prominence"] = round(prom, 4)
        det["aspect"] = round(aspect, 4)

        if "FACE" in cls_name:
            faces.append(box)

        if prom > max_prominence or (prom == max_prominence and conf > max_confidence):
            max_prominence = prom
            max_aspect = aspect
            max_confidence = conf
            best_det = det

    focal_x, focal_y = ken_burns_focal_point(detections, frame_width, frame_height)

    return {
        "max_prominence": round(max_prominence, 4),
        "max_aspect": round(max_aspect, 4),
        "max_confidence": round(max_confidence, 4),
        "best_detection": best_det,
        "focal_point": (focal_x, focal_y),
        "face_count": len(faces),
    }


def sustained_85th_percentile(values: Sequence[float], percentile: float = 85.0) -> float:
    """Evaluate scores at the 85th percentile over time.

    Sustained high marks beat a single lucky 1-frame artifact.
    """
    if not values:
        return 0.0
    arr = np.array(values, dtype=np.float32)
    return float(np.percentile(arr, percentile))


def ken_burns_focal_point(
    detections: list[dict],
    frame_width: int,
    frame_height: int,
) -> tuple[int, int]:
    """Determine the optimal focal center coordinates for smart zooming/cropping."""
    if not detections or frame_width <= 0 or frame_height <= 0:
        return max(0, frame_width // 2), max(0, frame_height // 2)

    best_score = -1.0
    best_center = (frame_width // 2, frame_height // 2)

    for det in detections:
        cls_name = det.get("class", "")
        weight = FOCAL_TARGET_WEIGHTS.get(cls_name, 0.2)
        conf = float(det.get("score", 0.0))
        box = det.get("box", [0, 0, 0, 0])
        if len(box) >= 4:
            try:
                coords = [float(v) for v in box[:4]]
                if all(math.isfinite(v) for v in coords):
                    x, y, w, h = coords
                    prom = box_prominence(coords, frame_width, frame_height, conf)
                    score = prom * weight
                    if score > best_score:
                        best_score = score
                        cx = int(round(max(0.0, min(float(frame_width), x + w / 2.0))))
                        cy = int(round(max(0.0, min(float(frame_height), y + h / 2.0))))
                        best_center = (cx, cy)
            except (TypeError, ValueError):
                continue

    return best_center


def video_prominence_profile(
    frame_results: list[tuple[float, list[dict]]],
    frame_width: int = 640,
    frame_height: int = 640,
    selected_classes: set[str] | None = None,
) -> dict:
    """Analyze temporal prominence, sustained 85th-percentile WOW, and focal path across video frames.

    Args:
        frame_results: List of (timestamp_s, detections_list) tuples.
        frame_width: Video frame width in pixels.
        frame_height: Video frame height in pixels.
        selected_classes: Optional filter of classes to consider.

    Returns:
        Dictionary with sustained_wow, peak_prominence, peak_timestamp_s,
        peak_aspect, best_box, focal_trajectory, and aggregated_detections.
    """
    if not frame_results:
        return {
            "sustained_wow": 0.0,
            "peak_prominence": 0.0,
            "peak_timestamp_s": 0.0,
            "peak_aspect": 0.0,
            "best_box": [],
            "best_detection": None,
            "focal_trajectory": [],
            "mean_aspect": 0.0,
            "frame_count": 0,
            "all_detections": [],
        }

    prominences = []
    aspects = []
    trajectory = []
    peak_prom = -1.0
    peak_ts = 0.0
    peak_aspect = 0.0
    best_box = []
    best_det = None
    all_detections = []

    for ts, dets in frame_results:
        eval_res = evaluate_frame_detections(
            dets, frame_width, frame_height, selected_classes=selected_classes
        )
        prom = eval_res["max_prominence"]
        asp = eval_res["max_aspect"]
        prominences.append(prom)
        if asp > 0:
            aspects.append(asp)
        trajectory.append((round(float(ts), 3), eval_res["focal_point"]))

        if prom > peak_prom:
            peak_prom = prom
            peak_ts = round(float(ts), 3)
            peak_aspect = asp
            best_det = eval_res["best_detection"]
            if best_det and best_det.get("box"):
                best_box = [int(round(v)) for v in best_det["box"]]

        for d in dets:
            all_detections.append(d)

    sustained = sustained_85th_percentile(prominences, percentile=85.0)
    mean_asp = float(np.mean(aspects)) if aspects else 0.0

    return {
        "sustained_wow": round(sustained, 4),
        "peak_prominence": round(max(peak_prom, 0.0), 4),
        "peak_timestamp_s": peak_ts,
        "peak_aspect": round(peak_aspect, 4),
        "best_box": best_box,
        "best_detection": best_det,
        "focal_trajectory": trajectory,
        "mean_aspect": round(mean_asp, 4),
        "frame_count": len(frame_results),
        "all_detections": all_detections,
    }
