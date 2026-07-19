import json
import math
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agile_vbt_motion import (
    AdaptiveSampleClock,
    ALGORITHM_VERSION,
    EXERCISE_PROFILES,
    ReversalRepTracker,
    SAMPLE_INTERVAL_S,
    SAMPLE_RATE_HZ,
    SessionRecorder,
    decode_imu_packet,
    recording_metadata,
    replay_items,
    rotate_body_to_world,
    tracker_config_for,
)


DT = SAMPLE_INTERVAL_S
GRAVITY = 9.80665
IDENTITY_QUATERNION = (0.0, 0.0, 0.0, 1.0)


def packet(
    sequence: int,
    acceleration_m_s2: float = 0.0,
    body_to_world=IDENTITY_QUATERNION,
    gravity_g: float = 1.0,
) -> bytes:
    x, y, z, w = body_to_world
    quaternion_norm = math.sqrt(x * x + y * y + z * z + w * w)
    x /= quaternion_norm
    y /= quaternion_norm
    z /= quaternion_norm
    w /= quaternion_norm

    # Agile VBT transmits the inverse (world-to-body) quaternion as x,y,w,z.
    device_quaternion = (-x, -y, w, -z)
    world_acceleration = (
        0.0,
        0.0,
        gravity_g + acceleration_m_s2 / GRAVITY,
    )
    body_acceleration = rotate_body_to_world(
        (
            device_quaternion[0],
            device_quaternion[1],
            device_quaternion[3],
            device_quaternion[2],
        ),
        world_acceleration,
    )
    return struct.pack(
        "<Hhhhhhhh",
        sequence & 0xFFFF,
        *(round(value * 32767.0) for value in device_quaternion),
        *(round(value * 1000.0) for value in body_acceleration),
    )


def decoded(
    sequence: int,
    acceleration_m_s2: float = 0.0,
    host_timestamp: float | None = None,
    body_to_world=IDENTITY_QUATERNION,
    gravity_g: float = 1.0,
):
    return decode_imu_packet(
        packet(
            sequence,
            acceleration_m_s2,
            body_to_world,
            gravity_g,
        ),
        sequence * DT if host_timestamp is None else host_timestamp,
    )


def feed_values(
    tracker: ReversalRepTracker,
    sequence: int,
    values,
    jittered_host_time: bool = False,
    body_to_world=IDENTITY_QUATERNION,
    gravity_g: float = 1.0,
):
    events = []
    for index, value in enumerate(values):
        sequence = (sequence + 1) & 0xFFFF
        if jittered_host_time:
            host_timestamp = 50.0 if index % 3 == 0 else 900.0 + index
        else:
            host_timestamp = sequence * DT
        current_events, _record = tracker.process(
            decoded(
                sequence,
                value,
                host_timestamp,
                body_to_world=body_to_world,
                gravity_g=gravity_g,
            )
        )
        events.extend(current_events)
    return sequence, events


def rest(
    tracker: ReversalRepTracker,
    sequence: int,
    seconds: float = 2.4,
    body_to_world=IDENTITY_QUATERNION,
    gravity_g: float = 1.0,
):
    return feed_values(
        tracker,
        sequence,
        [0.0] * round(seconds / DT),
        body_to_world=body_to_world,
        gravity_g=gravity_g,
    )


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


def axis_angle_quaternion(axis, angle):
    axis_norm = math.sqrt(sum(value * value for value in axis))
    scale = math.sin(angle / 2.0) / axis_norm
    return (
        axis[0] * scale,
        axis[1] * scale,
        axis[2] * scale,
        math.cos(angle / 2.0),
    )


def feed_oriented_values(tracker, sequence, values, orientations):
    events = []
    for value, orientation in zip(values, orientations, strict=True):
        sequence = (sequence + 1) & 0xFFFF
        current_events, _record = tracker.process(
            decoded(
                sequence,
                value,
                body_to_world=orientation,
            )
        )
        events.extend(current_events)
    return sequence, events


