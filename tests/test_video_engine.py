import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from platinum_sorter.video_engine import (
    is_video_path,
    is_animated_path,
    probe_media_file,
    sample_video_frames,
    detect_scene_cuts,
    split_compilation,
)


class VideoEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="test_video_engine_")
        cls.dir_path = Path(cls.temp_dir.name)
        cls.sample_video = cls.dir_path / "test_synth.mp4"

        # Generate a 2-second test video with color bars using ffmpeg
        cmd = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=30",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(cls.sample_video),
        ]
        subprocess.run(cmd, check=True)

        cls.sample_av_video = cls.dir_path / "test_synth_av.mp4"
        cmd_av = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(cls.sample_av_video),
        ]
        subprocess.run(cmd_av, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp_dir.cleanup()

    def test_extension_checks(self):
        self.assertTrue(is_video_path(Path("clip.mp4")))
        self.assertTrue(is_video_path(Path("clip.MKV")))
        self.assertTrue(is_video_path(Path("clip.webm")))
        self.assertFalse(is_video_path(Path("photo.jpg")))
        self.assertTrue(is_animated_path(Path("anim.gif")))

    def test_probe_media_file(self):
        info = probe_media_file(self.sample_video)
        self.assertEqual(info["width"], 320)
        self.assertEqual(info["height"], 240)
        self.assertGreaterEqual(info["duration_s"], 1.9)
        self.assertEqual(info["fps"], 30.0)
        self.assertGreaterEqual(info["frame_count"], 58)
        self.assertFalse(info["has_audio"])
        self.assertEqual(info["audio_channels"], 0)

        # Audiovisual file probe
        av_info = probe_media_file(self.sample_av_video)
        self.assertTrue(av_info["has_audio"])
        self.assertGreaterEqual(av_info["audio_channels"], 1)
        self.assertGreaterEqual(av_info["audio_sample_rate"], 8000)

        # Missing file graceful fallback
        missing_info = probe_media_file(Path("does_not_exist.mp4"))
        self.assertEqual(missing_info["duration_s"], 0.0)
        self.assertFalse(missing_info["has_audio"])

    def test_sample_video_frames(self):
        frames = sample_video_frames(self.sample_video, sample_fps=2.0, max_frames=5)
        self.assertGreaterEqual(len(frames), 2)
        ts, raw = frames[0]
        self.assertIsInstance(ts, float)
        self.assertTrue(raw.startswith(b"\xff\xd8"))  # JPEG SOI

    def test_scene_cuts_and_split(self):
        cuts = detect_scene_cuts(self.sample_video, adaptive=True)
        self.assertIn(0.0, cuts)

        out_dir = self.dir_path / "takes"
        takes = split_compilation(self.sample_video, out_dir, min_take_duration=0.5)
        self.assertGreaterEqual(len(takes), 1)
        self.assertTrue(takes[0].is_file())

        # Verify takes_manifest.json
        manifest_path = out_dir / "takes_manifest.json"
        self.assertTrue(manifest_path.is_file())
        import json
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["take_count"], len(takes))
        self.assertIn("takes", manifest)
        self.assertGreaterEqual(len(manifest["takes"]), 1)
        first_take = manifest["takes"][0]
        self.assertIn("take_file", first_take)
        self.assertIn("duration_s", first_take)
        self.assertGreaterEqual(first_take["duration_s"], 0.5)

    def test_split_min_take_duration(self):
        out_dir = self.dir_path / "takes_filtered"
        # 2-second video with min_take_duration=10.0 should produce 0 takes
        takes = split_compilation(self.sample_video, out_dir, min_take_duration=10.0)
        self.assertEqual(len(takes), 0)

    def test_split_cancellation(self):
        import threading
        out_dir = self.dir_path / "takes_cancel"
        cancel_event = threading.Event()
        cancel_event.set()
        takes = split_compilation(self.sample_video, out_dir, cancel_event=cancel_event)
        self.assertEqual(len(takes), 0)

    def test_probe_media_file_duration_na(self):
        from unittest.mock import patch, MagicMock
        fake_ffprobe_out = {
            "streams": [
                {
                    "codec_type": "video",
                    "width": 1280,
                    "height": 720,
                    "r_frame_rate": "30/1",
                    "duration": "N/A",
                    "nb_frames": "N/A",
                    "codec_name": "h264",
                }
            ],
            "format": {
                "duration": "N/A"
            }
        }
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps(fake_ffprobe_out)

        with patch("subprocess.run", return_value=mock_proc):
            info = probe_media_file(self.sample_video)
            self.assertEqual(info["width"], 1280)
            self.assertEqual(info["height"], 720)
            self.assertEqual(info["duration_s"], 0.0)
            self.assertEqual(info["codec"], "h264")

    def test_probe_corrupt_file(self):
        corrupt_path = self.dir_path / "corrupt.mp4"
        corrupt_path.write_bytes(b"NOT_A_VALID_VIDEO_STREAM_BYTES")
        info = probe_media_file(corrupt_path)
        self.assertEqual(info["duration_s"], 0.0)
        self.assertFalse(info["has_audio"])

    def test_sample_video_frames_full_timeline_distribution(self):
        # sample_video is 2.0s. Request sample_fps=10.0 with max_frames=3
        # duration * sample_fps = 20.0 > 3.0 -> triggers full timeline distribution
        frames = sample_video_frames(self.sample_video, sample_fps=10.0, max_frames=3)
        self.assertGreaterEqual(len(frames), 1)
        self.assertLessEqual(len(frames), 3)


if __name__ == "__main__":
    unittest.main()
