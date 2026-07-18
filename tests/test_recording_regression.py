import unittest
from pathlib import Path

from beast_motion import ReversalRepTracker, replay_items, tracker_config_for


PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]
LATEST_BENCH_RECORDING = (
    PROJECT_DIRECTORY
    / "outputs"
    / "recordings"
    / "beast-20260718-200019.jsonl"
)
SIXTEEN_REP_BENCH_RECORDING = (
    PROJECT_DIRECTORY
    / "outputs"
    / "recordings"
    / "beast-20260718-211135.jsonl"
)


@unittest.skipUnless(
    LATEST_BENCH_RECORDING.exists(),
    "local Beast regression recording is not available",
)
class LatestBenchRecordingRegressionTests(unittest.TestCase):
    def test_first_rest_closed_movement_is_recovered_without_recovering_gaps(
        self,
    ):
        tracker = ReversalRepTracker(tracker_config_for("bench"))
        events = []
        for item in replay_items(LATEST_BENCH_RECORDING):
            if item is None:
                tracker = ReversalRepTracker(tracker_config_for("bench"))
                continue
            current_events, _record = tracker.process(item)
            events.extend(current_events)

        repetitions = [event for event in events if event.kind == "rep"]
        self.assertGreater(len(repetitions), 0)
        self.assertEqual(
            repetitions[0].quality["top_detection"],
            "rest_orientation_fallback",
        )
        self.assertEqual(
            repetitions[0].quality["quality_status"],
            "recovered_top",
        )
        self.assertEqual(repetitions[0].quality["missing_samples"], 0)
        self.assertTrue(
            any(
                event.kind == "rejected"
                and "missing samples" in (event.reason or "")
                for event in events
            )
        )
        self.assertFalse(
            any(
                event.kind == "rep"
                and event.quality.get("missing_samples", 0) > 0
                for event in events
            )
        )


@unittest.skipUnless(
    SIXTEEN_REP_BENCH_RECORDING.exists(),
    "16-repetition Beast regression recording is not available",
)
class SixteenRepBenchRecordingRegressionTests(unittest.TestCase):
    def test_adaptive_regions_recover_exactly_sixteen_repetitions(self):
        tracker = ReversalRepTracker(tracker_config_for("bench"))
        timed_events = []
        ready_time_s = None
        for item in replay_items(SIXTEEN_REP_BENCH_RECORDING):
            if item is None:
                tracker = ReversalRepTracker(tracker_config_for("bench"))
                continue
            current_events, _record = tracker.process(item)
            for event in current_events:
                timed_events.append((tracker.sensor_time_s, event))
                if event.kind == "ready" and ready_time_s is None:
                    ready_time_s = tracker.sensor_time_s

        repetitions = [
            (time_s, event)
            for time_s, event in timed_events
            if event.kind == "rep"
        ]
        self.assertEqual(len(repetitions), 16)
        self.assertIsNotNone(ready_time_s)
        self.assertLess(ready_time_s, 5.0)
        self.assertFalse(
            any(23.0 < time_s < 58.0 for time_s, _event in repetitions)
        )
        self.assertGreaterEqual(
            sum(17.0 <= time_s <= 23.0 for time_s, _event in repetitions),
            3,
        )

        recovered_boundaries = [
            event
            for _time_s, event in repetitions
            if event.quality.get("top_detection")
            == "orientation_velocity_boundary"
        ]
        self.assertGreaterEqual(len(recovered_boundaries), 1)
        short_boundary = recovered_boundaries[0]
        self.assertTrue(short_boundary.quality["short_distance"])
        self.assertGreaterEqual(
            short_boundary.metrics["displacement_m"],
            0.03,
        )
        self.assertLess(
            short_boundary.metrics["displacement_m"],
            0.08,
        )
        self.assertAlmostEqual(
            short_boundary.quality["raw_final_velocity_m_s"],
            0.127,
            delta=0.04,
        )
        self.assertEqual(short_boundary.quality["missing_samples"], 0)
        self.assertEqual(
            short_boundary.quality["rate_confidence"],
            "high",
        )


if __name__ == "__main__":
    unittest.main()
