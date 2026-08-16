import unittest

from overtake_pipeline import (
    Observation,
    TrackHistory,
    deduplicate_candidates,
    evaluate_track,
)


def box(timestamp, center_x, bottom, width, height, confidence=0.9):
    return Observation(
        timestamp=timestamp,
        left=center_x - width / 2,
        top=bottom - height,
        right=center_x + width / 2,
        bottom=bottom,
        confidence=confidence,
    )


class EventDetectorTests(unittest.TestCase):
    def test_adjacent_class_switch_is_deduplicated(self):
        first = {
            "candidate": True,
            "track_id": 20,
            "side": "right",
            "peak_time": 10.0,
            "first_seen": 9.6,
            "last_seen": 10.4,
            "detections": 5,
            "duration": 0.8,
            "peak_area": 0.12,
        }
        second = {
            "candidate": True,
            "track_id": 21,
            "side": "right",
            "peak_time": 10.6,
            "first_seen": 10.4,
            "last_seen": 12.0,
            "detections": 9,
            "duration": 1.6,
            "peak_area": 0.06,
        }
        results = deduplicate_candidates([first, second])
        self.assertEqual(sum(item["candidate"] for item in results), 1)
        self.assertEqual(first["duplicate_of"], 21)

    def test_front_receding_vehicle_is_candidate(self):
        track = TrackHistory(track_id=10, class_id=2)
        for observation in [
            box(0.0, 0.82, 0.92, 0.36, 0.42),
            box(0.2, 0.78, 0.85, 0.31, 0.36),
            box(0.4, 0.72, 0.76, 0.25, 0.29),
            box(0.8, 0.65, 0.65, 0.18, 0.21),
            box(1.2, 0.59, 0.57, 0.13, 0.15),
        ]:
            track.add(observation)
        self.assertTrue(evaluate_track(track, "front")["candidate"])

    def test_front_approaching_vehicle_is_rejected(self):
        track = TrackHistory(track_id=11, class_id=2)
        for observation in [
            box(0.0, 0.60, 0.55, 0.10, 0.12),
            box(0.3, 0.65, 0.64, 0.15, 0.18),
            box(0.6, 0.72, 0.75, 0.22, 0.27),
            box(0.9, 0.80, 0.88, 0.34, 0.40),
        ]:
            track.add(observation)
        self.assertFalse(evaluate_track(track, "front")["candidate"])

    def test_cross_traffic_is_rejected(self):
        track = TrackHistory(track_id=14, class_id=2)
        for observation in [
            box(0.0, 0.78, 0.90, 0.34, 0.40),
            box(0.3, 0.63, 0.82, 0.28, 0.33),
            box(0.6, 0.47, 0.75, 0.22, 0.26),
            box(0.9, 0.30, 0.68, 0.16, 0.19),
            box(1.2, 0.16, 0.63, 0.11, 0.13),
        ]:
            track.add(observation)
        result = evaluate_track(track, "front")
        self.assertFalse(result["candidate"])
        self.assertFalse(result["centerline_consistent"])

    def test_rear_approaching_vehicle_is_candidate(self):
        track = TrackHistory(track_id=12, class_id=7)
        for observation in [
            box(0.0, 0.58, 0.54, 0.09, 0.11),
            box(0.3, 0.62, 0.62, 0.13, 0.16),
            box(0.6, 0.68, 0.72, 0.20, 0.24),
            box(0.9, 0.76, 0.84, 0.31, 0.37),
        ]:
            track.add(observation)
        self.assertTrue(evaluate_track(track, "rear")["candidate"])

    def test_short_track_is_rejected(self):
        track = TrackHistory(track_id=13, class_id=2)
        track.add(box(0.0, 0.80, 0.90, 0.35, 0.40))
        track.add(box(0.2, 0.70, 0.70, 0.15, 0.18))
        self.assertFalse(evaluate_track(track, "front")["candidate"])

    def test_single_endpoint_box_jitter_does_not_reject_real_front_pass(self):
        track = TrackHistory(track_id=15, class_id=2)
        for observation in [
            box(0.0, 0.82, 0.92, 0.36, 0.42),
            box(0.2, 0.78, 0.85, 0.31, 0.36),
            box(0.4, 0.72, 0.76, 0.25, 0.29),
            box(0.8, 0.65, 0.65, 0.18, 0.21),
            box(1.0, 0.59, 0.57, 0.13, 0.15),
            # One badly localized final box used to dominate endpoint tests.
            box(1.2, 0.84, 0.88, 0.30, 0.35),
        ]:
            track.add(observation)
        result = evaluate_track(track, "front")
        self.assertTrue(result["candidate"])
        self.assertEqual(result["geometry_filter"], "rolling_median_3")


if __name__ == "__main__":
    unittest.main()
