<p align="center">
  <img src="docs/images/banner.svg" alt="Love Sensation — Your library. After dark." width="100%">
</p>

<p align="center">
  <strong>A private NSFW image organizer for Windows.</strong><br>
  Sort large folders into categories, review every result, and keep your library on your own machine.
</p>

<p align="center">
  <a href="#get-started"><img src="https://img.shields.io/badge/Windows_11-ready-e8c58a?style=flat-square&labelColor=19181d" alt="Windows 11"></a>
  <img src="https://img.shields.io/badge/NVIDIA_CUDA-accelerated-c4c7cd?style=flat-square&labelColor=19181d" alt="NVIDIA CUDA accelerated">
  <a href="https://github.com/coldbricks/love-sensation/actions/workflows/tests.yml"><img src="https://github.com/coldbricks/love-sensation/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-a99b83?style=flat-square&labelColor=19181d" alt="AGPL-3.0 license"></a>
</p>

<p align="center">
  <a href="#get-started">Get started</a> · <a href="#how-it-works">How it works</a> · <a href="#face-crops">Face crops</a> · <a href="docs/USER_GUIDE.md">User guide</a>
</p>

![Love Sensation desktop organizer](docs/images/organizer.png)

<p align="center"><sub>Actual desktop interface with fabricated demo results. No personal library is pictured.</sub></p>

## Put your collection in order

| Feature | What you get |
| --- | --- |
| **Choose your categories** | Select exactly what belongs in your output, with confidence controls and one-click Select all / Deselect all. |
| **Review before filing** | Searchable results, category and confidence details, filters, and a separate Apply step. |
| **Use your GPU** | CUDA processing by default, half precision, bounded batch prefetch, and a visible compute status. |
| **Keep files intact** | Copy by default, SHA-256 verification, preserved subfolders, and numbered filenames when a destination already exists. |
| **Pick up where you left off** | Cached detections, saved reviews, recoverable filing journals, and JSON or CSV reports. |
| **Export face crops** | Preview detected faces, adjust padding, and save separate PNG files. |
| **Set an opening groove** | An optional one-bar startup cue from a local audio file, with preview, tempo, and mute controls. |

An original moon-and-spoon motif, silver details, and warm stage-light accents give the desktop its late-night character. Read the [design notes](docs/DESIGN.md) for the period references behind the look.

No account is required. Images are processed locally. Initial setup downloads dependencies and the 52 MB detection model; after setup, image analysis works locally without an upload service.

The opening groove is off by default. Choose your own WAV, MP3, FLAC, or OGG file; no music recording is included in the download.

## Get started

**You need:** Windows 11, a 64-bit Python installation (3.12–3.14), and space for the runtime and your output files. An NVIDIA GPU with a CUDA 12.8 compatible driver is recommended. CPU processing is available when CUDA is absent.

Open PowerShell in the folder where you want the application:

```powershell
git clone https://github.com/coldbricks/love-sensation.git
cd love-sensation
powershell -NoProfile -ExecutionPolicy Bypass -File .\Setup.ps1
```

You can also use **Code → Download ZIP**, extract it, and run the same setup command from the extracted folder. Setup creates a local virtual environment, installs the runtime, and verifies the compute device. The first installation can take several minutes.

Then double-click **Launch Love Sensation.vbs**, or run:

```powershell
.\.venv\Scripts\python.exe .\main.py
```

The launcher is a small script; this release is installed from source, with no standalone executable required. See the [user guide](docs/USER_GUIDE.md) for setup options and troubleshooting.

## How it works

1. **Choose folders.** Pick your source library and a separate output folder.
2. **Set your rules.** Choose categories, a confidence threshold, and the best, top three, or all matching categories. Copy is the default.
3. **Analyze.** Review filenames, categories, confidence, and any file errors. Your originals stay in place.
4. **Apply sorting.** The app verifies each destination and records the result. Open a saved run to resume interrupted filing.

Subfolders are preserved inside each category. Existing files receive a numbered suffix. In Move mode, every required destination copy is verified before the original is removed.

JPG, JPEG, PNG, WebP, BMP, and single-frame GIF/TIFF images are supported. Confirmed macOS metadata sidecars are ignored. Animated or multi-page files are reported as unsupported. This release handles still images.

## Face crops

After analysis, choose **Face crops…** in the results header. Preview a face, set padding from 0–30%, choose a folder, and export every eligible face in the list. Cropping creates new files and leaves the source images intact.

![Face crop preview and export](docs/images/face-crops.png)

<p align="center"><sub>Synthetic portrait and fabricated detections, used only to demonstrate the interface.</sub></p>

## Private by design

Your preferences, detection cache, and saved runs live in the local `data/` folder. These records include filenames, paths, and categories. Model files live in `models/`. Both folders are excluded from version control.

Sorting decisions can be imperfect. Use the review step and start with Copy when trying new settings. Confidence values are detection scores, not guarantees. See [file handling and recovery](docs/USER_GUIDE.md#file-handling-and-recovery) for the precise behavior.

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe .\main.py --diagnose
```

The automated suite uses temporary synthetic files. Additional desktop and GPU checks use ordinary sample photographs supplied with a dependency; they do not read your library. [Validation notes](VALIDATION.md) describe what has been checked and the limits of the measurements. [Contributing](CONTRIBUTING.md) covers the project structure and development workflow.

## License and acknowledgments

Application source is available under [AGPL-3.0](LICENSE). Love Sensation uses PySide6, PyTorch, torchvision, Ultralytics, and the NudeNet 640m detector. Third-party components retain their own licenses. Model weights are downloaded from the upstream release and are not bundled in this repository. See [third-party notices](THIRD_PARTY.md) for provenance and licensing references.
