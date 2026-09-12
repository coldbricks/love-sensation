import unittest
from platinum_sorter.metrics import (
    box_area,
    box_prominence,
    box_aspect,
    subject_to_face_ratio,
    evaluate_frame_detections,
    sustained_85th_percentile,
    ken_burns_focal_point,
    video_prominence_profile,
)


class MetricsTests(unittest.TestCase):
    def test_box_prominence_scale_invariance(self):
        # Full frame box (640x640)
        full_box = [0, 0, 640, 640]
        prom_full = box_prominence(full_box, 640, 640, 0.9)
        self.assertAlmostEqual(prom_full, 0.9, places=2)

        # Quarter area box (320x320) -> sqrt(1/4) = 0.5 -> 0.9 * 0.5 = 0.45
        quarter_box = [160, 160, 320, 320]
        prom_quarter = box_prominence(quarter_box, 640, 640, 0.9)
        self.assertAlmostEqual(prom_quarter, 0.45, places=2)

        # Tiny background box (32x32) -> 1/400 area -> sqrt = 1/20 -> 0.9 * 0.05 = 0.045
        tiny_box = [10, 10, 32, 32]
        prom_tiny = box_prominence(tiny_box, 640, 640, 0.9)
        self.assertAlmostEqual(prom_tiny, 0.045, places=3)

    def test_box_aspect(self):
        wide_box = [0, 0, 400, 200]
        self.assertEqual(box_aspect(wide_box), 2.0)
        tall_box = [0, 0, 200, 400]
        self.assertEqual(box_aspect(tall_box), 0.5)

    def test_subject_to_face_ratio(self):
        subject = [100, 200, 400, 300]
        face = [150, 50, 100, 120]
        self.assertEqual(subject_to_face_ratio(subject, face), 4.0)

    def test_evaluate_frame_detections(self):
        detections = [
            {"class": "BUTTOCKS_EXPOSED", "score": 0.85, "box": [100, 100, 320, 320]},
            {"class": "FACE_FEMALE", "score": 0.95, "box": [200, 50, 80, 80]},
        ]
        result = evaluate_frame_detections(detections, 640, 640)
        self.assertGreater(result["max_prominence"], 0.4)
        self.assertEqual(result["face_count"], 1)
        self.assertEqual(result["best_detection"]["class"], "BUTTOCKS_EXPOSED")

    def test_sustained_85th_percentile(self):
        # 100 samples with 84 low scores (0.1) and 16 high scores (0.9)
        scores = [0.1] * 84 + [0.9] * 16
        sustained = sustained_85th_percentile(scores, 85.0)
        self.assertGreaterEqual(sustained, 0.8)

        # Mostly low with a single 1.0 fluke
        fluke_scores = [0.1] * 99 + [1.0]
        fluke_sustained = sustained_85th_percentile(fluke_scores, 85.0)
        self.assertLess(fluke_sustained, 0.2)

    def test_ken_burns_focal_point(self):
        detections = [
            {"class": "BUTTOCKS_EXPOSED", "score": 0.9, "box": [100, 200, 200, 200]},
            {"class": "FACE_FEMALE", "score": 0.5, "box": [10, 10, 50, 50]},
        ]
        center = ken_burns_focal_point(detections, 640, 640)
        # Center of [100, 200, 200, 200] is (200, 300)
        self.assertEqual(center, (200, 300))

    def test_video_prominence_profile_empty(self):
        res = video_prominence_profile([])
        self.assertEqual(res["sustained_wow"], 0.0)
        self.assertEqual(res["peak_prominence"], 0.0)
        self.assertEqual(res["frame_count"], 0)
        self.assertEqual(res["best_box"], [])

    def test_video_prominence_profile_sequence(self):
        # Frame 0 at 0.5s: modest prominence
        f0 = (0.5, [{"class": "BUTTOCKS_EXPOSED", "score": 0.6, "box": [50, 50, 100, 100]}])
        # Frame 1 at 1.5s: peak prominence
        f1 = (1.5, [{"class": "BUTTOCKS_EXPOSED", "score": 0.95, "box": [0, 0, 640, 640]}])
        # Frame 2 at 2.5s: low prominence
        f2 = (2.5, [{"class": "FACE_FEMALE", "score": 0.4, "box": [10, 10, 50, 50]}])

        res = video_prominence_profile([f0, f1, f2], frame_width=640, frame_height=640)
        self.assertEqual(res["frame_count"], 3)
        self.assertAlmostEqual(res["peak_prominence"], 0.95, places=2)
        self.assertEqual(res["peak_timestamp_s"], 1.5)
        self.assertEqual(res["best_box"], [0, 0, 640, 640])
        self.assertEqual(len(res["focal_trajectory"]), 3)
        self.assertEqual(res["focal_trajectory"][0][0], 0.5)
        self.assertEqual(res["focal_trajectory"][1][0], 1.5)
        self.assertGreater(res["sustained_wow"], 0.0)

    def test_metrics_hardened_edge_cases(self):
        # NaN / Inf coordinates in box
        nan_box = [float("nan"), 0, 100, 100]
        self.assertEqual(box_area(nan_box), 0.0)
        self.assertEqual(box_prominence(nan_box, 640, 640, 0.9), 0.0)
        self.assertEqual(box_aspect(nan_box), 0.0)

        inf_box = [0, 0, float("inf"), 100]
        self.assertEqual(box_area(inf_box), 0.0)
        self.assertEqual(box_prominence(inf_box, 640, 640, 0.9), 0.0)
        self.assertEqual(box_aspect(inf_box), 0.0)

        # Non-finite confidence
        self.assertEqual(box_prominence([0, 0, 100, 100], 640, 640, float("nan")), 0.0)
        self.assertEqual(box_prominence([0, 0, 100, 100], 640, 640, float("inf")), 0.0)

        # Zero or inverted frame dimensions
        self.assertEqual(box_prominence([0, 0, 100, 100], 0, 640, 0.9), 0.0)
        self.assertEqual(box_prominence([0, 0, 100, 100], -640, -640, 0.9), 0.0)

        # Subject to face ratio with zero/nan/inf face area
        self.assertEqual(subject_to_face_ratio([0, 0, 100, 100], [0, 0, 0, 0]), 0.0)
        self.assertEqual(subject_to_face_ratio([0, 0, 100, 100], nan_box), 0.0)
        self.assertEqual(subject_to_face_ratio(nan_box, [0, 0, 50, 50]), 0.0)

        # ken_burns_focal_point handles NaN/Inf and invalid dimensions safely
        self.assertEqual(ken_burns_focal_point([{"class": "FACE_FEMALE", "score": 0.8, "box": nan_box}], 640, 640), (320, 320))
        self.assertEqual(ken_burns_focal_point([], 0, 0), (0, 0))

    def test_evaluate_frame_detections_attaches_prominence_and_aspect(self):
        dets = [
            {"class": "ANUS_EXPOSED", "score": 0.9, "box": [100, 100, 320, 320]},
            {"class": "FACE_FEMALE", "score": 0.8, "box": [50, 50, 80, 80]},
        ]
        res = evaluate_frame_detections(dets, 640, 640)
        # Verify det dicts were enriched
        self.assertIn("prominence", dets[0])
        self.assertIn("aspect", dets[0])
        self.assertAlmostEqual(dets[0]["prominence"], 0.45, places=2)
        self.assertAlmostEqual(dets[0]["aspect"], 1.0, places=2)
        self.assertAlmostEqual(dets[1]["aspect"], 1.0, places=2)


if __name__ == "__main__":
    unittest.main()
