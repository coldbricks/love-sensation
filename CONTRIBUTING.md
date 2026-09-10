# Contributing

Small, focused changes and reproducible bug reports are welcome. Use synthetic fixtures or ordinary, redistributable sample images when demonstrating an issue. Keep personal libraries, saved runs, and private paths out of issues and commits.

## Development setup

Run `Setup.ps1`, then install test dependencies and run the suite:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
```

The core tests use temporary synthetic files and fake detector responses. Face-export tests use synthetic pixels with the available compute device. No detector weights or user library are needed for this suite. Windows-specific locking tests run on Windows; symbolic-link tests may skip when privileges are unavailable.

For real desktop and GPU integration checks on a configured CUDA machine:

```powershell
.\.venv\Scripts\python.exe tools\verify_desktop.py
.\.venv\Scripts\python.exe tools\verify_face_crops.py
.\.venv\Scripts\python.exe tools\verify_crop_desktop.py
.\.venv\Scripts\python.exe tools\verify_gpu.py
```

These checks use temporary copies of ordinary bundled sample photos. Run GPU benchmarks separately from other GPU jobs. Generated reports stay local.

## Project map

| File | Responsibility |
| --- | --- |
| `main.py` | Desktop entry point, logging, and compute diagnosis |
| `platinum_sorter/ui.py` | Main desktop window and background sort worker |
| `platinum_sorter/engine.py` | Scanning, caching, file verification, copying/moving, recovery |
| `platinum_sorter/detector.py` | Model provenance, decoding, batched detection, compute verification |
| `platinum_sorter/face_crops.py` | Verified source reads and face-only crop export |
| `platinum_sorter/crop_dialog.py` | Face preview and export controls |
| `platinum_sorter/startup_audio.py` | Optional local audio cue and playback bounds |
| `platinum_sorter/contracts.py` | Shared options and result records |

Preserve the review-before-apply workflow, filename collision handling, source hash checks, and move verification. Keep model and filesystem work off the interface thread. CUDA is the default numerical processing path when available; make fallback behavior visible.

For changes to file handling, add regression coverage for the failure mode. For UI-only changes, verify the minimum supported window size and use isolated temporary settings. `tools/capture_readme.py` reproduces the documentation screenshots using fabricated demo content.
