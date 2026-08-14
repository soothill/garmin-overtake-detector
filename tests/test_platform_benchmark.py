import unittest

from check_pi_health import parse_throttled
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

    from platform_video_benchmark import CommonTracker, Detection, _hailo_class_arrays
except ImportError:
    np = None
    CommonTracker = Detection = _hailo_class_arrays = None


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
    def test_hailo_tensorflow_nms_shape_is_normalized(self):
        raw = np.zeros((1, 80, 5, 100), dtype=np.float32)
        raw[0, 2, :, 0] = [0.1, 0.2, 0.8, 0.9, 0.75]
        classes = _hailo_class_arrays(raw)
        self.assertEqual(classes[2].shape, (100, 5))
        self.assertAlmostEqual(float(classes[2][0, 4]), 0.75)

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
