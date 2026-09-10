from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def main():
    (PROJECT_ROOT / "data").mkdir(exist_ok=True)
    logging.basicConfig(filename=PROJECT_ROOT / "data" / "application.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    parser = argparse.ArgumentParser(description="Love Sensation local image organizer")
    parser.add_argument("--diagnose", action="store_true", help="Verify the model and CUDA with synthetic input")
    args = parser.parse_args()
    if args.diagnose:
        from platinum_sorter.detector import GpuDetector
        detector = GpuDetector(PROJECT_ROOT / "models")
        path = PROJECT_ROOT / "reports" / "gpu-diagnostic.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(detector.info, indent=2), encoding="utf-8")
        print(f"Saved {path}")
    else:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Coldbricks.LoveSensation")
        from platinum_sorter.ui import launch_ui
        launch_ui()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Application startup failed")
        import sys
        if "--diagnose" not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"Love Sensation could not start.\nDetails: {PROJECT_ROOT / 'data' / 'application.log'}", "Love Sensation", 0x10)
        raise
