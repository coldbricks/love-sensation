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
    _h264_encoder,
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
            "-c:v", _h264_encoder(), "-pix_fmt", "yuv420p",
            str(cls.sample_video),
        ]
        subprocess.run(cmd, check=True)

        cls.sample_av_video = cls.dir_path / "test_synth_av.mp4"
        cmd_av = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", _h264_encoder(), "-pix_fmt", "yuv420p",
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
        manifest_path = takes[0].parent / "takes_manifest.json"
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

    def test_long_scene_tail_and_actual_manifest_duration(self):
        from unittest.mock import patch
        with patch('platinum_sorter.video_engine.detect_scene_cuts',return_value=[0.]):
            takes=split_compilation(self.sample_video,self.dir_path/'tail',max_take_duration=.8,min_take_duration=.1)
        manifest=json.loads((takes[0].parent/'takes_manifest.json').read_text())
        self.assertEqual(len(takes),3)
        self.assertAlmostEqual(manifest['takes'][-1]['requested_end_s'],2,places=2)
        self.assertAlmostEqual(sum(r['requested_duration_s'] for r in manifest['takes']),2,places=2)
        for path,record in zip(takes,manifest['takes']):
            self.assertEqual(record['duration_s'],probe_media_file(path)['duration_s'])

    def test_accurate_cut_duration_and_audio_preservation(self):
        from unittest.mock import patch
        with patch('platinum_sorter.video_engine.detect_scene_cuts',return_value=[0.,.7,1.3]):
            takes=split_compilation(self.sample_av_video,self.dir_path/'accurate',min_take_duration=.1,cut_mode='accurate')
        self.assertEqual(len(takes),3)
        for path,expected in zip(takes,[.7,.6,.7]):
            info=probe_media_file(path)
            self.assertAlmostEqual(info['duration_s'],expected,delta=.05)
            self.assertTrue(info['has_audio'])

    def test_sampling_reports_frame_times_and_cancels(self):
        import threading
        frames=sample_video_frames(self.sample_video,sample_fps=2,max_frames=4)
        self.assertEqual(len(frames),4)
        self.assertTrue(all(abs(ts*30-round(ts*30))<.001 for ts,_ in frames))
        self.assertTrue(all(a[0]<b[0] for a,b in zip(frames,frames[1:])))
        cancelled=threading.Event(); cancelled.set()
        with self.assertRaises(InterruptedError): sample_video_frames(self.sample_video,cancel_event=cancelled)

    def test_repeated_harvest_preserves_prior_takes_and_manifest(self):
        import hashlib
        from unittest.mock import patch
        destination=self.dir_path/'repeat'
        events=[]
        with patch('platinum_sorter.video_engine.detect_scene_cuts',side_effect=lambda *a,**kw:[0.]):
            first=split_compilation(self.sample_video,destination,min_take_duration=.1,emit=events.append)
            first_files=first+[first[0].parent/'takes_manifest.json']
            before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in first_files}
            second=split_compilation(self.sample_video,destination,min_take_duration=.1,emit=events.append)
        self.assertNotEqual(first[0].parent,second[0].parent)
        self.assertEqual(first[0].parent.parent,destination)
        self.assertEqual(second[0].parent.parent,destination)
        self.assertEqual(before,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in first_files})
        self.assertTrue((second[0].parent/'takes_manifest.json').is_file())
        started=[ev['output_dir'] for ev in events if ev['type']=='harvest_started']
        self.assertEqual(started,[str(first[0].parent),str(second[0].parent)])


if __name__ == "__main__":
    unittest.main()
