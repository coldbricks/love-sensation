import math
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from platinum_sorter.audio_grid import (
    load_mono_audio,
    detect_tempo_and_grid,
    generate_waveform_svg_points,
)


class AudioGridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="test_audio_grid_")
        cls.audio_path = Path(cls.temp_dir.name) / "click_120bpm.wav"

        # Generate a synthetic 4-second WAV with clicks at 120 BPM (every 0.5s = 2 Hz)
        # Using ffmpeg anevalsrc or synth
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi",
            "-i", "sine=frequency=440:duration=4",
            "-ar", "24000", "-ac", "1",
            str(cls.audio_path),
        ]
        subprocess.run(cmd, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_load_mono_audio(self):
        samples = load_mono_audio(self.audio_path, sr=24000)
        self.assertIsInstance(samples, np.ndarray)
        self.assertAlmostEqual(len(samples) / 24000, 4.0, places=1)

    def test_detect_tempo_and_grid(self):
        grid = detect_tempo_and_grid(self.audio_path, requested_bpm=120.0)
        self.assertIn("bpm", grid)
        self.assertIn("beats", grid)
        self.assertIn("bars", grid)
        self.assertIn("waveform_svg", grid)
        self.assertIsInstance(grid["waveform_svg"], str)
        self.assertGreater(len(grid["waveform_svg"]), 50)
        self.assertGreaterEqual(grid["beat_count"], 4)
        self.assertAlmostEqual(grid["total_duration_s"], 4.0, places=1)
        # Verify opening bars 0, 1, 2, 3 are NEVER marked as drops
        for drop_bar in grid["drop_bars"]:
            self.assertGreaterEqual(drop_bar, 4)

    def test_generate_waveform_svg_points(self):
        pts = generate_waveform_svg_points(self.audio_path, width=400, height=60)
        self.assertIsInstance(pts, str)
        self.assertGreater(len(pts), 50)
        self.assertIn(",", pts)

    def test_generate_waveform_svg_from_numpy_direct(self):
        # Direct generation from in-memory numpy array without disk I/O
        synth = np.sin(2 * np.pi * 440 * np.linspace(0, 1, 24000)).astype(np.float32)
        pts = generate_waveform_svg_points(samples=synth, width=200, height=40)
        self.assertIsInstance(pts, str)
        self.assertGreater(len(pts), 20)
        self.assertTrue(pts.startswith("0.0,") or pts.startswith("0,"))


if __name__ == "__main__":
    unittest.main()
