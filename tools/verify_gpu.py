"""Exercise GPU inference on bundled, non-explicit Ultralytics sample photographs."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from platinum_sorter.detector import GpuDetector
from platinum_sorter.evidence import is_semantic


def main():
    detector = GpuDetector(ROOT / "models")
    import ultralytics
    import torch
    if not detector.cuda:
        raise RuntimeError("This GPU verification requires CUDA.")
    assets = Path(ultralytics.__file__).parent / "assets"
    paths = [assets / "zidane.jpg", assets / "bus.jpg"]
    fp16 = detector.detect_batch(paths)
    assert all(isinstance(result, list) for result in fp16), repr(fp16)
    assert any(any(row["class"].startswith("FACE_") for row in result) for result in fp16)
    detector.model.float()
    detector.dtype = torch.float32
    fp32 = detector.detect_batch(paths)
    detector.model.half()
    detector.dtype = torch.float16
    def classes(results):
        return [{row["class"] for row in result if not is_semantic(row) and row["score"] >= 0.62} for result in results]
    assert classes(fp16) == classes(fp32), (classes(fp16), classes(fp32))
    # Include GPU resize, normalization, inference, NMS and CPU file decoding if required.
    batch = paths * 8
    detector.detect_batch(batch)
    durations = []
    for _ in range(3):
        started = time.perf_counter()
        results = detector.detect_batch(batch)
        assert all(isinstance(result, list) for result in results)
        durations.append(time.perf_counter() - started)
    evidence = {
        "device": detector.info,
        "sample_files": [str(path) for path in paths],
        "fp16_detections": fp16,
        "nudenet_fp32_semantic_fp16_detections": fp32,
        "same_nudenet_categories_at_0_62": True,
        "semantic_precision_comparison": "Not performed; the semantic model remains FP16 in both calls.",
        "benchmark_batch": 16,
        "benchmark_seconds": durations,
        "mean_images_per_second": 48 / sum(durations),
        "scope": "Two bundled non-explicit images repeated; includes decode, excludes hashing/copy/UI. Not an accuracy benchmark.",
    }
    output = ROOT / "reports" / "gpu-verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps({"fps": evidence["mean_images_per_second"], "same_categories": True, "decode": detector.info["decode"]}))
    print(output)


if __name__ == "__main__":
    main()
