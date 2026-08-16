import unittest

from check_pi_health import parse_throttled
from prepare_platform_review import EventNode, evenly_sample, union_find_components
from sample_pi_pmic import parse_pmic_output
from select_benchmark_pair import select_pair
from summarize_platform_power import summarize
from summarize_platform_results import (
    match_event_pairs,
    match_event_times,
    three_platform_consensus,
)
from wait_for_amd_idle import assess_window

try:
    import numpy as np

    from platform_video_benchmark import (
        CommonTracker,
        Detection,
        _hailo_class_arrays,
        configure_hailo_runtime_nms,
        letterbox_rgb,
        rgb_to_ultralytics_bgr,
    )
except ImportError:
    np = None
    CommonTracker = Detection = _hailo_class_arrays = configure_hailo_runtime_nms = None
    letterbox_rgb = rgb_to_ultralytics_bgr = None

try:
    from npu_detect_frames import decode_outputs
except ImportError:
    decode_outputs = None


class PlatformBenchmarkTests(unittest.TestCase):
    @unittest.skipIf(np is None, "NumPy is supplied by the benchmark runtimes")
    def test_common_tracker_keeps_a_moving_vehicle(self):
        tracker = CommonTracker()
        tracker.update(0.0, [Detection(2, 0.9, 0.70, 0.50, 0.90, 0.90)])
        tracker.update(0.2, [Detection(2, 0.9, 0.65, 0.45, 0.86, 0.84)])
        tracks = tracker.finish()
        self.assertEqual(len(tracks), 1)
        self.assertEqual(len(tracks[0].observations), 2)

    @unittest.skipIf(np is None, "NumPy is supplied by the benchmark runtimes")
    def test_rgb_contract_is_explicit_for_ultralytics(self):
        rgb = np.zeros((1, 2, 3), dtype=np.uint8)
        rgb[0, 0] = [11, 22, 33]
        canvas, _, _, _ = letterbox_rgb(rgb, 2)
        bgr = rgb_to_ultralytics_bgr(canvas)
        self.assertEqual(canvas[0, 0].tolist(), [11, 22, 33])
        self.assertEqual(bgr[0, 0].tolist(), [33, 22, 11])
        self.assertTrue(bgr.flags.c_contiguous)

    @unittest.skipIf(np is None, "NumPy is supplied by the benchmark runtimes")
    def test_low_confidence_detection_continues_but_does_not_start_track(self):
        tracker = CommonTracker(start_confidence=0.20, continuation_confidence=0.10)
        tracker.update(0.0, [Detection(2, 0.12, 0.70, 0.50, 0.90, 0.90)])
        self.assertEqual(tracker.finish(), [])

        tracker = CommonTracker(start_confidence=0.20, continuation_confidence=0.10)
        tracker.update(0.0, [Detection(2, 0.80, 0.70, 0.50, 0.90, 0.90)])
        tracker.update(0.2, [Detection(2, 0.12, 0.65, 0.45, 0.86, 0.84)])
        tracks = tracker.finish()
        self.assertEqual(len(tracks), 1)
        self.assertEqual(len(tracks[0].observations), 2)

    @unittest.skipIf(np is None, "NumPy is supplied by the benchmark runtimes")
    def test_vehicle_class_switch_does_not_split_track(self):
        tracker = CommonTracker()
        tracker.update(0.0, [Detection(2, 0.90, 0.70, 0.50, 0.90, 0.90)])
        tracker.update(0.2, [Detection(7, 0.70, 0.65, 0.45, 0.86, 0.84)])
        tracker.update(0.4, [Detection(2, 0.80, 0.60, 0.40, 0.82, 0.78)])
        tracks = tracker.finish()
        self.assertEqual(len(tracks), 1)
        self.assertEqual(len(tracks[0].observations), 3)
        self.assertEqual(tracks[0].class_id, 2)

    @unittest.skipIf(np is None, "NumPy is supplied by the benchmark runtimes")
    def test_hailo_tensorflow_nms_shape_is_normalized(self):
        raw = np.zeros((1, 80, 5, 100), dtype=np.float32)
        raw[0, 2, :, 0] = [0.1, 0.2, 0.8, 0.9, 0.75]
        classes = _hailo_class_arrays(raw)
        self.assertEqual(classes[2].shape, (100, 5))
        self.assertAlmostEqual(float(classes[2][0, 4]), 0.75)

    @unittest.skipIf(np is None, "NumPy is supplied by the benchmark runtimes")
    def test_hailo_runtime_nms_is_overridden(self):
        class Pipeline:
            def set_nms_score_threshold(self, value):
                self.score = value

            def set_nms_iou_threshold(self, value):
                self.iou = value

        pipeline = Pipeline()
        configure_hailo_runtime_nms(pipeline, 0.1, 0.5)
        self.assertEqual(pipeline.score, 0.1)
        self.assertEqual(pipeline.iou, 0.5)

    @unittest.skipIf(
        np is None or decode_outputs is None,
        "NumPy and ONNX Runtime are supplied by the NPU runtime",
    )
    def test_standard_yolov8_npu_output_is_decoded(self):
        output = np.zeros((1, 84, 8400), dtype=np.float32)
        output[0, :4, 0] = [320.0, 320.0, 200.0, 100.0]
        output[0, 4 + 2, 0] = 0.9
        detections = decode_outputs(
            [output],
            confidence=0.2,
            iou_threshold=0.5,
            scale=1.0,
            pad_left=0,
            pad_top=0,
            frame_width=640,
            frame_height=640,
        )
        self.assertEqual(len(detections), 1)
        self.assertEqual(detections[0]["class_id"], 2)
        self.assertEqual(detections[0]["box"], [220.0, 270.0, 420.0, 370.0])

    def test_amd_power_summary_includes_idle_adjusted_energy(self):
        rows = [
            {"timestamp": "0", "apu_average_socket_power": "10"},
            {"timestamp": "1", "apu_average_socket_power": "10"},
            {"timestamp": "2", "apu_average_socket_power": "30"},
            {"timestamp": "3", "apu_average_socket_power": "30"},
        ]
        result = summarize(rows, "amd-smi", 2, 4, 0, 1)
        metric = result["metrics"]["system_package"]
        self.assertAlmostEqual(metric["incremental_mean_watts"], 20.0)

    def test_pmic_parser_sums_named_rails(self):
        lines = []
        from sample_pi_pmic import RAILS

        for index, rail in enumerate(RAILS):
            lines.append(f" {rail}_A current({index})=1.00000000A")
            lines.append(f" {rail}_V volt({index + 20})=2.00000000V")
        powers = parse_pmic_output("\n".join(lines))
        self.assertEqual(sum(powers.values()), 2.0 * len(RAILS))

    def test_pi_throttle_status_distinguishes_current_and_historical_bits(self):
        status = parse_throttled("throttled=0x50000")
        self.assertEqual(status & 0xF, 0)
        self.assertEqual(status & 0xF0000, 0x50000)

    def test_pair_selector_prefers_target_then_camera_match(self):
        fronts = [
            {"path": "front-short", "duration_seconds": 100.0},
            {"path": "front-target", "duration_seconds": 200.0},
        ]
        rears = [
            {"path": "rear-short", "duration_seconds": 102.0},
            {"path": "rear-target", "duration_seconds": 198.0},
        ]
        front, rear = select_pair(fronts, rears, 200.0)
        self.assertEqual(front["path"], "front-target")
        self.assertEqual(rear["path"], "rear-target")

    def test_event_agreement_is_one_to_one(self):
        self.assertEqual(match_event_times([10.0, 11.0], [10.5], 2.0), 1)

    def test_event_agreement_reports_matched_indices_and_delta(self):
        self.assertEqual(
            match_event_pairs([10.0, 20.0], [20.25, 40.0], 1.0),
            [(1, 0, 0.25)],
        )

    def test_review_components_preserve_platform_only_and_shared_events(self):
        nodes = [
            EventNode("gpu", "front", 0, {"peak_time": 10.0}),
            EventNode("npu", "front", 0, {"peak_time": 10.5}),
            EventNode("hailo", "front", 0, {"peak_time": 30.0}),
        ]
        components = union_find_components(nodes, 2.0)
        self.assertEqual(len(components), 2)
        self.assertEqual({item.platform for item in components[0]}, {"gpu", "npu"})
        self.assertEqual({item.platform for item in components[1]}, {"hailo"})

    def test_even_review_sample_includes_both_ends(self):
        values = [[index] for index in range(10)]
        self.assertEqual(evenly_sample(values, 3), [[0], [4], [9]])

    def test_three_platform_consensus_counts_two_of_three_events(self):
        records = {
            "gpu": [{"peak_time": 10.0}, {"peak_time": 20.0}],
            "npu": [{"peak_time": 10.2}, {"peak_time": 30.0}],
            "hailo": [{"peak_time": 20.1}, {"peak_time": 30.2}],
        }
        result = three_platform_consensus(records, "gpu", 1.0)
        self.assertEqual(result["consensus_events"], 3)
        self.assertEqual(result["platform_supported_events"]["gpu"], 2)
        self.assertEqual(result["platform_supported_events"]["npu"], 2)
        self.assertEqual(result["platform_supported_events"]["hailo"], 2)

    def test_idle_gate_rejects_residual_gpu_power(self):
        samples = [
            {"timestamp": float(index), "system_package": 20.0, "gpu": 8.0, "npu": 0.0}
            for index in range(30)
        ]
        result = assess_window(
            samples,
            30,
            {"system_package": 30.0, "gpu": 2.0, "npu": 0.5},
            {"system_package": 40.0, "gpu": 5.0, "npu": 1.0},
        )
        self.assertFalse(result["valid"])
        self.assertIn("gpu_mean", result["failures"])

    def test_idle_gate_accepts_clean_window(self):
        samples = [
            {"timestamp": float(index), "system_package": 16.0, "gpu": 0.1, "npu": 0.0}
            for index in range(30)
        ]
        result = assess_window(
            samples,
            30,
            {"system_package": 30.0, "gpu": 2.0, "npu": 0.5},
            {"system_package": 40.0, "gpu": 5.0, "npu": 1.0},
        )
        self.assertTrue(result["valid"])
        self.assertEqual(result["start_epoch"], 0.0)
        self.assertEqual(result["end_epoch"], 29.0)


if __name__ == "__main__":
    unittest.main()
