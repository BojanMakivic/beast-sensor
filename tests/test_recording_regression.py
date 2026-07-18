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
                and "no valid top" in (event.reason or "")
                for event in events
            )
        )
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


if __name__ == "__main__":
    unittest.main()
