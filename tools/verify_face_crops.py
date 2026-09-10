"""Real CUDA face-export check using an ordinary bundled sample photo only."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from PIL import Image
    import ultralytics
    from platinum_sorter.contracts import SortOptions
    from platinum_sorter.detector import GpuDetector
    from platinum_sorter.engine import SorterEngine
    from platinum_sorter.face_crops import FaceCropService

    with tempfile.TemporaryDirectory(prefix="platinum-face-verify-") as temporary:
        root = Path(temporary)
        source = root / "source"
        source.mkdir()
        original = source / "sample-portrait.jpg"
        shutil.copy2(Path(ultralytics.__file__).parent / "assets" / "zidane.jpg", original)
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        engine = SorterEngine(root / "data", lambda: GpuDetector(ROOT / "models"))
        options = SortOptions(str(source), str(root / "sorted"), operation="move")
        report = engine.analyze(options, lambda event: None, threading.Event())
        assert report.analysis_complete and report.device["cuda_verified"]
        service = FaceCropService(report)
        candidates = service.candidates()
        assert candidates, "The public sample did not produce a qualifying face detection"
        preview = service.preview(candidates[0], padding=0.10)
        with Image.open(io.BytesIO(preview["png"])) as image:
            assert max(image.size) <= 480
        assert "cuda" in str(preview["info"].get("device", "")).lower(), preview["info"]
        result = service.export(candidates, str(root / "faces"), 0.10, lambda event: None, threading.Event())
        assert not result["errors"] and result["exported"] == len(candidates), result
        first_hashes = {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in result["outputs"]}
        assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
        second = service.export(candidates, str(root / "faces"), 0.10, lambda event: None, threading.Event())
        assert not set(result["outputs"]) & set(second["outputs"])
        assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == value for path, value in first_hashes.items())
        engine.execute(report, lambda event: None, threading.Event())
        assert report.results[0].status == "moved" and not original.exists()
        moved_service = FaceCropService(report)
        moved_candidates = moved_service.candidates()
        moved_preview = moved_service.preview(moved_candidates[0], 0.10)
        assert moved_preview["png"]
        moved_export = moved_service.export(moved_candidates, str(root / "faces-after-move"), 0.10, lambda event: None, threading.Event())
        assert moved_export["exported"] == len(moved_candidates) and not moved_export["errors"]
        evidence = {
            "passed": True, "sample": "Bundled non-explicit Ultralytics portrait",
            "face_count": len(candidates), "preview_info": preview["info"],
            "export_info": result["info"], "original_preserved_by_crop": True,
            "collision_safe": True, "verified_moved_source_supported": True,
        }
        path = ROOT / "reports" / "face-crop-gpu-verification.json"
        path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print(json.dumps(evidence))


if __name__ == "__main__":
    main()
