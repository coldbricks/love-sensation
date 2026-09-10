from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol

EventSink = Callable[[dict], None]


@dataclass
class SortOptions:
    source: str
    destination: str
    threshold: float = 0.62
    mode: str = "top3"  # best, top3, all; unique categories
    operation: str = "copy"  # copy or move
    include_unmatched: bool = True
    selected_classes: list[str] = field(default_factory=list)  # empty = all
    batch_size: int = 16


@dataclass
class ImageResult:
    source: str
    relative_path: str
    size: int
    mtime_ns: int
    detections: list[dict] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    status: str = "ready"
    error: str = ""
    destinations: list[str] = field(default_factory=list)
    cached: bool = False
    sha256: str = ""
    output_details: list[dict] = field(default_factory=list)


@dataclass
class ScanReport:
    run_id: str
    options: SortOptions
    results: list[ImageResult] = field(default_factory=list)
    device: dict = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    cancelled: bool = False
    manifest_path: str = ""
    phase: str = "analyze"
    analysis_complete: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


class Detector(Protocol):
    fingerprint: str
    info: dict

    def detect_batch(self, paths: list[Path]) -> list[list[dict] | Exception]: ...
