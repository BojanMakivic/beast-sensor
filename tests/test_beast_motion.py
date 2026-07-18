import json
import math
import struct
import tempfile
import unittest
from pathlib import Path

from beast_motion import (
    ReversalRepTracker,
    SessionRecorder,
    decode_imu_packet,
    replay_items,
    rotate_body_to_world,
)


DT = 0.020
GRAVITY = 9.80665


def packet(sequence: int, acceleration_m_s2: float = 0.0) -> bytes:
    z_mg = round((1.0 + acceleration_m_s2 / GRAVITY) * 1000.0)
    return struct.pack(
        "<Hhhhhhhh",
        sequence & 0xFFFF,
        0,
        0,
        0,
        32767,
        0,
        0,
        z_mg,
    )


def decoded(
    sequence: int,
    acceleration_m_s2: float = 0.0,
    host_timestamp: float | None = None,
):
    return decode_imu_packet(
        packet(sequence, acceleration_m_s2),
        sequence * DT if host_timestamp is None else host_timestamp,
    )


def feed_values(
    tracker: ReversalRepTracker,
    sequence: int,
    values,
    jittered_host_time: bool = False,
):
    events = []
    for index, value in enumerate(values):
        sequence = (sequence + 1) & 0xFFFF
        if jittered_host_time:
            host_timestamp = 50.0 if index % 3 == 0 else 900.0 + index
        else:
            host_timestamp = sequence * DT
        current_events, _record = tracker.process(
            decoded(sequence, value, host_timestamp)
        )
        events.extend(current_events)
    return sequence, events


def rest(tracker: ReversalRepTracker, sequence: int, seconds: float = 2.4):
    return feed_values(tracker, sequence, [0.0] * round(seconds / DT))


def phase_values(distance_m: float, duration_s: float, direction: float):
    count = round(duration_s / DT)
    return [
        direction
        * distance_m
        * math.pi**2
        / (2.0 * duration_s**2)
        * math.cos(math.pi * index * DT / duration_s)
        for index in range(count + 1)
    ]


def upward(distance_m: float, duration_s: float):
    return phase_values(distance_m, duration_s, 1.0)


def downward(distance_m: float, duration_s: float):
    return phase_values(distance_m, duration_s, -1.0)


class PacketTests(unittest.TestCase):
    def test_packet_layout_is_sequence_xyzw_then_acceleration(self):
        raw = struct.pack(
            "<Hhhhhhhh",
            513,
            0,
            0,
            0,
            32767,
            100,
            -200,
            1000,
        )
        sample = decode_imu_packet(raw, 99.0)
        self.assertIsNotNone(sample)
        self.assertEqual(sample.sequence, 513)
        self.assertEqual(sample.quaternion_xyzw, (0.0, 0.0, 0.0, 1.0))
        self.assertEqual(sample.acceleration_g, (0.1, -0.2, 1.0))
        self.assertAlmostEqual(sample.vertical_g, 1.0)

    def test_world_z_is_stable_after_sensor_rotation(self):
        root_half = math.sqrt(0.5)
        world = rotate_body_to_world(
            (0.0, root_half, 0.0, root_half),
            (-1.0, 0.0, 0.0),
        )
        self.assertAlmostEqual(world[0], 0.0, places=6)
        self.assertAlmostEqual(world[2], 1.0, places=6)

    def test_invalid_packets_are_ignored(self):
        self.assertIsNone(decode_imu_packet(b"\x00" * 10, 0.0))
        self.assertIsNone(decode_imu_packet(b"\x00" * 16, 0.0))


