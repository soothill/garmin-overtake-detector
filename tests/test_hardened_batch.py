import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import summarize_batch
import validate_batch_result
import validate_combined_result
from review_skipped_events import all_detector_checks_pass, has_strong_track_evidence
from compose_paired_events import (
    build_clock_handoff_alignment,
    build_vehicle_handoff_alignment,
    compose_clip,
    estimate_event_offset,
    parse_overlay_clock,
    refine_offset_with_overlay,
)
from overtake_pipeline import write_heartbeat, write_reports

try:
    import numpy as np
    from calibrate_pair_offset import correlation_offset
except ModuleNotFoundError:
    np = None
    correlation_offset = None


class HardenedBatchTests(unittest.TestCase):
    def test_compositor_places_front_input_before_rear_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "combined.mp4"
            captured = []

            def fake_run(command, check):
                self.assertTrue(check)
                captured.append(command)
                Path(command[-1]).write_bytes(b"video")

            with patch("compose_paired_events.subprocess.run", side_effect=fake_run):
                compose_clip(
                    root / "rear.mp4",
                    root / "front.mp4",
                    output,
                    1.0,
                    2.0,
                    3.0,
                    1280,
                    720,
                )

            command = captured[0]
            filters = command[command.index("-filter_complex") + 1]
            self.assertIn("[0:v]", filters)
            self.assertIn("text=REAR", filters)
            self.assertIn("[1:v]", filters)
            self.assertIn("text=FRONT", filters)
            self.assertIn("[front][rear]hstack=inputs=2", filters)

    def test_skipped_review_requires_all_trajectory_checks(self):
        event = {
            "candidate": True,
            "class_id": 2,
            "max_confidence": 0.92,
            "detections": 18,
            "duration": 4.0,
            "peak_area": 0.08,
            "checks": {
                "detections": True,
                "duration": True,
                "peak_area": True,
                "area_ratio": True,
                "side_offset": True,
                "trajectory": True,
            },
        }
        self.assertTrue(all_detector_checks_pass(event))
        self.assertTrue(has_strong_track_evidence(event))
        event["checks"]["trajectory"] = False
        self.assertFalse(all_detector_checks_pass(event))
        self.assertFalse(has_strong_track_evidence(event))

    def test_skipped_review_routes_weaker_track_to_visual_review(self):
        event = {
            "candidate": True,
            "class_id": 7,
            "max_confidence": 0.69,
            "detections": 8,
            "duration": 3.0,
            "peak_area": 0.2,
            "checks": {
                name: True
                for name in (
                    "detections",
                    "duration",
                    "peak_area",
                    "area_ratio",
                    "side_offset",
                    "trajectory",
                )
            },
        }
        self.assertTrue(all_detector_checks_pass(event))
        self.assertFalse(has_strong_track_evidence(event))

    @unittest.skipIf(np is None, "NumPy is supplied by the production container")
    def test_audio_correlation_offset_sign(self):
        front = np.zeros(2000, dtype=np.float32)
        rear = np.zeros(2000, dtype=np.float32)
        front[500:510] = 1
        rear[700:710] = 1
        result = correlation_offset(front, rear, rate=20, limit=20)
        self.assertEqual(result["offset_seconds"], 10.0)

    def test_event_sequence_estimates_camera_clock_offset(self):
        front = [{"peak_time": value} for value in (105, 205, 305, 405)]
        rear = [{"peak_time": value} for value in (110, 210, 310, 410)]
        result = estimate_event_offset(rear, front, 5, 2, 100, 0)
        self.assertEqual(result["offset_seconds"], 10.0)
        self.assertEqual(result["matches"], 4)

    def test_vehicle_handoff_alignment_rejects_an_unstable_clock_match(self):
        front = [
            {"track_id": 11, "peak_time": 105, "first_seen": 101, "last_seen": 106},
            {"track_id": 12, "peak_time": 205, "first_seen": 201, "last_seen": 206},
            {"track_id": 13, "peak_time": 305, "first_seen": 260, "last_seen": 306},
        ]
        rear = [
            {"track_id": 21, "peak_time": 110, "first_seen": 104, "last_seen": 111},
            {"track_id": 22, "peak_time": 210, "first_seen": 204, "last_seen": 211},
            {"track_id": 23, "peak_time": 310, "first_seen": 304, "last_seen": 311},
        ]
        result = build_vehicle_handoff_alignment(rear, front, 5, 6, 1.5, 100, 0)
        self.assertEqual(result["method"], "vehicle_handoff_v1")
        self.assertEqual(result["offset_seconds"], 10.0)
        self.assertEqual(result["accepted_matches"], 2)
        self.assertTrue(
            all(abs(item["handoff_residual_seconds"]) <= 1.5 for item in result["matches"])
        )

    def test_clock_handoff_alignment_handles_missing_media_sections(self):
        def observation(track_id, media_time, clock_seconds):
            return {
                "event": {"track_id": track_id},
                "media_time": media_time,
                "clock_seconds": clock_seconds,
                "clock": str(clock_seconds),
            }

        rear = [observation(21, 111, 1000), observation(22, 500, 2000)]
        front = [
            observation(10, 50, 1000),  # tempting equal-clock false match
            observation(11, 101, 1014),
            observation(12, 300, 2014),
        ]
        result = build_clock_handoff_alignment(rear, front, 1.5, 120)
        self.assertEqual(result["method"], "vehicle_handoff_clock_v2")
        self.assertEqual(result["clock_bias_seconds"], -14.0)
        self.assertEqual(result["accepted_matches"], 2)
        self.assertEqual(
            [item["physical_offset_seconds"] for item in result["matches"]],
            [10.0, 200.0],
        )

    def test_combined_validator_requires_physical_handoff_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "output"
            result_dir = output_root / "batch" / "combined" / "date"
            clip = result_dir / "clips" / "pass.mp4"
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"video")
            payload = {
                "schema_version": 3,
                "alignment_method": "vehicle_handoff_clock_v2",
                "layout": "front-left_rear-right",
                "events_attempted": 1,
                "combined_clips": 1,
                "skipped_events": [],
                "calibration": {
                    "accepted_matches": 1,
                    "clock_match_tolerance_seconds": 1.5,
                },
                "events": [
                    {
                        "front_track_id": 12,
                        "clip": str(clip),
                        "media": {"width": 2560, "height": 720, "duration": 45},
                        "synchronization": {
                            "method": "vehicle_handoff_clock_v2",
                            "clock_match_residual_seconds": 0.4,
                        },
                    }
                ],
            }
            (result_dir / "combined.json").write_text(json.dumps(payload))
            (result_dir / "progress.json").write_text(json.dumps({"state": "complete"}))
            arguments = [
                "validate_combined_result.py",
                "--result-dir",
                str(result_dir),
                "--output-root",
                str(output_root),
            ]
            with patch("sys.argv", arguments), redirect_stdout(io.StringIO()):
                self.assertEqual(validate_combined_result.main(), 0)
            payload["alignment_method"] = "burned_in_clock_ocr"
            (result_dir / "combined.json").write_text(json.dumps(payload))
            with patch("sys.argv", arguments), redirect_stdout(io.StringIO()):
                self.assertEqual(validate_combined_result.main(), 1)

    def test_combined_validator_rejects_old_camera_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "output"
            result_dir = output_root / "batch" / "combined" / "date"
            clip = result_dir / "clips" / "pass.mp4"
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"video")
            (result_dir / "combined.json").write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "alignment_method": "vehicle_handoff_clock_v2",
                        "layout": "rear-left_front-right",
                        "events_attempted": 1,
                        "combined_clips": 1,
                        "skipped_events": [],
                        "calibration": {
                            "accepted_matches": 1,
                            "clock_match_tolerance_seconds": 1.5,
                        },
                        "events": [
                            {
                                "front_track_id": 12,
                                "clip": str(clip),
                                "media": {"width": 2560, "height": 720, "duration": 45},
                                "synchronization": {
                                    "method": "vehicle_handoff_clock_v2",
                                    "clock_match_residual_seconds": 0.4,
                                },
                            }
                        ],
                    }
                )
            )
            (result_dir / "progress.json").write_text(json.dumps({"state": "complete"}))
            arguments = [
                "validate_combined_result.py",
                "--result-dir",
                str(result_dir),
                "--output-root",
                str(output_root),
            ]
            with patch("sys.argv", arguments), redirect_stdout(io.StringIO()):
                self.assertEqual(validate_combined_result.main(), 1)

    def test_burned_in_clock_parser_ignores_other_overlay_fields(self):
        parsed = parse_overlay_clock(
            "GARMIN 22/05/2026 08:41:36 52.08517 -0.72872 20 MPH"
        )
        self.assertEqual(parsed.isoformat(), "2026-05-22T08:41:36")

    def test_burned_in_clock_parser_rejects_invalid_ocr_values(self):
        with self.assertRaisesRegex(RuntimeError, "camera timestamp is invalid"):
            parse_overlay_clock("GARMIN 22/45/2026 08:41:36")

    @patch("compose_paired_events.synchronize_clock_time")
    def test_clock_calibration_tries_another_event_after_bad_ocr(self, synchronize):
        synchronize.side_effect = [
            RuntimeError("unreadable overlay"),
            {"offset_seconds": 8.0},
        ]
        result = refine_offset_with_overlay(
            Path("front.mp4"),
            Path("rear.mp4"),
            {"anchor": {"rear_peak": 10}, "offset_seconds": 7, "matches": 3},
            100,
            100,
            [{"peak_time": 20}],
        )
        self.assertEqual(result["offset_seconds"], 8.0)
        self.assertEqual(len(result["failed_anchor_attempts"]), 1)

    def test_reports_and_heartbeat_are_complete_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            write_reports(output, {"candidate_events": 0}, [])
            write_heartbeat(output / "progress.json", "complete", frames=10)
            self.assertEqual(json.loads((output / "run.json").read_text())["events"], [])
            self.assertEqual(
                json.loads((output / "progress.json").read_text())["state"], "complete"
            )
            self.assertFalse(list(output.glob("*.tmp")))

    def test_validator_accepts_consistent_zero_event_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "output"
            result_dir = output_root / "batch/front/date/video"
            benchmark_dir = output_root / "benchmarks/job"
            result_dir.mkdir(parents=True)
            benchmark_dir.mkdir(parents=True)
            source = "/videos/varia-vue/date/video.mp4"
            run = {
                "source": source,
                "camera": "front",
                "weights": "/models/yolov8s.pt",
                "device": "0",
                "decode": "vaapi",
                "sample_fps": 5.0,
                "source_video": {"size": 100, "duration": 10.0},
                "processed_source_seconds": 10.0,
                "wall_seconds": 2.0,
                "candidate_events": 0,
                "completed_tracks": 0,
                "events": [],
            }
            (result_dir / "run.json").write_text(json.dumps(run))
            (result_dir / "events.csv").write_text(
                "track_id,class_name,peak_time,first_seen,last_seen,side,max_confidence,clip,paired_clip\n"
            )
            (result_dir / "tracks.jsonl").write_text("")
            (result_dir / "progress.json").write_text(
                json.dumps({"state": "complete", "source": source})
            )
            (benchmark_dir / "benchmark.json").write_text(
                json.dumps(
                    {
                        "samples": 2,
                        "telemetry_available": True,
                        "run": {
                            "source": source,
                            "processed_source_seconds": 10.0,
                        },
                    }
                )
            )
            arguments = [
                "validate_batch_result.py",
                "--source",
                source,
                "--camera",
                "front",
                "--duration",
                "10",
                "--size",
                "100",
                "--result-dir",
                str(result_dir),
                "--benchmark-dir",
                str(benchmark_dir),
                "--output-root",
                str(output_root),
            ]
            with patch("sys.argv", arguments), redirect_stdout(io.StringIO()):
                self.assertEqual(validate_batch_result.main(), 0)
            self.assertTrue(json.loads((result_dir / "validation.json").read_text())["valid"])

    def test_summary_does_not_count_unvalidated_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_dir = root / "result"
            benchmark_dir = root / "benchmark"
            result_dir.mkdir()
            benchmark_dir.mkdir()
            (result_dir / "run.json").write_text("{}")
            (benchmark_dir / "benchmark.json").write_text("{}")
            manifest = root / "manifest.tsv"
            with manifest.open("w", newline="") as handle:
                writer = csv.writer(handle, delimiter="\t")
                writer.writerow(
                    [
                        "camera",
                        "date",
                        "source",
                        "duration_seconds",
                        "size_bytes",
                        "result_dir",
                        "benchmark_dir",
                    ]
                )
                writer.writerow(
                    ["front", "date", "/videos/test.mp4", 10, 100, result_dir, benchmark_dir]
                )
            summary_json = root / "summary.json"
            arguments = [
                "summarize_batch.py",
                "--manifest",
                str(manifest),
                "--output-csv",
                str(root / "summary.csv"),
                "--output-json",
                str(summary_json),
            ]
            with patch("sys.argv", arguments), redirect_stdout(io.StringIO()):
                self.assertEqual(summarize_batch.main(), 0)
            payload = json.loads(summary_json.read_text())
            self.assertEqual(payload["overall"]["completed_files"], 0)
            self.assertEqual(payload["files"][0]["status"], "awaiting_validation")


if __name__ == "__main__":
    unittest.main()
