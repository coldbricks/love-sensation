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

## Create a music video

The app opens in **Create music video**. You can work directly from a video folder; a detection or sorting run is not required.

1. **Your footage:** click **Choose footage folder…**. The folder loads immediately. **Include subfolders** controls recursive loading; **Load clips** reloads a typed path or an updated folder. The clip count, combined duration, and skipped-file count appear below it. Hover over the count for reasons files were skipped. The source files remain in place.
2. **Your music:** choose a soundtrack, then **Analyze Track**. Use the BPM override when needed; changing it requires reanalysis.
3. **Edit settings:** choose pacing with Chaos, an edit seed, and the timeline frame rate.
4. **Build timeline:** review the ordered cuts, their timeline positions, source filenames, source in-points, and durations. Changing footage, music, or edit settings clears the old preview so it cannot be exported accidentally.
5. **Export XML…:** save the exact previewed sequence and its companion HTML report. Import the XML into Premiere Pro or a compatible editor to review playback and render the movie. This app does not render an MP4 in this workflow.

Loading footage, analyzing music, building, and exporting run in workers. **Stop** requests cancellation where supported; an operation already writing output finishes safely. Closing the app waits for active work to stop. The hidden-console fix applies to every media helper.

Editor folders, music and edit settings are remembered in `data/music_video_settings.json`, separately from the library's normal source and destination. Opening the app does not automatically scan those folders. If you have reviewed videos in the library, **Use N clips from library review** imports those included clips explicitly.

**Library & sorting** in the sidebar opens the existing detection, review, filing and face-crop tools. **Create music video** returns to the editor without discarding its current inputs or preview.

## Choosing a sort

Use separate, non-overlapping source and output folders. The app scans supported image and video extensions recursively and preserves relative subfolders inside each category.

- **Best category:** file into the single highest-scoring selected category above the threshold.
- **Top three categories:** file into up to three unique selected categories.
- **All categories:** file into every selected category above the threshold.
- **Unmatched:** optionally retain images with no qualifying selected category in `_Unmatched`.

The confidence threshold is a detector score. Raising it usually produces fewer matches; lowering it accepts more uncertain matches. Repeated detections of the same category do not consume extra category slots.

Use **Deselect all**, then enable the categories you want. Analyze stays disabled while no categories are selected. Changing settings after a review requires a new analysis before filing; unchanged image detections can be reused from the cache.

## Review and apply

Analysis reads source files without creating sorted copies. When it completes, folder setup collapses to leave more room for **Library & Review**. Click **Folders & sorting** to expand it again.

- Select one or several rows. Use **Include selected** or **Skip selected**, or the checkbox beside a ready file. Skipped files keep their analysis and can be included later.
- **Edit categories…** replaces the categories of selected ready files. Original model detections and original category choices are retained in the saved run. No checked categories means `_Unmatched`.
- **Space** on the focused results table opens or closes an on-demand preview. The inspector also shows source dimensions, model scores, filing categories and video sample counts. Previews stay in memory and are never written to a thumbnail cache.
- The action button shows the operation and exact included count, for example **Copy 12 included**. It applies to the full included set, even when search hides some rows. Search and filters only change the view.
- Review changes are saved with the run. **Open run** restores them. Completed rows cannot be edited or filed a second time; skipped ready rows remain available.

Copy is the default operation. A file can create more than one output when it matches multiple selected categories. A hardlink shares the same writable data with its source: editing either name changes both. Hash verification and cross-volume copy fallback take time and may require additional space.

Press **Escape** or **Hide workspace** to conceal the workspace, previews and owned application dialogs. Background work continues. **Resume workspace** deliberately restores the interface; previews remain closed. This covers this app's Qt windows, not external browsers or editors opened from it.

Video analysis is sampled. **Partial** means some frames failed; **Error** means analysis could not complete. Such videos are not filed as successful unmatched results. A completed sampled scan does not establish that every video frame was inspected.

The progress label reports elapsed analysis throughput and an estimated time remaining. Disk speed, image dimensions, codec, verification, caching, and other applications affect the rate. GPU utilization alone does not measure total sorting throughput.

The reader prepares one bounded group ahead of detection. Encoded image buffers are limited to 128 MiB across the current and prefetched groups; decoded tensors, model memory, and review records use additional memory. Oversized individual files use a verified path-reading fallback.

## Face export

After a completed analysis, **Face crops…** lists qualifying face detections. The selected row controls the preview; **Export face crops** exports every face in the list.

Set padding between 0 and 30%. Crops use the EXIF-corrected image orientation and are saved as PNG files. The exporter verifies source bytes against the reviewed file hash. If a run already moved an original, the exporter can use a verified full-image copy recorded by that run.

Crop filenames include a face number and receive a safe suffix on repeat export. Export summaries and append-only event journals record results. An interrupted face export can be started again; a new export creates distinct filenames rather than resuming existing crops.

## PMV Forge & Comp Harvester

- **Music video editor (PMV Forge):** Choose footage first, then music, build a cut-list preview, and export its editing sequence. Four beats per bar are assumed; silence does not produce a usable grid. Source metadata and bounds are checked before XML export. Imported playback in your target editor still needs review.
- **Comp Harvester:** Choose **Fast copy** for unchanged compressed media with potentially approximate keyframe boundaries, or **Accurate cuts** to re-encode at requested boundaries with GPU encoding when available. Every run uses a separate output subfolder and records measured durations. Long scenes are divided into multiple takes. After extraction, **Open in Library & Review** loads that folder into source setup; choose a separate output and analyze to review it.
- **Flight Report:** Opens an offline HTML summary with included/skipped state. Each run has a distinct report filename. A report catalog is metadata, not an embedded media player.

BeatEdit marker import and a persistent catalog across multiple runs are not implemented in this release.
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
