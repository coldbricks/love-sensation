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

## Opening groove

The optional startup cue plays one bar of a local audio file. Open **Startup music…** in the sidebar, choose a WAV, MP3, FLAC, or OGG file, and set a starting time and tempo. A bar is four beats: at 120 BPM it lasts two seconds. Use Preview to adjust the cue before enabling it for startup. If your file is already trimmed to the desired bar, use a starting time of zero.

Playback is off by default. Volume and mute controls are available, and closing the cue dialog stops a preview. Audio plays asynchronously so the organizer remains responsive. Compressed audio seeking can vary with the file and codec; an accurately trimmed WAV gives the most predictable opening.

The app does not fetch music from a URL. It stores the chosen local path and cue settings in `data/startup_audio.json`, which stays outside version control. No music recording is included in the release.

## File handling and recovery

Copy and Move reserve output filenames without replacing an existing file. Each copied file must match the reviewed SHA-256 hash. Files changed since analysis require a new analysis.

Move first creates and verifies every required category copy. It removes the original only after all copies pass verification. Windows source and destination handles are held during the final verification/removal step. Failed or partial operations appear in the results and saved journal.

Cancellation is cooperative: an active batch or file operation finishes its safe stopping point first. A cancelled analysis must be run again. Interrupted filing can be resumed by opening its saved run and applying the reviewed plan.

Saved runs are under `data/runs/`. Keep the `.json` manifest and matching `.wal.jsonl` journal together. **Open run** loads the manifest and replays its journal. Do not edit these records while a run is active.

Symbolic links and Windows reparse points are rejected for file operations. Animated GIFs and multi-page TIFFs are reported as unsupported; the app does not silently choose one frame. A `._` filename is skipped only if its file header identifies macOS AppleDouble metadata. Such files are left untouched.

## Local data

| Location | Contents |
| --- | --- |
| `data/settings.json` | Folder choices and interface preferences |
| `data/startup_audio.json` | Optional local audio path, cue point, tempo, and volume |
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
