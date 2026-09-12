# Changelog

## 2.0.0

- **PMV Forge & Premiere Pro / FCP XML Sequence Exporter**: Musical edit assembly engine with Chaos Knob (0.0–1.0), pre-drop acceleration/retrigger fills, drop downbeat chokes/peaks, breakdown holds, and frame-quantized Final Cut Pro 7 / Premiere Pro XML (`<xmeml version="4">`) export with color-coded sequence markers (`DROP`, `FILL`, `CHOKE`, `PEAK`, `OUTRO`).
- **Compilation Harvester & Hardware Video Engine**: Fast FFmpeg/NVDEC-probed scene-cut detection and lossless stream-copy compilation splitting into reusable rhythm takes without transcoding.
- **Scale-Invariant WOW & Prominence Metrics**: Ratio math calculating area-normalized prominence ($\text{conf} \times \sqrt{\text{area}/\text{frame\_area}}$), aspect ratio ($w/h$), subject-to-face ratios, 85th-percentile sustained video score evaluation, and automated Ken Burns focal point coordinates.
- **CUDA Audio Beat-Grid & Waveform Radar**: Tensor-accelerated onset flux analysis, Fourier phase coherence BPM estimation, 4-beat bar lines, drop/breakdown RMS detection, and dynamic SVG waveform envelope rendering.
- **NTFS Hardlinks**: Instantaneous, zero-copy media organization on Windows NTFS drives via Win32 `CreateHardLinkW` with fallback to verified atomic file copying on cross-volume moves.
- **Interactive Flight Report Cockpit**: Standalone, zero-dependency HTML cockpit generator (`report.html`) featuring an embedded moon-and-spoon insignia, summary statistics, SVG waveform radar scope, colored cut timeline lane, and prominence-ranked media catalog.
- **Stealth Loupe & Panic Controls**: Instant floating frosted glass inspection HUD invoked via `Spacebar` or mouse hover, paired with instant `Esc` panic window close/wipe.
- **Streamlined Audio Stack**: Completely retired the incomplete one-bar startup audio cue and player in favor of full-fidelity musical analysis in the PMV Forge.

## 1.1.0

- First packaged source release of Love Sensation.
- Disco-era black, silver, and champagne desktop with original moon-and-spoon artwork.
- Folder pickers, search, and review filters.
- Category selection with Select all / Deselect all and persistent preferences.
- CUDA batch processing, half precision, bounded source prefetch, and visible throughput.
- Local detection cache and automatic recognition of macOS metadata sidecars.
- Verified copy/move operations, collision-safe naming, saved runs, and recovery journals.
- Face-only crop preview and PNG export with adjustable padding.
- Optional one-bar startup groove from a local audio file, with preview and mute.
- Portable Windows setup, synthetic test suite, and GitHub Actions checks.
