# Using Love Sensation

## Installation

Run `Setup.ps1` from an extracted release or a cloned checkout. It installs into `.venv` alongside the application. It does not need a system-wide package installation.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup.ps1
```

To choose a specific Python installation:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup.ps1 -Python 'C:\Path\To\Python\python.exe'
```

Use `-SkipDiagnose` to finish dependency setup without downloading and verifying the detection model immediately. The app obtains the model when analysis first needs it. Normal setup verifies the actual compute device using a real operation.

The supported desktop target is Windows 11. Python 3.12–3.14, PyTorch, and torchvision must be 64-bit. The default setup uses the CUDA 12.8 package channel. Keep your NVIDIA driver compatible with that runtime. The app selects CPU processing if CUDA is unavailable and displays that choice.

## Choosing a sort

Use separate, non-overlapping source and output folders. The app scans supported image extensions recursively and preserves relative subfolders inside each category.

- **Best category:** file into the single highest-scoring selected category above the threshold.
- **Top three categories:** file into up to three unique selected categories.
- **All categories:** file into every selected category above the threshold.
- **Unmatched:** optionally retain images with no qualifying selected category in `_Unmatched`.

The confidence threshold is a detector score. Raising it usually produces fewer matches; lowering it accepts more uncertain matches. Repeated detections of the same category do not consume extra category slots.

Use **Deselect all**, then enable the categories you want. Analyze stays disabled while no categories are selected. Changing settings after a review requires a new analysis before filing; unchanged image detections can be reused from the cache.

## Review and apply

Analysis reads source images without creating sorted copies. Search and filter the results before choosing **Apply sorting**. Copy is the default operation. Each image can create more than one full-image output when it matches multiple selected categories.

The progress label reports elapsed analysis throughput and an estimated time remaining. Disk speed, image dimensions, codec, verification, caching, and other applications affect the rate. GPU utilization alone does not measure total sorting throughput.

The reader prepares one bounded group ahead of detection. Encoded image buffers are limited to 128 MiB across the current and prefetched groups; decoded tensors, model memory, and review records use additional memory. Oversized individual files use a verified path-reading fallback.

## Face export

After a completed analysis, **Face crops…** lists qualifying face detections. The selected row controls the preview; **Export face crops** exports every face in the list.

Set padding between 0 and 30%. Crops use the EXIF-corrected image orientation and are saved as PNG files. The exporter verifies source bytes against the reviewed file hash. If a run already moved an original, the exporter can use a verified full-image copy recorded by that run.

Crop filenames include a face number and receive a safe suffix on repeat export. Export summaries and append-only event journals record results. An interrupted face export can be started again; a new export creates distinct filenames rather than resuming existing crops.

## PMV Forge & Comp Harvester

- **The PMV Forge:** Open **PMV Forge…** in the sidebar. Drop any audio track (WAV, MP3, FLAC, OGG) to detect its exact BPM, 4-beat bar lines, and drop cues via GPU autocorrelation. Adjust the Chaos Knob (0.0 for steady 1-bar cuts, 0.5 for balanced grooves, 1.0 for rapid-fire stutters) and export an automated Final Cut Pro 7 / Premiere Pro XML sequence with sequence markers (`DROP`, `FILL`, `CHOKE`, `PEAK`).
- **Comp Harvester:** Open **Comp Harvester…** in the sidebar. Point at compilation videos to perform lossless scene-cut splitting into standalone takes without re-encoding.
- **Flight Report:** Open **Flight Report…** to view the interactive radar cockpit dashboard with the audio waveform, cut timeline blocks, and clickable media cards.
- **NTFS Hardlinks:** In the Vault organizer, select **Hardlink** to place files in multiple category folders with zero duplicate disk usage.
- **Stealth Loupe:** Press `Space` on any selected row to view an instant frosted-glass inspection card with prominence and aspect ratios; press `Esc` to instantly sanitize the UI.

## File handling and recovery

Copy, Move, and Hardlink reserve output filenames without replacing an existing file. Each copied or linked file must match the reviewed SHA-256 hash. Files changed since analysis require a new analysis.

Move first creates and verifies every required category copy. It removes the original only after all copies pass verification. Windows source and destination handles are held during the final verification/removal step. Failed or partial operations appear in the results and saved journal.

Cancellation is cooperative: an active batch or file operation finishes its safe stopping point first. A cancelled analysis must be run again. Interrupted filing can be resumed by opening its saved run and applying the reviewed plan.

Saved runs are under `data/runs/`. Keep the `.json` manifest and matching `.wal.jsonl` journal together. **Open run** loads the manifest and replays its journal. Do not edit these records while a run is active.

Symbolic links and Windows reparse points are rejected for file operations. A `._` filename is skipped only if its file header identifies macOS AppleDouble metadata. Such files are left untouched.

## Local data

| Location | Contents |
| --- | --- |
| `data/settings.json` | Folder choices and interface preferences |
| `data/detections.sqlite3` | Local paths, source hashes, and cached detection results |
| `data/runs/` | Review manifests and filing recovery journals |
| `data/application.log` | Diagnostics, potentially including local file paths |
| `models/640m.pt` | Automatically downloaded detector, checked against its known hash |
| `reports/` | Optional local verification output |

Keep these folders private when sharing a copy of the app. They are excluded from Git. Deleting the cache forces detection to run again; retain run manifests and journals for any filing you may need to resume.

## Troubleshooting

**The launcher does nothing:** run `.\.venv\Scripts\python.exe .\main.py` in PowerShell to see startup output. Check `data/application.log`.

**CPU fallback appears:** run `.\.venv\Scripts\python.exe .\main.py --diagnose`. Confirm that the selected virtual environment has the CUDA build of PyTorch and that the NVIDIA driver supports it. Diagnosis prints the compute device used by a real model operation.

**Model download fails:** setup needs network access to GitHub releases. Retry setup or analysis after connectivity returns. Downloads failing the expected size or hash are rejected.

**An image errors:** inspect its reported error. A filename extension alone does not guarantee a valid image. Unsupported animations, damaged images, and files that changed during a run are isolated so other files can continue.

**The output folder is rejected:** choose a separate location outside the source tree, with no symlinks or reparse points in its path.

**Reporting a problem:** include app version, Windows/Python versions, operation, and a sanitized error message. Remove personal filenames and paths. Do not attach your private library or entire `data/` directory.
