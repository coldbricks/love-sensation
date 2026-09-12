# Validation

Verified locally on Windows 11 with an NVIDIA GeForce RTX 5090 Laptop GPU (24 GB), Python 3.14, PyTorch `2.10.0.dev20251209+cu128`, torchvision `0.25.0.dev20251209+cu128`, and PySide6 6.11.2.

## Automated suite

`python -m pytest tests -q`: **89 passed, 2 skipped, 13 subtests passed** in ~6.8s. Both skips require Windows symbolic-link creation privileges. Separate Windows reparse rejection, file-locking, hardlink creation, and delete-by-handle checks passed.

Coverage includes destination collisions, concurrent reservation, unique categories, verified multi-category moves, concurrent source changes, cache invalidation, corrupt inputs, bounded prefetch, cancellation, interrupted journals, saved-run recovery, and completed-analysis guards. Face-export checks use synthetic pixels and cover bounds, padding, source verification, moved-source recovery, collisions, cancellation, and journals. New test suites cover geometric ratio metrics (`test_metrics.py`), hardware video probing and scene splitting (`test_video_engine.py`), CUDA audio beat-grids and SVG waveforms (`test_audio_grid.py`), PMV Forge assembly and FCP/Premiere XML generation (`test_pmv_forge.py`), NTFS hardlink execution (`test_hardlink.py`), and interactive flight report cockpit generation (`test_flight_report.py`), with deep null-resilience and edge-case testing.

## Desktop and GPU integration

- `tools/verify_desktop.py`: real Qt workers analyzed and copied three temporary ordinary sample images, including an accented uppercase JPEG filename. Output hashes matched, sources stayed unchanged, the next scan used the detection cache, and a saved review reopened. Changed settings invalidated the previous plan.
- `tools/verify_face_crops.py`: real CUDA face preview and export passed, including repeat-export collisions and recovery from a verified full-image copy after moving a temporary source.
- `tools/verify_crop_desktop.py`: 43 checks passed through the actual face-crop dialog, covering loading, preview, changed padding, PNG export, unchanged source/report hashes, and persistent Select all / Deselect all controls.
- Actual model convolution output was observed on `cuda:0` in `torch.float16`. Supported JPEG decoding, preprocessing, inference, NMS, and crop/preview tensor work used the GPU. PNG output uses a CPU codec.
- Numerical preprocessing parity was checked for PNG, ordinary JPEG, and EXIF-rotated JPEG paths versus immutable input bytes.
- The obsidian, silver, and champagne interface passed layout/control checks at 1300 × 840 and 1080 × 720, including all sidebar and footer actions (PMV Forge, Comp Harvester, Flight Report, and Stealth Loupe inspection).
- Incomplete one-bar startup audio cue was completely retired and replaced with high-performance musical analysis in the PMV Forge dialog.

These checks use synthetic files or ordinary sample photos supplied with Ultralytics. They do not scan a personal library. Local evidence is written under the ignored `reports/` folder.

## Performance measurement

Three alternating fresh-detection-cache scans of 128 temporary copies of two ordinary sample photos measured:

| Pipeline | Batch | Median images/second |
| --- | ---: | ---: |
| Sequential file reading | 16 | 150.6 |
| Verified snapshot and bounded prefetch | 16 | 199.0 |
| Verified snapshot and bounded prefetch | 32 | 194.6 |

Batch 16 improved by approximately **32.2%** in this small measurement, with exactly matching detections. It remains the default.

The measurement includes discovery, hashing, decode, detection, and journaling. It uses warm operating-system and model caches and excludes the desktop results view and copy/move execution. It is not representative of a complete library, a general accuracy evaluation, or a promised throughput rate.

## Installation and platform limits

The portable setup script was checked with the Windows PowerShell 5.1 parser and interpreter probes. The stable CUDA 12.8 package pair used for fresh installation differs from the existing development runtime listed above. A complete clean installation on another physical PC has not been tested.

GitHub Actions runs synthetic tests on Windows and Ubuntu with Python 3.12 and 3.14 using CPU wheels. Remote status is shown by the repository's Tests badge. Linux CI checks core behavior; the supported desktop target is Windows.

The documentation images are captures of the actual Qt interface populated with fabricated demo records and an original illustrated portrait. They are presentation fixtures, not detection-accuracy evidence.
