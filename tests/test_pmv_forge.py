import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

from platinum_sorter.pmv_forge import assemble_pmv_timeline, export_fcp7_xml, CutSlice


class PmvForgeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="test_pmv_forge_")
        self.dir_path = Path(self.temp_dir.name)

        self.audio_grid = {
            "bpm": 120.0,
            "beat_period": 0.5,
            "first_beat_s": 0.0,
            "total_duration_s": 8.0,
            "beats": [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5],
            "bars": [0.0, 2.0, 4.0, 6.0, 8.0, 10.0],
            "drop_bars": [4],  # Bar 4 is drop -> bar 3 is predrop (fill)
            "breakdown_bars": [1],  # Bar 1 is breakdown
        }

        self.clips = [
            {"path": "clip_a.mp4", "duration_s": 5.0, "prominence": 0.9, "aspect_ratio": 1.5},
            {"path": "clip_b.mp4", "duration_s": 5.0, "prominence": 0.4, "aspect_ratio": 1.2},
            {"path": "clip_c.mp4", "duration_s": 5.0, "prominence": 0.6, "aspect_ratio": 1.0},
        ]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_assemble_timeline_chaos_grammar(self):
        # Chaos 0.0 -> steady cuts
        cuts_square = assemble_pmv_timeline(self.audio_grid, self.clips, chaos=0.0, seed=42)
        self.assertGreater(len(cuts_square), 0)

        # Verify that bar 1 before drop 2 has fills
        tags = [c.tag for c in cuts_square]
        self.assertIn("fill", tags)
        self.assertIn("peak", tags)
        self.assertIn("breakdown", tags)
        self.assertEqual(cuts_square[-1].tag, "outro")

    def test_export_fcp7_xml(self):
        cuts = assemble_pmv_timeline(self.audio_grid, self.clips, chaos=0.5, seed=42)
        out_xml = self.dir_path / "assembly.xml"
        audio_file = self.dir_path / "song.wav"
        audio_file.write_bytes(b"RIFF dummy audio data")

        res_path = export_fcp7_xml(cuts, audio_file, out_xml, fps=30)
        self.assertTrue(res_path.is_file())

        # Verify XML structure
        tree = ET.parse(str(res_path))
        root = tree.getroot()
        self.assertEqual(root.tag, "xmeml")
        seq = root.find("sequence")
        self.assertIsNotNone(seq)
        tracks = seq.findall(".//track")
        self.assertGreaterEqual(len(tracks), 2)  # Video and Audio tracks
        markers = seq.findall("marker")
        self.assertGreaterEqual(len(markers), 1)

    def test_xml_gapless_frame_continuity(self):
        # High chaos produces multiple cuts per bar
        cuts = assemble_pmv_timeline(self.audio_grid, self.clips, chaos=0.8, seed=123)
        out_xml = self.dir_path / "gapless.xml"
        audio_file = self.dir_path / "song.wav"
        audio_file.write_bytes(b"RIFF dummy audio data")

        export_fcp7_xml(cuts, audio_file, out_xml, fps=30)
        tree = ET.parse(str(out_xml))
        video_track = tree.find(".//media/video/track")
        self.assertIsNotNone(video_track)

        clipitems = video_track.findall("clipitem")
        self.assertGreaterEqual(len(clipitems), 2)

        prev_end = None
        for item in clipitems:
            start = int(item.find("start").text)
            end = int(item.find("end").text)
            dur = int(item.find("duration").text)
            self.assertEqual(end - start, dur)
            if prev_end is not None:
                # Gapless continuity: each cut must begin exactly where the previous cut ended
                self.assertEqual(start, prev_end, f"Frame gap or overlap detected between cuts: prev end {prev_end}, next start {start}")
            prev_end = end

    def test_xml_stereo_audio_and_markers(self):
        cuts = assemble_pmv_timeline(self.audio_grid, self.clips, chaos=0.2, seed=99)
        out_xml = self.dir_path / "stereo_test.xml"
        audio_file = self.dir_path / "song.wav"
        audio_file.write_bytes(b"RIFF dummy audio data")

        export_fcp7_xml(cuts, audio_file, out_xml, fps=23.976)
        tree = ET.parse(str(out_xml))

        # Check NTSC and 24 timebase for 23.976
        rate = tree.find(".//rate")
        self.assertEqual(rate.find("timebase").text, "24")
        self.assertEqual(rate.find("ntsc").text, "TRUE")

        # Check stereo audio tracks: 2 tracks with sourcetrack indices 1 and 2
        audio_tracks = tree.findall(".//media/audio/track")
        self.assertEqual(len(audio_tracks), 2)
        ci1 = audio_tracks[0].find("clipitem")
        ci2 = audio_tracks[1].find("clipitem")
        self.assertIn("[L]", ci1.find("name").text)
        self.assertIn("[R]", ci2.find("name").text)
        self.assertEqual(ci1.find(".//sourcetrack/trackindex").text, "1")
        self.assertEqual(ci2.find(".//sourcetrack/trackindex").text, "2")

        # Verify marker colors exist
        markers = tree.findall(".//marker")
        self.assertGreater(len(markers), 0)
        marker_colors = {m.find("color").text for m in markers if m.find("color") is not None}
        self.assertTrue(len(marker_colors) >= 1)

    def test_transition_handles(self):
        cuts = assemble_pmv_timeline(self.audio_grid, self.clips, chaos=0.1, seed=5)
        for cut in cuts:
            # Handles must be non-negative
            self.assertGreaterEqual(cut.handle_in_s, 0.0)
            self.assertGreaterEqual(cut.handle_out_s, 0.0)
            # Duration must be positive
            self.assertGreater(cut.duration_s, 0.0)

    def test_xml_deduplication_and_pathurl_encoding(self):
        # Create cuts that deliberately reuse the same clip path
        single_clip_cuts = [
            CutSlice(
                clip_path=str(self.dir_path / "clip_repeated.mp4"),
                clip_name="clip_repeated.mp4",
                timeline_start_s=0.0,
                timeline_end_s=2.0,
                duration_s=2.0,
                clip_in_s=0.0,
                clip_out_s=2.0,
                tag="normal",
            ),
            CutSlice(
                clip_path=str(self.dir_path / "clip_repeated.mp4"),
                clip_name="clip_repeated.mp4",
                timeline_start_s=2.0,
                timeline_end_s=4.0,
                duration_s=2.0,
                clip_in_s=0.5,
                clip_out_s=2.5,
                tag="normal",
            ),
        ]
        out_xml = self.dir_path / "dedup.xml"
        audio_file = self.dir_path / "song.wav"
        audio_file.write_bytes(b"RIFF dummy audio data")

        export_fcp7_xml(single_clip_cuts, audio_file, out_xml, fps=30)
        content = out_xml.read_text(encoding="utf-8")

        # Windows drive letter must be encoded as %3A
        if ":" in str(self.dir_path):
            self.assertIn("%3A", content)

        # File node should have full definition once, and self-closing empty tag on reuse
        tree = ET.parse(str(out_xml))
        video_track = tree.find(".//media/video/track")
        file_nodes = video_track.findall(".//file")
        self.assertEqual(len(file_nodes), 2)
        # First file node has sub-elements like <name>, <pathurl>, <media>
        self.assertIsNotNone(file_nodes[0].find("pathurl"))
        # Second file node is deduplicated (no child nodes)
        self.assertEqual(len(list(file_nodes[1])), 0)
        self.assertEqual(file_nodes[0].get("id"), file_nodes[1].get("id"))

    def test_breakdown_transition_item(self):
        cuts = [
            CutSlice(
                clip_path="clip_a.mp4",
                clip_name="clip_a.mp4",
                timeline_start_s=0.0,
                timeline_end_s=2.0,
                duration_s=2.0,
                clip_in_s=0.0,
                clip_out_s=2.0,
                tag="normal",
            ),
            CutSlice(
                clip_path="clip_b.mp4",
                clip_name="clip_b.mp4",
                timeline_start_s=2.0,
                timeline_end_s=4.0,
                duration_s=2.0,
                clip_in_s=0.0,
                clip_out_s=2.0,
                tag="breakdown",
            ),
        ]
        out_xml = self.dir_path / "breakdown.xml"
        audio_file = self.dir_path / "song.wav"
        audio_file.write_bytes(b"RIFF dummy audio data")

        export_fcp7_xml(cuts, audio_file, out_xml, fps=30)
        tree = ET.parse(str(out_xml))
        transition = tree.find(".//media/video/track/transitionitem")
        self.assertIsNotNone(transition)
        self.assertEqual(transition.find(".//effect/name").text, "Cross Dissolve")

    def test_peak_cut_focal_alignment(self):
        clips = [
            {
                "path": "action_clip.mp4",
                "duration_s": 12.0,
                "prominence": 0.98,
                "best_timestamp_s": 6.0,
            }
        ]
        grid = {
            "bpm": 120.0,
            "beat_period": 0.5,
            "total_duration_s": 8.0,
            "beats": [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0],
            "bars": [0.0, 2.0, 4.0, 6.0],
            "drop_bars": [1],  # Bar 1 is drop
            "breakdown_bars": [],
        }
        cuts = assemble_pmv_timeline(grid, clips, chaos=0.0, seed=1)
        peak_cuts = [c for c in cuts if c.tag == "peak"]
        self.assertGreaterEqual(len(peak_cuts), 1)
        peak = peak_cuts[0]
        # Bar 1 duration is 2.0s -> clip should be sliced [5.0s, 7.0s] centered on 6.0s
        self.assertAlmostEqual(peak.clip_in_s, 5.0, places=1)
        self.assertAlmostEqual(peak.clip_out_s, 7.0, places=1)

    def test_assemble_timeline_null_and_malformed_grid(self):
        malformed_grid = {
            "bpm": None,
            "beat_period": None,
            "total_duration_s": None,
            "bars": None,
            "beats": None,
            "drop_bars": None,
            "breakdown_bars": None,
        }
        cuts = assemble_pmv_timeline(malformed_grid, self.clips)
        self.assertEqual(cuts, [])

        empty_clips_cuts = assemble_pmv_timeline(self.audio_grid, [])
        self.assertEqual(empty_clips_cuts, [])


if __name__ == "__main__":
    unittest.main()
