"""Acceptance and ordering for distinct local-model evidence scales."""
from __future__ import annotations

import math

from .contracts import SortOptions


def is_semantic(detection: dict) -> bool:
    """Image-level SigLIP evidence has a signed margin, not confidence."""
    return detection.get("source") == "siglip2"


def accepts_detection(detection: dict, options: SortOptions) -> bool:
    """Apply shared class/geometry filters without mixing model score scales."""
    if options.selected_classes and detection["class"] not in options.selected_classes:
        return False
    if is_semantic(detection):
        return (float(detection["raw_margin"]) > 0.0
                and options.min_prominence <= 0.0 and options.min_aspect_ratio <= 0.0)
    confidence = float(detection["score"])
    return (confidence >= options.threshold
            and float(detection.get("prominence", confidence)) >= options.min_prominence
            and float(detection.get("aspect", 0.0)) >= options.min_aspect_ratio)


def is_accepted_semantic(detection: dict, categories) -> bool:
    """Whether image-level evidence qualifies in this saved review context."""
    if not isinstance(detection, dict):
        return False
    try:
        margin = float(detection.get("raw_margin", 0.0))
    except (TypeError, ValueError, OverflowError):
        return False
    return (is_semantic(detection) and detection.get("accepted") is True
            and detection.get("class") in categories and math.isfinite(margin) and margin > 0.0)


def evidence_sort_key(detection: dict) -> tuple[int, float]:
    """Sort descending: detector evidence first, then semantic raw margin.

    The source priority keeps an uncalibrated margin from competing numerically
    with a detector confidence. Values are comparable only within each source.
    """
    if is_semantic(detection):
        return 0, float(detection["raw_margin"])
    return 1, float(detection["score"])
