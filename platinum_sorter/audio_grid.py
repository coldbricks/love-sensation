"""Audio beat-tracking, musical grid detection, and SVG waveform generation.

CUDA-accelerated on PyTorch with CPU fallback. Analyzes tracks for BPM, downbeats,
bars, energy profiles, drops, and breakdowns for beat-locked PMV assembly.
"""
from __future__ import annotations

import logging
import math
import subprocess
from pathlib import Path
from typing import Sequence

import numpy as np
import torch

SR = 24000
HOP = 256


def load_mono_audio(path: Path | str, sr: int = SR) -> np.ndarray:
    """Decode audio file to mono float32 numpy array via ffmpeg."""
    path = str(path)
    cmd = [
        "ffmpeg", "-v", "error",
        "-i", path,
        "-ac", "1",
        "-ar", str(sr),
        "-f", "f32le",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else str(exc)
        raise ValueError(f"Failed to decode audio file {path}: {stderr}") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg is required for audio decoding but was not found in PATH.") from exc

    arr = np.frombuffer(proc.stdout, dtype=np.float32).copy()
    if len(arr) == 0:
        raise ValueError(f"No audio samples decoded from {path}")
    return arr


def compute_onset_envelope(wave: torch.Tensor, hop_size: int = HOP) -> tuple[torch.Tensor, torch.Tensor]:
    """Calculate spectral/energy onset strength envelope on GPU."""
    # Chunk into frames of length hop_size * 2
    frame_len = hop_size * 2
    if wave.numel() < frame_len:
        wave = torch.nn.functional.pad(wave, (0, frame_len - wave.numel()))

    unfolded = wave.unfold(0, frame_len, hop_size)
    window = torch.hann_window(frame_len, device=wave.device)
    windowed = unfolded * window
    # Short-time RMS energy per hop
    rms = torch.sqrt((windowed ** 2).mean(dim=-1) + 1e-9)
    # Half-wave rectified difference (onset flux)
    diff = torch.diff(rms, prepend=rms[:1])
    flux = torch.clamp_min(diff, 0.0)

    times = torch.arange(flux.numel(), device=wave.device, dtype=torch.float32) * (hop_size / SR)
    return flux, times


def detect_tempo_and_grid(
    audio_path: Path | str,
    requested_bpm: float | None = None,
    min_bpm: float = 75.0,
    max_bpm: float = 175.0,
) -> dict:
    """Detect BPM, beats, 4-beat bar lines, drops, and breakdowns from an audio track."""
    cuda_available = torch.cuda.is_available()
    device = torch.device("cuda:0" if cuda_available else "cpu")
    device_name = torch.cuda.get_device_name(0) if cuda_available else "CPU (CUDA unavailable)"
    logging.info("[AudioGrid] Compute path: %s (%s)", device_name, device)

    samples = load_mono_audio(audio_path, sr=SR)
    total_duration = float(len(samples) / SR)

    # Handle short audio snippet gracefully
    if total_duration < 1.0:
        bpm = requested_bpm or 120.0
        period = 60.0 / bpm
        return {
            "bpm": round(bpm, 2),
            "beat_period": round(period, 4),
            "first_beat_s": 0.0,
            "total_duration_s": round(total_duration, 3),
            "beat_count": max(1, int(total_duration / period)),
            "bar_count": 1,
            "beats": [0.0],
            "bars": [0.0],
            "drop_bars": [],
            "breakdown_bars": [],
            "energy_per_bar": [0.5],
            "device": str(device),
            "device_name": device_name,
        }

    wave = torch.from_numpy(samples).to(device)
    flux, times = compute_onset_envelope(wave, hop_size=HOP)

    # Search tempo via Fourier phase coherence (bounded window for long tracks)
    max_search_hops = int(180.0 / (HOP / SR))
    flux_search = flux[:max_search_hops] if flux.numel() > max_search_hops else flux
    times_search = times[:max_search_hops] if times.numel() > max_search_hops else times

    if requested_bpm and requested_bpm > 0:
        bpm_hypotheses = torch.linspace(requested_bpm - 4.0, requested_bpm + 4.0, 161, device=device)
    else:
        bpm_hypotheses = torch.linspace(min_bpm, max_bpm, int((max_bpm - min_bpm) / 0.1) + 1, device=device)

    centered = flux_search - flux_search.mean()
    # Batch compute power for all BPM candidates on GPU
    angles = 2 * torch.pi * bpm_hypotheses[:, None] * times_search[None, :] / 60.0
    real = angles.cos() @ centered
    imag = -(angles.sin() @ centered)
    powers = real.square() + imag.square()

    best_idx = int(powers.argmax().item()) if powers.numel() > 0 else 0
    detected_bpm = round(float(bpm_hypotheses[best_idx].item()), 2) if powers.numel() > 0 else (requested_bpm or 120.0)
    beat_period = 60.0 / detected_bpm

    # Phase calculation via comb-filter energy integration on GPU (locks to real downbeats)
    period_hops = beat_period / (HOP / SR)
    if period_hops >= 2.0 and flux.numel() > period_hops:
        int_period = max(2, int(round(period_hops)))
        nb = int(flux.numel() / period_hops)
        if nb >= 2:
            offs = torch.arange(int_period, device=device)
            k = torch.arange(nb, device=device)
            idx = (offs[:, None] + (k * period_hops).long()[None, :]).clamp(0, flux.numel() - 1)
            scores = flux[idx].sum(dim=1)
            best_offset_hops = int(offs[scores.argmax()].item())
            first_beat = round(float(best_offset_hops * (HOP / SR)), 4)
        else:
            first_beat = 0.0
    else:
        first_beat = 0.0

    # Generate complete beat grid
    beats = []
    t = first_beat
    while t < total_duration:
        beats.append(round(t, 4))
        t += beat_period

    # 4-beat bars (4/4 time)
    bar_len = 4 * beat_period
    bars = []
    t = first_beat
    while t < total_duration:
        bars.append(round(t, 4))
        t += bar_len

    # Per-bar energy profile (RMS)
    samples_cpu = samples
    energy_per_bar = []
    for i in range(len(bars)):
        t_start = bars[i]
        t_end = bars[i + 1] if i + 1 < len(bars) else total_duration
        idx_start = int(t_start * SR)
        idx_end = int(t_end * SR)
        chunk = samples_cpu[idx_start:idx_end]
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) > 0 else 0.0
        energy_per_bar.append(rms)

    # Detect drops and breakdowns (drops require post-intro bar >= 4)
    drop_bars = []
    breakdown_bars = []
    if energy_per_bar:
        median_e = float(np.median(energy_per_bar)) or 1e-4
        for b in range(1, len(energy_per_bar)):
            e_prev = energy_per_bar[b - 1]
            e_curr = energy_per_bar[b]
            # Drop: sudden jump of > 1.5x energy after build/lull, occurring after intro
            if b >= 4 and e_curr >= median_e * 0.9 and e_curr > e_prev * 1.5:
                drop_bars.append(b)
            # Breakdown: low energy lull (< 45% of median)
            if e_curr < median_e * 0.45:
                breakdown_bars.append(b)

    waveform_svg = generate_waveform_svg_points(samples=samples, width=800, height=80)

    return {
        "bpm": detected_bpm,
        "beat_period": round(beat_period, 4),
        "first_beat_s": first_beat,
        "total_duration_s": round(total_duration, 3),
        "beat_count": len(beats),
        "bar_count": len(bars),
        "beats": beats,
        "bars": bars,
        "drop_bars": drop_bars,
        "breakdown_bars": breakdown_bars,
        "energy_per_bar": [round(e, 4) for e in energy_per_bar],
        "waveform_svg": waveform_svg,
    }


def generate_waveform_svg_points(
    audio_path: Path | str | None = None,
    samples: np.ndarray | None = None,
    width: int = 800,
    height: int = 80,
) -> str:
    """Generate SVG polygon coordinate string for an audio waveform envelope."""
    if samples is None:
        if not audio_path:
            return f"0,{height/2} {width},{height/2}"
        try:
            samples = load_mono_audio(audio_path, sr=16000)
        except Exception:
            return f"0,{height/2} {width},{height/2}"

    if len(samples) == 0:
        return f"0,{height/2} {width},{height/2}"

    x = np.abs(samples)
    n = max(len(x) // width, 1)
    m = len(x) // n
    if m <= 0:
        return f"0,{height/2} {width},{height/2}"
    env = x[: m * n].reshape(m, n).max(axis=1)
    max_val = float(env.max()) or 1.0
    env = env / max_val

    mid = height / 2.0
    half = (height / 2.0) - 1.0

    top_pts = [f"{i * width / max(m, 1):.1f},{mid - e * half:.1f}" for i, e in enumerate(env)]
    bot_pts = [f"{i * width / max(m, 1):.1f},{mid + e * half:.1f}" for i, e in enumerate(reversed(env))]
    return " ".join(top_pts + bot_pts)
