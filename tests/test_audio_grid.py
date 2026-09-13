import math
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np
import torch
import wave
from unittest.mock import patch

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

        device='cuda' if torch.cuda.is_available() else 'cpu'
        t=torch.arange(24000*4,device=device)/24000
        phase=torch.remainder(t,.5)
        clicks=torch.sin(2*torch.pi*1100*t)*torch.exp(-180*phase)*(phase<.04)
        with wave.open(str(cls.audio_path),'wb') as wav:
            wav.setparams((1,2,24000,0,'NONE','not compressed'))
            wav.writeframes((clicks*30000).to(torch.int16).cpu().numpy().tobytes())

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
        self.assertTrue(grid['reliable'])
        self.assertAlmostEqual(grid['bpm'],120,places=1)
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

    def test_silence_has_no_invented_tempo(self):
        silence=torch.zeros(24000*4,device='cuda' if torch.cuda.is_available() else 'cpu').cpu().numpy()
        with patch('platinum_sorter.audio_grid.load_mono_audio',return_value=silence):
            grid=detect_tempo_and_grid('synthetic')
        self.assertFalse(grid['reliable'])
        self.assertIsNone(grid['bpm'])
        self.assertEqual(grid['bars'],[])

    def test_auto_tempo_on_real_click_fixture(self):
        # Sixteen seconds gives the estimator enough repeated beats to test
        # sub-BPM accuracy; a four-second excerpt has finite-window ambiguity.
        samples=load_mono_audio(self.audio_path)
        repeated=torch.from_numpy(samples).to('cuda' if torch.cuda.is_available() else 'cpu').repeat(4).cpu().numpy()
        with patch('platinum_sorter.audio_grid.load_mono_audio',return_value=repeated):
            grid=detect_tempo_and_grid(self.audio_path)
        self.assertTrue(grid['reliable'])
        self.assertAlmostEqual(grid['bpm'],120,delta=.3)

    def test_waveform_polygon_closes_in_reverse_x_order(self):
        points=generate_waveform_svg_points(samples=np.ones(100,dtype=np.float32),width=10,height=40)
        xs=[float(p.split(',')[0]) for p in points.split()]
        self.assertEqual(len(xs),20)
        self.assertEqual(xs[10:],list(reversed(xs[:10])))


if __name__ == "__main__":
    unittest.main()