class ReversalTrackerTests(unittest.TestCase):
    def setUp(self):
        self.tracker = ReversalRepTracker()
        self.sequence = 0
        self.sequence, events = rest(self.tracker, self.sequence)
        self.assertTrue(any(event.kind == "ready" for event in events))

    def test_stationary_sensor_never_counts(self):
        self.sequence, events = rest(self.tracker, self.sequence, 60.0)
        self.assertFalse(any(event.kind == "rep" for event in events))
        self.assertEqual(self.tracker.state, self.tracker.REST)

    def test_upward_then_downward_is_one_rep(self):
        self.sequence, up_events = feed_values(
            self.tracker,
            self.sequence,
            upward(0.4, 1.0),
        )
        self.sequence, down_events = feed_values(
            self.tracker,
            self.sequence,
            downward(0.4, 1.0),
        )
        self.sequence, rest_events = rest(self.tracker, self.sequence, 0.4)
        events = up_events + down_events + rest_events
        reps = [event for event in events if event.kind == "rep"]
        self.assertEqual(len(reps), 1)
        self.assertTrue(any(event.kind == "top" for event in events))
        self.assertTrue(any(event.kind == "bottom" for event in events))
        self.assertGreater(reps[0].metrics["peak_speed_m_s"], 0.0)
        self.assertGreater(reps[0].metrics["displacement_m"], 0.0)

    def test_downward_phase_is_never_counted(self):
        self.sequence, events = feed_values(
            self.tracker,
            self.sequence,
            downward(0.4, 1.0),
        )
        self.assertFalse(any(event.kind == "rep" for event in events))

    def test_two_cycles_count_two_upward_phases(self):
        events = []
        for values in (
            upward(0.4, 0.9),
            downward(0.4, 0.9),
            upward(0.4, 1.2),
            downward(0.4, 1.2),
        ):
            self.sequence, current = feed_values(
                self.tracker,
                self.sequence,
                values,
            )
            events.extend(current)
        self.assertEqual(sum(event.kind == "rep" for event in events), 2)

    def test_short_and_long_travel_are_both_counted(self):
        for distance, duration in ((0.05, 0.45), (0.8, 1.4)):
            tracker = ReversalRepTracker()
            sequence, _ = rest(tracker, 0)
            sequence, events = feed_values(
                tracker,
                sequence,
                upward(distance, duration) + downward(distance, duration),
            )
            self.assertEqual(
                sum(event.kind == "rep" for event in events),
                1,
                (distance, duration),
            )

    def test_host_callback_jitter_does_not_change_result(self):
        smooth = ReversalRepTracker()
        jittered = ReversalRepTracker()
        smooth_sequence, _ = rest(smooth, 0)
        jitter_sequence, _ = rest(jittered, 0)
        values = upward(0.4, 1.0) + downward(0.4, 1.0)
        smooth_sequence, smooth_events = feed_values(
            smooth,
            smooth_sequence,
            values,
        )
        jitter_sequence, jitter_events = feed_values(
            jittered,
            jitter_sequence,
            values,
            jittered_host_time=True,
        )
        smooth_rep = next(event for event in smooth_events if event.kind == "rep")
        jitter_rep = next(
            event for event in jitter_events if event.kind == "rep"
        )
        self.assertEqual(smooth_rep.metrics, jitter_rep.metrics)

    def test_skipped_sequence_is_the_only_reported_gap(self):
        self.sequence, _ = feed_values(
            self.tracker,
            self.sequence,
            [1.0, 1.0, 1.0],
        )
        self.sequence += 3
        events, _record = self.tracker.process(decoded(self.sequence, 0.0))
        self.assertTrue(any(event.kind == "gap" for event in events))
        self.assertEqual(self.tracker.total_missing_samples, 2)


class RecordingTests(unittest.TestCase):
    def test_recording_replay_preserves_packets(self):
        tracker = ReversalRepTracker()
        sequence = 0
        samples = []
        for value in [0.0] * 120 + upward(0.3, 0.8):
            sequence += 1
            samples.append(decoded(sequence, value))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            recorder = SessionRecorder(path)
            recorder.mark_tracker_reset(0.0)
            original_reps = 0
            for sample in samples:
                events, record = tracker.process(sample)
                original_reps += sum(event.kind == "rep" for event in events)
                recorder.write(record)
            recorder.close()

            metadata = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(metadata["algorithm_version"], "generic-reversal-v1")
            replay = ReversalRepTracker()
            replay_reps = 0
            replayed_samples = 0
            for item in replay_items(path):
                if item is None:
                    replay = ReversalRepTracker()
                    continue
                replayed_samples += 1
                events, _record = replay.process(item)
                replay_reps += sum(event.kind == "rep" for event in events)

            self.assertEqual(replayed_samples, len(samples))
            self.assertEqual(replay_reps, original_reps)


if __name__ == "__main__":
    unittest.main()