class PacketTests(unittest.TestCase):
    def test_packet_layout_is_sequence_xywz_then_acceleration(self):
        raw = struct.pack(
            "<Hhhhhhhh",
            513,
            0,
            0,
            32767,
            0,
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

    def test_real_packets_rotate_stationary_gravity_to_world_z(self):
        packets = [
            "5400de07f8f6c4832d1cb4005200e603",
            "52006fc777f740eef08e3603fbff6902",
            "5d00d51096090f57bf5b4d01b8ffbe03",
            "5e006db7af372542c4c3e103ad00e5ff",
        ]
        for packet_hex in packets:
            with self.subTest(packet_hex=packet_hex):
                sample = decode_imu_packet(bytes.fromhex(packet_hex), 0.0)
                self.assertIsNotNone(sample)
                horizontal_g = math.hypot(
                    sample.world_acceleration_g[0],
                    sample.world_acceleration_g[1],
                )
                self.assertLess(horizontal_g, 0.05)
                self.assertAlmostEqual(sample.vertical_g, 1.025, delta=0.02)

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

    def test_stable_gravity_shift_is_relearned_without_timeout_loop(self):
        tracker = ReversalRepTracker()
        sequence, ready_events = rest(
            tracker,
            0,
            2.4,
            gravity_g=1.028,
        )
        self.assertTrue(any(event.kind == "ready" for event in ready_events))
        sequence, events = rest(
            tracker,
            sequence,
            3.0,
            gravity_g=1.114,
        )
        self.assertFalse(any(event.kind == "rep" for event in events))
        timeout_rejections = [
            event
            for event in events
            if event.kind == "rejected"
            and "no valid top" in (event.reason or "")
        ]
        self.assertLessEqual(len(timeout_rejections), 1)
        self.assertEqual(tracker.state, tracker.REST)
        self.assertAlmostEqual(
            tracker.gravity_baseline_g,
            1.114,
            delta=0.003,
        )

    def test_constant_speed_segment_is_not_called_rest(self):
        self.sequence, start_events = feed_values(
            self.tracker,
            self.sequence,
            [1.2] * 15,
        )
        self.assertTrue(
            any(event.kind == "up_started" for event in start_events)
        )
        self.sequence, cruise_events = feed_values(
            self.tracker,
            self.sequence,
            [0.0] * 100,
        )
        self.assertFalse(any(event.kind == "rest" for event in cruise_events))
        self.assertEqual(self.tracker.state, self.tracker.UP)

    def test_bottom_braking_candidate_rearms_before_next_real_rep(self):
        self.tracker.positive_start_samples = [1.0] * 4
        self.tracker.start_buffer_from_bottom = True
        self.tracker._start_up_from_rest()
        self.sequence, settling_events = feed_values(
            self.tracker,
            self.sequence,
            [0.0] * 60,
        )
        self.assertTrue(
            any(
                event.kind == "rejected"
                and "bottom braking impulse" in (event.reason or "")
                for event in settling_events
            )
        )
        self.assertEqual(self.tracker.state, self.tracker.REST)

        self.sequence, rep_events = feed_values(
            self.tracker,
            self.sequence,
            upward(0.35, 0.8) + downward(0.35, 0.8),
        )
        self.assertEqual(
            sum(event.kind == "rep" for event in rep_events),
            1,
        )

    def test_rest_and_orientation_recover_a_drifted_top(self):
        tracker = ReversalRepTracker(tracker_config_for("bench"))
        sequence, _ = rest(tracker, 0)

        up_values = [
            value + 1.20
            for value in upward(0.35, 0.8)
        ]
        final_orientation = axis_angle_quaternion(
            (1.0, 0.0, 0.0),
            math.radians(45.0),
        )
        orientations = [
            axis_angle_quaternion(
                (1.0, 0.0, 0.0),
                math.radians(45.0) * index / (len(up_values) - 1),
            )
            for index in range(len(up_values))
        ]
        sequence, up_events = feed_oriented_values(
            tracker,
            sequence,
            up_values,
            orientations,
        )
        sequence, settling_events = rest(
            tracker,
            sequence,
            0.8,
            body_to_world=final_orientation,
        )
        events = up_events + settling_events
        recovered = [
            event
            for event in events
            if event.kind == "rep"
            and event.quality.get("top_detection")
            == "rest_orientation_fallback"
        ]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(
            recovered[0].quality["quality_status"],
            "recovered_top",
        )
        self.assertGreater(
            recovered[0].quality["orientation_excursion_deg"],
            10.0,
        )
        self.assertAlmostEqual(recovered[0].trace[-1].velocity_m_s, 0.0)

    def test_orientation_change_alone_never_counts(self):
        orientations = [
            axis_angle_quaternion(
                (0.0, 1.0, 0.0),
                math.radians(55.0)
                * math.sin(2.0 * math.pi * index / 40.0),
            )
            for index in range(120)
        ]
        self.sequence, events = feed_oriented_values(
            self.tracker,
            self.sequence,
            [0.0] * len(orientations),
            orientations,
        )
        self.assertFalse(any(event.kind == "rep" for event in events))

    def test_multiple_orientation_peaks_recover_only_one_rep(self):
        tracker = ReversalRepTracker(tracker_config_for("bench"))
        sequence, _ = rest(tracker, 0)
        sequence, _ = feed_values(
            tracker,
            sequence,
            downward(0.35, 0.9),
        )
        up_values = [
            value + 0.65
            for value in upward(0.35, 0.8)
        ]
        orientations = [
            axis_angle_quaternion(
                (1.0, 0.0, 0.0),
                math.radians(35.0)
                * math.sin(4.0 * math.pi * index / len(up_values)),
            )
            for index in range(len(up_values))
        ]
        sequence, movement_events = feed_oriented_values(
            tracker,
            sequence,
            up_values,
            orientations,
        )
        sequence, rest_events = rest(tracker, sequence, 0.8)
        events = movement_events + rest_events
        self.assertEqual(sum(event.kind == "rep" for event in events), 1)

    def test_slow_bench_rep_uses_one_orientation_region(self):
        tracker = ReversalRepTracker(tracker_config_for("bench"))
        sequence, _ = rest(tracker, 0, 5.0)
        values = upward(0.8, 5.0)
        orientations = [
            axis_angle_quaternion(
                (1.0, 0.0, 0.0),
                math.radians(180.0)
                * (
                    index / (len(values) - 1)
                    if index <= (len(values) - 1) / 2
                    else 1.0 - index / (len(values) - 1)
                )
                * 2.0,
            )
            for index in range(len(values))
        ]
        sequence, movement_events = feed_oriented_values(
            tracker,
            sequence,
            values,
            orientations,
        )
        sequence, settling_events = rest(tracker, sequence, 0.8)
        events = movement_events + settling_events
        repetitions = [event for event in events if event.kind == "rep"]
        self.assertEqual(len(repetitions), 1)
        self.assertGreater(repetitions[0].metrics["duration_s"], 4.0)
        self.assertTrue(
            repetitions[0].quality["slow_orientation_movement"]
        )

    def test_tiny_velocity_lobe_is_rejected(self):
        self.sequence, events = feed_values(
            self.tracker,
            self.sequence,
            upward(0.02, 0.4) + downward(0.02, 0.4),
        )
        self.assertFalse(any(event.kind == "rep" for event in events))
        self.assertTrue(any(event.kind == "rejected" for event in events))

    def test_fixed_mounting_angle_does_not_change_rep_metrics(self):
        orientations = [
            IDENTITY_QUATERNION,
            axis_angle_quaternion((1.0, 0.0, 0.0), math.pi / 2.0),
            axis_angle_quaternion((0.0, 1.0, 0.0), -math.pi / 2.0),
            axis_angle_quaternion((1.0, -2.0, 0.5), 1.1),
        ]
        values = upward(0.4, 1.0) + downward(0.4, 1.0)
        metrics = []
        for orientation in orientations:
            tracker = ReversalRepTracker()
            sequence, ready_events = rest(
                tracker,
                0,
                body_to_world=orientation,
            )
            self.assertTrue(
                any(event.kind == "ready" for event in ready_events)
            )
            sequence, events = feed_values(
                tracker,
                sequence,
                values,
                body_to_world=orientation,
            )
            reps = [event for event in events if event.kind == "rep"]
            self.assertEqual(len(reps), 1)
            metrics.append(reps[0].metrics)

        reference = metrics[0]
        for current in metrics[1:]:
            for name, expected in reference.items():
                self.assertAlmostEqual(current[name], expected, delta=0.002)

    def test_changing_orientation_preserves_world_vertical_motion(self):
        values = upward(0.4, 1.0) + downward(0.4, 1.0)

        reference_tracker = ReversalRepTracker()
        reference_sequence, _ = rest(reference_tracker, 0)
        reference_sequence, reference_events = feed_values(
            reference_tracker,
            reference_sequence,
            values,
        )
        reference_rep = next(
            event for event in reference_events if event.kind == "rep"
        )

        rotating_tracker = ReversalRepTracker()
        rotating_sequence, ready_events = rest(rotating_tracker, 0)
        self.assertTrue(any(event.kind == "ready" for event in ready_events))
        orientations = [
            axis_angle_quaternion(
                (1.0, 0.2, 0.0),
                (math.pi / 2.0) * index / (len(values) - 1),
            )
            for index in range(len(values))
        ]
        rotating_sequence, rotating_events = feed_oriented_values(
            rotating_tracker,
            rotating_sequence,
            values,
            orientations,
        )
        rotating_rep = next(
            event for event in rotating_events if event.kind == "rep"
        )

        for name, expected in reference_rep.metrics.items():
            self.assertAlmostEqual(
                rotating_rep.metrics[name],
                expected,
                delta=0.002,
            )

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

    def test_five_continuous_bench_repetitions_count_exactly_five(self):
        tracker = ReversalRepTracker(tracker_config_for("bench"))
        sequence, _ = rest(tracker, 0, 5.0)
        events = []
        for _ in range(5):
            sequence, current = feed_values(
                tracker,
                sequence,
                downward(0.35, 0.9) + upward(0.35, 0.8),
            )
            events.extend(current)
        sequence, top_settling = rest(tracker, sequence, 0.5)
        sequence, final_rest = rest(tracker, sequence, 4.5)
        events.extend(top_settling)
        events.extend(final_rest)
        self.assertEqual(sum(event.kind == "rep" for event in events), 5)
        self.assertFalse(
            any(
                event.kind == "rep"
                for event in final_rest
            )
        )

    def test_drift_correction_ends_at_zero_velocity(self):
        self.sequence, events = feed_values(
            self.tracker,
            self.sequence,
            upward(0.4, 1.0) + downward(0.4, 1.0),
        )
        repetition = next(event for event in events if event.kind == "rep")
        self.assertIsNotNone(repetition.trace)
        self.assertAlmostEqual(repetition.trace[0].velocity_m_s, 0.0)
        self.assertAlmostEqual(repetition.trace[-1].velocity_m_s, 0.0)
        self.assertAlmostEqual(
            repetition.trace[-1].displacement_m,
            repetition.metrics["displacement_m"],
        )
        self.assertIn("drift_correction_m_s", repetition.quality)

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

    def test_gap_during_motion_requires_rest_before_rearming(self):
        self.sequence, _ = feed_values(
            self.tracker,
            self.sequence,
            upward(0.4, 1.0)[:15],
        )
        self.sequence += 2
        gap_events, _record = self.tracker.process(
            decoded(self.sequence, 0.0)
        )
        self.assertTrue(any(event.kind == "gap" for event in gap_events))
        self.assertTrue(any(event.kind == "rejected" for event in gap_events))
        self.assertEqual(self.tracker.state, self.tracker.RECOVERY)
        self.sequence, recovery_events = rest(
            self.tracker,
            self.sequence,
            0.6,
        )
        self.assertTrue(any(event.kind == "rest" for event in recovery_events))
        self.assertEqual(self.tracker.state, self.tracker.REST)


class ExerciseProfileTests(unittest.TestCase):
    def test_profile_defaults_match_initial_gates(self):
        expected = {
            "generic": (0.15, 5.0, 0.03, 0.10),
            "bench": (0.15, 4.0, 0.08, 0.10),
            "squat": (0.20, 5.0, 0.10, 0.10),
            "deadlift": (0.20, 5.0, 0.15, 0.10),
        }
        for name, gates in expected.items():
            with self.subTest(name=name):
                profile = EXERCISE_PROFILES[name]
                self.assertEqual(
                    (
                        profile.min_duration_s,
                        profile.max_duration_s,
                        profile.min_displacement_m,
                        profile.min_peak_velocity_m_s,
                    ),
                    gates,
                )

    def test_profile_validation_rejects_distance_below_its_gate(self):
        for name in EXERCISE_PROFILES:
            with self.subTest(name=name):
                tracker = ReversalRepTracker(tracker_config_for(name))
                profile = tracker.profile
                displacement = (
                    0.029
                    if name == "bench"
                    else profile.min_displacement_m - 0.001
                )
                failures = tracker._metric_failures(
                    {
                        "duration_s": profile.min_duration_s + 0.2,
                        "displacement_m": displacement,
                        "average_speed_m_s": 0.5,
                        "peak_speed_m_s": max(
                            0.5,
                            profile.min_peak_velocity_m_s,
                        ),
                    }
                )
                self.assertTrue(
                    any("displacement below" in failure for failure in failures)
                )

    def test_bench_short_distance_is_countable_but_flagged(self):
        tracker = ReversalRepTracker(tracker_config_for("bench"))
        self.assertEqual(
            tracker._metric_failures(
                {
                    "duration_s": 0.40,
                    "displacement_m": 0.05,
                    "average_speed_m_s": 0.125,
                    "peak_speed_m_s": 0.20,
                }
            ),
            [],
        )
        self.assertTrue(
            any(
                "displacement below" in failure
                for failure in tracker._metric_failures(
                    {
                        "duration_s": 0.40,
                        "displacement_m": 0.029,
                        "average_speed_m_s": 0.07,
                        "peak_speed_m_s": 0.20,
                    }
                )
            )
        )

    def test_each_profile_accepts_a_valid_movement_shape(self):
        for name in EXERCISE_PROFILES:
            with self.subTest(name=name):
                tracker = ReversalRepTracker(tracker_config_for(name))
                sequence, _ = rest(tracker, 0)
                if name in {"bench", "squat"}:
                    values = (
                        downward(0.35, 0.9)
                        + upward(0.35, 0.8)
                    )
                else:
                    values = (
                        upward(0.35, 0.8)
                        + downward(0.35, 0.9)
                    )
                sequence, events = feed_values(
                    tracker,
                    sequence,
                    values,
                )
                sequence, settling = rest(tracker, sequence, 0.6)
                events.extend(settling)
                self.assertEqual(
                    sum(event.kind == "rep" for event in events),
                    1,
                )


class AdaptiveSampleClockTests(unittest.TestCase):
    def test_long_sequence_windows_adapt_across_supported_rates(self):
        for actual_rate_hz in (44.0, 49.0):
            with self.subTest(actual_rate_hz=actual_rate_hz):
                clock = AdaptiveSampleClock()
                for sequence in range(1, round(actual_rate_hz * 40.0)):
                    clock.observe(sequence, sequence / actual_rate_hz)
                self.assertAlmostEqual(
                    clock.rate_hz,
                    actual_rate_hz,
                    delta=0.40,
                )
                self.assertEqual(clock.confidence, "high")

    def test_bursty_or_invalid_callback_times_keep_bounded_fallback(self):
        clock = AdaptiveSampleClock()
        for sequence in range(1, 300):
            host_timestamp = (
                50.0
                if sequence % 3 == 0
                else 900.0 + sequence
            )
            clock.observe(sequence, host_timestamp)
        self.assertGreaterEqual(clock.rate_hz, 43.0)
        self.assertLessEqual(clock.rate_hz, 52.0)
        self.assertAlmostEqual(clock.rate_hz, SAMPLE_RATE_HZ)

    def test_new_clock_resets_estimation_after_reconnect(self):
        clock = AdaptiveSampleClock()
        for sequence in range(1, 1000):
            clock.observe(sequence, sequence / 44.0)
        self.assertLess(clock.rate_hz, SAMPLE_RATE_HZ)
        reconnected = AdaptiveSampleClock()
        self.assertEqual(reconnected.rate_hz, SAMPLE_RATE_HZ)
        self.assertEqual(reconnected.confidence, "fallback")


class RecordingTests(unittest.TestCase):
    def test_recording_writer_reports_background_failures(self):
        class FailingFile:
            def write(self, _text):
                raise OSError("disk failed")

            def flush(self):
                return None

            def close(self):
                return None

        with patch.object(Path, "open", return_value=FailingFile()):
            recorder = SessionRecorder(
                Path("failed-recording.jsonl"),
                flush_interval_s=0.01,
            )
            deadline = time.monotonic() + 1.0
            while (
                recorder._worker_error is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            with self.assertRaisesRegex(RuntimeError, "writer failed"):
                recorder.write({"type": "test"})
            with self.assertRaisesRegex(RuntimeError, "writer failed"):
                recorder.close()

    def test_recording_writer_flushes_in_order_and_drains_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "batched.jsonl"
            recorder = SessionRecorder(
                path,
                exercise="bench",
                flush_interval_s=0.05,
            )
            for index in range(12):
                recorder.write({"type": "test", "index": index})

            deadline = time.monotonic() + 1.0
            visible_records = []
            while time.monotonic() < deadline:
                text = path.read_text(encoding="utf-8")
                visible_records = [
                    json.loads(line) for line in text.splitlines()
                ]
                if len(visible_records) >= 13:
                    break
                time.sleep(0.01)

            self.assertEqual(
                [record["index"] for record in visible_records[1:]],
                list(range(12)),
            )
            recorder.write({"type": "test", "index": 12})
            recorder.close()
            final_records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [record["index"] for record in final_records[1:]],
                list(range(13)),
            )

    def test_recording_replay_preserves_packets(self):
        tracker = ReversalRepTracker()
        sequence = 0
        samples = []
        for value in [0.0] * 120 + upward(0.3, 0.8):
            sequence += 1
            samples.append(decoded(sequence, value))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            recorder = SessionRecorder(path, exercise="bench")
            recorder.mark_tracker_reset(0.0)
            original_reps = 0
            for sample in samples:
                events, record = tracker.process(sample)
                original_reps += sum(event.kind == "rep" for event in events)
                recorder.write(record)
            recorder.close()

            metadata = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(metadata["algorithm_version"], ALGORITHM_VERSION)
            self.assertAlmostEqual(
                1.0 / metadata["sample_interval_s"],
                SAMPLE_RATE_HZ,
            )
            self.assertEqual(metadata["exercise_profile"], "bench")
            self.assertEqual(recording_metadata(path), metadata)
            self.assertIn(
                "sequence, qx, qy, qw, qz, ax, ay, az",
                metadata["packet_layout"],
            )
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
