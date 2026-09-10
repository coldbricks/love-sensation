# Model and runtime provenance

Love Sensation is a local desktop organizer. Images are decoded and classified on the user's computer. Model setup downloads public weights; analysis does not upload images.

The detector uses the official **NudeNet 640m PyTorch model**, a YOLOv8m model trained at 640 x 640. It is the largest model documented by the NudeNet README; the old scripts used the package's default 320n model. A larger model does not by itself establish improved accuracy on a particular library.

- [NudeNet README](https://raw.githubusercontent.com/notAI-tech/NudeNet/v3/README.md)
- [Official model download](https://github.com/notAI-tech/NudeNet/releases/download/v3.4-weights/640m.pt)
- [Official release metadata](https://api.github.com/repos/notAI-tech/NudeNet/releases/tags/v3.4-weights)
- Binary asset ID: `176832117`; size: `52,023,681` bytes.
- SHA-256 of the downloaded official binary: `e6d7cddecc4417ff62db5b92c1c9f5d0d7b0f92e6cfd562120aea59a6ec3af3f`.

The implementation loads the model using Ultralytics and runs its PyTorch network directly. Resize, normalization, model inference and nonmaximum suppression use CUDA when present. Native half precision is used on CUDA. JPEG decoding tries torchvision's CUDA codec; other formats and unavailable GPU codecs use CPU decoding. CPU model inference is the fallback only when CUDA is absent. This application does not depend on ONNX Runtime or the installed NudeNet Python wrapper.

Licensing sources should be reviewed before redistribution. NudeNet's current repository contains an [AGPL-3.0 license](https://raw.githubusercontent.com/notAI-tech/NudeNet/v3/LICENSE), while [PyPI 3.4.2 metadata](https://pypi.org/project/nudenet/) labels the package MIT. The weights' licensing should not be inferred solely from that conflicting package metadata. [Ultralytics licensing](https://www.ultralytics.com/license) describes AGPL-3.0 and its commercial license options. [PySide6 licensing](https://doc.qt.io/qtforpython-6/licenses.html) describes Qt for Python licenses. No ownership claim is made over these dependencies or the model.

Fresh Windows setup installs the official PyTorch `2.10.0` / torchvision `0.25.0` CUDA 12.8 pair, or preserves an already compatible installation. GPU and precision are checked by observing a real convolution during detector startup; the status is also written by `main.py --diagnose`. Package versions and platform support are documented by [PyTorch](https://pytorch.org/get-started/previous-versions/).

The application source is released under AGPL-3.0; see `LICENSE`. This does not replace or remove the licenses, notices, or terms of its dependencies and upstream model. The repository includes no detector weights or bundled third-party runtime binaries.

Documentation screenshots contain fabricated result records and an original geometric portrait drawn by `tools/capture_readme.py`. They include no user library images or third-party sample photographs. Optional local integration tools refer to ordinary sample photos installed with Ultralytics; those photographs are not redistributed here.
