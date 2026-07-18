"""Velocity-based signal processing for Beast Sensor repetitions."""

from __future__ import annotations

import json
import math
import statistics
import struct
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi


ALGORITHM_VERSION = "generic-velocity-v3"
GRAVITY_M_S2 = 9.80665
SAMPLE_RATE_HZ = 47.6
SAMPLE_INTERVAL_S = 1.0 / SAMPLE_RATE_HZ

CALIBRATION_SAMPLES = 100
CALIBRATION_MAX_VERTICAL_STD_G = 0.010
CALIBRATION_MIN_VERTICAL_G = 0.85
FILTER_CUTOFF_HZ = 5.0
FILTER_ORDER = 4
HAMPEL_WINDOW_SAMPLES = 5
HAMPEL_SIGMA = 3.0
REST_WINDOW_SAMPLES = round(0.5 * SAMPLE_RATE_HZ)
REST_ORIENTATION_MAX_DEG = 2.0
FALLBACK_MIN_ORIENTATION_DEG = 10.0
FALLBACK_REST_JITTER_MULTIPLIER = 4.0
FALLBACK_BRAKING_CONFIRM_SAMPLES = 3
MIN_START_ACCELERATION_M_S2 = 0.08
START_CONFIRM_SAMPLES = 4
START_MIN_VELOCITY_M_S = 0.02
END_CONFIRM_SAMPLES = 3
END_MIN_VELOCITY_M_S = 0.03
END_PEAK_FRACTION = 0.10
DOWN_MAX_DURATION_S = 6.0
MAX_MISSING_SAMPLES = 0


@dataclass(frozen=True)
class ImuSample:
    """One decoded sample with a body-to-world x,y,z,w quaternion."""

    sequence: int
    host_timestamp: float
    packet_hex: str
    quaternion_xyzw: tuple[float, float, float, float]
    acceleration_g: tuple[float, float, float]
    world_acceleration_g: tuple[float, float, float]

    @property
    def vertical_g(self) -> float:
        return self.world_acceleration_g[2]


@dataclass(frozen=True)
class MotionPoint:
    elapsed_s: float
    velocity_m_s: float
    displacement_m: float


@dataclass(frozen=True)
class MotionEvent:
    kind: str
    reason: str | None = None
    metrics: dict[str, float] | None = None
    quality: dict[str, bool | float | int | str] | None = None
    trace: list[MotionPoint] | None = None


@dataclass(frozen=True)
class ExerciseProfile:
    name: str
    movement_pattern: str
    min_duration_s: float
    max_duration_s: float
    min_displacement_m: float
    min_peak_velocity_m_s: float


EXERCISE_PROFILES: dict[str, ExerciseProfile] = {
    "generic": ExerciseProfile(
        "generic",
        "either",
        0.15,
        5.0,
        0.03,
        0.10,
    ),
    "bench": ExerciseProfile(
        "bench",
        "down_then_up",
        0.15,
        4.0,
        0.08,
        0.10,
    ),
    "squat": ExerciseProfile(
        "squat",
        "down_then_up",
        0.20,
        5.0,
        0.10,
        0.10,
    ),
    "deadlift": ExerciseProfile(
        "deadlift",
        "up_from_rest",
        0.20,
        5.0,
        0.15,
        0.10,
    ),
}


@dataclass(frozen=True)
class TrackerConfig:
    profile: ExerciseProfile = EXERCISE_PROFILES["generic"]
    rest_window_samples: int = REST_WINDOW_SAMPLES
    rest_orientation_max_deg: float = REST_ORIENTATION_MAX_DEG
    hampel_window_samples: int = HAMPEL_WINDOW_SAMPLES
    filter_cutoff_hz: float = FILTER_CUTOFF_HZ


def tracker_config_for(exercise: str) -> TrackerConfig:
    try:
        return TrackerConfig(profile=EXERCISE_PROFILES[exercise])
    except KeyError as exc:
        choices = ", ".join(EXERCISE_PROFILES)
        raise ValueError(
            f"Unknown exercise profile '{exercise}'. Choose from: {choices}."
        ) from exc


def rotate_body_to_world(
    quaternion_xyzw: tuple[float, float, float, float],
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Rotate a body-frame vector using a body-to-world x,y,z,w quaternion."""
    x, y, z, w = quaternion_xyzw
    vx, vy, vz = vector
    return (
        (1.0 - 2.0 * (y * y + z * z)) * vx
        + 2.0 * (x * y - w * z) * vy
        + 2.0 * (x * z + w * y) * vz,
        2.0 * (x * y + w * z) * vx
        + (1.0 - 2.0 * (x * x + z * z)) * vy
        + 2.0 * (y * z - w * x) * vz,
        2.0 * (x * z - w * y) * vx
        + 2.0 * (y * z + w * x) * vy
        + (1.0 - 2.0 * (x * x + y * y)) * vz,
    )


def decode_imu_packet(
    data: bytes | bytearray,
    host_timestamp: float,
) -> ImuSample | None:
    """Decode a Beast packet into world-oriented acceleration.

    Beast sends its world-to-body quaternion as x,y,w,z. Conjugating it yields
    the body-to-world x,y,z,w quaternion used everywhere else in the tracker.
    """
    if len(data) < 16:
        return None

    (
        sequence,
        device_qx,
        device_qy,
        device_qw,
        device_qz,
        ax,
        ay,
        az,
    ) = struct.unpack("<Hhhhhhhh", bytes(data[:16]))
    quaternion_norm = math.sqrt(
        device_qx * device_qx
        + device_qy * device_qy
        + device_qz * device_qz
        + device_qw * device_qw
    )
    if quaternion_norm == 0.0:
        return None

    quaternion = (
        -device_qx / quaternion_norm,
        -device_qy / quaternion_norm,
        -device_qz / quaternion_norm,
        device_qw / quaternion_norm,
    )
    acceleration = (ax / 1000.0, ay / 1000.0, az / 1000.0)
    return ImuSample(
        sequence=sequence,
        host_timestamp=host_timestamp,
        packet_hex=bytes(data).hex(),
        quaternion_xyzw=quaternion,
        acceleration_g=acceleration,
        world_acceleration_g=rotate_body_to_world(quaternion, acceleration),
    )


class ReversalRepTracker:
    """Detect concentric repetitions from a drift-corrected velocity curve."""

    CALIBRATING = "calibrating"
    REST = "rest"
    UP = "up"
    DOWN = "down"
    RECOVERY = "recovery"

    def __init__(self, config: TrackerConfig | None = None) -> None:
        self.config = config or TrackerConfig()
        self.profile = self.config.profile
        self.state = self.CALIBRATING

        self.calibration_values: deque[float] = deque(maxlen=CALIBRATION_SAMPLES)
        self.calibration_horizontal: deque[float] = deque(
            maxlen=CALIBRATION_SAMPLES
        )
        self.gravity_baseline_g: float | None = None
        self.gravity_sign = 1.0
        self.noise_m_s2 = 0.0
        self.start_threshold_m_s2 = MIN_START_ACCELERATION_M_S2
        self.stationary_threshold_m_s2 = 0.05

        self.filter_sos = butter(
            FILTER_ORDER,
            self.config.filter_cutoff_hz,
            btype="lowpass",
            fs=SAMPLE_RATE_HZ,
            output="sos",
        )
        self.filter_initial_state = sosfilt_zi(self.filter_sos)
        self.filter_state = self.filter_initial_state * 0.0
        self.hampel_values: deque[float] = deque(
            maxlen=self.config.hampel_window_samples
        )

        self.rest_vertical_g: deque[float] = deque(
            maxlen=self.config.rest_window_samples
        )
        self.rest_world_acceleration_g: deque[
            tuple[float, float, float]
        ] = deque(maxlen=self.config.rest_window_samples)
        self.rest_quaternions: deque[tuple[float, float, float, float]] = deque(
            maxlen=self.config.rest_window_samples
        )
        self.rest_confirmed = False
        self.rest_confidence = 0.0
        self.rest_acceleration_variation_m_s2 = 0.0
        self.orientation_change_deg = 0.0
        self.rest_orientation_jitter_deg = 0.0

        self.last_sequence: int | None = None
        self.sensor_time_s = 0.0
        self.raw_acceleration_m_s2 = 0.0
        self.filtered_acceleration_m_s2 = 0.0
        self.previous_acceleration_m_s2 = 0.0
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0

        self.phase_started_s = 0.0
        self.phase_points: list[MotionPoint] = []
        self.phase_peak_velocity_m_s = 0.0
        self.phase_missed_samples = 0
        self.deceleration_seen = False
        self.phase_propulsion_samples = 0
        self.phase_braking_samples = 0
        self.phase_reference_quaternion: (
            tuple[float, float, float, float] | None
        ) = None
        self.phase_orientation_excursion_deg = 0.0
        self.phase_started_from_confirmed_rest = False
        self.fallback_rejection_evidence = ""
        self.end_candidate_samples = 0
        self.downward_motion_seen = False
        self.down_peak_velocity_m_s = 0.0
        self.bottom_candidate_samples = 0

        self.positive_start_samples: list[float] = []
        self.negative_start_samples: list[float] = []
        self.start_buffer_from_bottom = False
        self.phase_started_from_bottom = False
        self.upward_armed = True
        self.total_missing_samples = 0
        self.duplicate_packets = 0
        self.motion_bout_id = 0
        self.active_motion_bout_id: int | None = None

    def process(self, sample: ImuSample) -> tuple[list[MotionEvent], dict]:
        events: list[MotionEvent] = []
        state_before = self.state
        sequence_delta = self._sequence_delta(sample.sequence)

        if sequence_delta == 0:
            self.duplicate_packets += 1
            events.append(MotionEvent("duplicate", "duplicate sensor packet"))
            return events, self._record(sample, state_before, sequence_delta, 0.0)

        missing_samples = max(0, sequence_delta - 1)
        if missing_samples:
            self.total_missing_samples += missing_samples
            self.phase_missed_samples += missing_samples
            events.append(
                MotionEvent(
                    "gap",
                    f"{missing_samples} sensor sample(s) missing",
                )
            )
            if missing_samples > MAX_MISSING_SAMPLES and self.state in {
                self.UP,
                self.DOWN,
            }:
                events.append(
                    MotionEvent(
                        "rejected",
                        "motion interrupted by missing samples; waiting for rest",
                        quality=self._phase_quality("rejected"),
                    )
                )
                self._enter_recovery()

        sample_dt = sequence_delta * SAMPLE_INTERVAL_S
        self.sensor_time_s += sample_dt

        if self.state == self.CALIBRATING:
            ready = self._calibrate(sample)
            if ready is not None:
                events.append(ready)
            return events, self._record(
                sample,
                state_before,
                sequence_delta,
                sample_dt,
            )

        assert self.gravity_baseline_g is not None
        self.raw_acceleration_m_s2 = (
            self.gravity_sign * (sample.vertical_g - self.gravity_baseline_g)
            * GRAVITY_M_S2
        )
        hampel_value = self._hampel_filter(self.raw_acceleration_m_s2)
        self.filtered_acceleration_m_s2 = self._butterworth_filter(hampel_value)
        self._update_rest_status(sample)

        if self.state == self.REST:
            self._handle_rest(sample, events)
        elif self.state == self.UP:
            self._handle_up(sample, sample_dt, events)
        elif self.state == self.DOWN:
            self._handle_down(sample_dt, events)
        else:
            self._handle_recovery(events)

        self.previous_acceleration_m_s2 = self.filtered_acceleration_m_s2
        return events, self._record(
            sample,
            state_before,
            sequence_delta,
            sample_dt,
            self.raw_acceleration_m_s2,
        )

    def _sequence_delta(self, sequence: int) -> int:
        if self.last_sequence is None:
            self.last_sequence = sequence
            return 1
        delta = (sequence - self.last_sequence) & 0xFFFF
        self.last_sequence = sequence
        return delta

    def _calibrate(self, sample: ImuSample) -> MotionEvent | None:
        horizontal = math.hypot(
            sample.world_acceleration_g[0],
            sample.world_acceleration_g[1],
        )
        self.calibration_values.append(sample.vertical_g)
        self.calibration_horizontal.append(horizontal)
        if len(self.calibration_values) < CALIBRATION_SAMPLES:
            return None

        values = list(self.calibration_values)
        baseline = statistics.median(values)
        vertical_std = statistics.pstdev(values)
        horizontal_median = statistics.median(self.calibration_horizontal)
        stable = (
            vertical_std <= CALIBRATION_MAX_VERTICAL_STD_G
            and CALIBRATION_MIN_VERTICAL_G <= abs(baseline) <= 1.20
            and horizontal_median <= 0.30
        )
        if not stable:
            return None

        deviations = [abs(value - baseline) for value in values]
        robust_std_g = max(statistics.median(deviations) * 1.4826, 0.0005)
        self.gravity_baseline_g = baseline
        self.gravity_sign = 1.0 if baseline >= 0.0 else -1.0
        self.noise_m_s2 = robust_std_g * GRAVITY_M_S2
        self.start_threshold_m_s2 = max(
            MIN_START_ACCELERATION_M_S2,
            6.0 * self.noise_m_s2,
        )
        self.stationary_threshold_m_s2 = max(
            0.05,
            4.0 * self.noise_m_s2,
        )
        self.state = self.REST
        self._reset_filter()
        self._clear_motion()
        return MotionEvent(
            "ready",
            quality={
                "gravity_baseline_g": baseline,
                "vertical_noise_m_s2": self.noise_m_s2,
                "start_threshold_m_s2": self.start_threshold_m_s2,
                "stationary_threshold_m_s2": self.stationary_threshold_m_s2,
                "sample_rate_hz": SAMPLE_RATE_HZ,
                "exercise_profile": self.profile.name,
            },
        )

    def _hampel_filter(self, value: float) -> float:
        self.hampel_values.append(value)
        if len(self.hampel_values) < self.config.hampel_window_samples:
            return value
        values = list(self.hampel_values)
        median = statistics.median(values)
        mad = statistics.median(abs(item - median) for item in values)
        robust_sigma = max(1.4826 * mad, self.noise_m_s2, 0.005)
        if abs(value - median) > HAMPEL_SIGMA * robust_sigma:
            return median
        return value

    def _butterworth_filter(self, value: float) -> float:
        filtered, self.filter_state = sosfilt(
            self.filter_sos,
            np.asarray([value], dtype=float),
            zi=self.filter_state,
        )
        return float(filtered[0])

    def _reset_filter(self) -> None:
        self.filter_state = self.filter_initial_state * 0.0
        self.hampel_values.clear()
        self.filtered_acceleration_m_s2 = 0.0
        self.previous_acceleration_m_s2 = 0.0

    def _update_rest_status(self, sample: ImuSample) -> None:
        self.rest_vertical_g.append(sample.vertical_g)
        self.rest_world_acceleration_g.append(sample.world_acceleration_g)
        self.rest_quaternions.append(sample.quaternion_xyzw)
        if len(self.rest_vertical_g) < self.config.rest_window_samples:
            self.rest_confirmed = False
            self.rest_confidence = 0.0
            self.rest_acceleration_variation_m_s2 = 0.0
            self.orientation_change_deg = 0.0
            return

        axis_variances_g2 = [
            statistics.pvariance(
                acceleration[axis]
                for acceleration in self.rest_world_acceleration_g
            )
            for axis in range(3)
        ]
        self.rest_acceleration_variation_m_s2 = (
            math.sqrt(sum(axis_variances_g2)) * GRAVITY_M_S2
        )
        first_quaternion = self.rest_quaternions[0]
        self.orientation_change_deg = max(
            self._quaternion_angle_deg(first_quaternion, quaternion)
            for quaternion in self.rest_quaternions
        )
        acceleration_ratio = (
            self.rest_acceleration_variation_m_s2
            / self.stationary_threshold_m_s2
        )
        orientation_ratio = (
            self.orientation_change_deg
            / self.config.rest_orientation_max_deg
        )
        self.rest_confidence = max(
            0.0,
            min(1.0, 1.0 - max(acceleration_ratio, orientation_ratio)),
        )
        self.rest_confirmed = (
            self.rest_acceleration_variation_m_s2
            <= self.stationary_threshold_m_s2
            and self.orientation_change_deg
            <= self.config.rest_orientation_max_deg
        )
        if self.rest_confirmed:
            self.rest_orientation_jitter_deg = self.orientation_change_deg

    @staticmethod
    def _quaternion_angle_deg(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
    ) -> float:
        dot = abs(sum(left * right for left, right in zip(first, second)))
        dot = min(1.0, max(-1.0, dot))
        return math.degrees(2.0 * math.acos(dot))

    def _handle_rest(
        self,
        sample: ImuSample,
        events: list[MotionEvent],
    ) -> None:
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        if self.rest_confirmed:
            self._adopt_rest_baseline()
            self.active_motion_bout_id = None
            if self.profile.movement_pattern == "up_from_rest":
                self.upward_armed = True
            self.positive_start_samples = []
            self.negative_start_samples = []
            self.start_buffer_from_bottom = False
            return

        acceleration = self.filtered_acceleration_m_s2
        if acceleration >= self.start_threshold_m_s2:
            self.positive_start_samples.append(acceleration)
            self.negative_start_samples = []
            buffered_velocity = (
                sum(self.positive_start_samples) * SAMPLE_INTERVAL_S
            )
            if (
                len(self.positive_start_samples) >= START_CONFIRM_SAMPLES
                and buffered_velocity >= START_MIN_VELOCITY_M_S
                and self.upward_armed
            ):
                self._start_up_from_rest(sample.quaternion_xyzw)
                events.append(
                    MotionEvent(
                        "up_started",
                        "sustained upward velocity developed",
                    )
                )
        elif acceleration <= -self.start_threshold_m_s2:
            self.negative_start_samples.append(acceleration)
            self.positive_start_samples = []
            self.start_buffer_from_bottom = False
            buffered_velocity = (
                sum(self.negative_start_samples) * SAMPLE_INTERVAL_S
            )
            if (
                len(self.negative_start_samples) >= START_CONFIRM_SAMPLES
                and buffered_velocity <= -START_MIN_VELOCITY_M_S
            ):
                self._start_down_from_rest()
                events.append(
                    MotionEvent(
                        "down_started",
                        "sustained downward velocity developed",
                    )
                )
        else:
            self.positive_start_samples = []
            self.negative_start_samples = []
            self.start_buffer_from_bottom = False

    def _start_up_from_rest(
        self,
        quaternion_xyzw: tuple[float, float, float, float] = (
            0.0,
            0.0,
            0.0,
            1.0,
        ),
    ) -> None:
        buffered = list(self.positive_start_samples)
        self.phase_started_from_confirmed_rest = (
            self.active_motion_bout_id is None
        )
        self._begin_motion_bout()
        self.phase_started_from_bottom = self.start_buffer_from_bottom
        self.state = self.UP
        self.phase_started_s = self.sensor_time_s - (
            len(buffered) - 1
        ) * SAMPLE_INTERVAL_S
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        self.phase_points = [MotionPoint(0.0, 0.0, 0.0)]
        previous_acceleration = buffered[0]
        for index, acceleration in enumerate(buffered[1:], 1):
            previous_velocity = self.velocity_m_s
            self.velocity_m_s += 0.5 * (
                previous_acceleration + acceleration
            ) * SAMPLE_INTERVAL_S
            self.displacement_m += 0.5 * (
                previous_velocity + self.velocity_m_s
            ) * SAMPLE_INTERVAL_S
            self.phase_points.append(
                MotionPoint(
                    index * SAMPLE_INTERVAL_S,
                    self.velocity_m_s,
                    self.displacement_m,
                )
            )
            previous_acceleration = acceleration
        self.previous_acceleration_m_s2 = buffered[-1]
        self.phase_peak_velocity_m_s = max(
            point.velocity_m_s for point in self.phase_points
        )
        self.phase_missed_samples = 0
        self.deceleration_seen = False
        self.phase_propulsion_samples = len(buffered)
        self.phase_braking_samples = 0
        self.phase_reference_quaternion = quaternion_xyzw
        self.phase_orientation_excursion_deg = 0.0
        self.end_candidate_samples = 0
        self.positive_start_samples = []
        self.negative_start_samples = []
        self.start_buffer_from_bottom = False

    def _start_down_from_rest(self) -> None:
        buffered = list(self.negative_start_samples)
        self._begin_motion_bout()
        self.state = self.DOWN
        self.phase_started_s = self.sensor_time_s - (
            len(buffered) - 1
        ) * SAMPLE_INTERVAL_S
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        previous_acceleration = buffered[0]
        for acceleration in buffered[1:]:
            self.velocity_m_s += 0.5 * (
                previous_acceleration + acceleration
            ) * SAMPLE_INTERVAL_S
            previous_acceleration = acceleration
        self.previous_acceleration_m_s2 = buffered[-1]
        self.phase_missed_samples = 0
        self.down_peak_velocity_m_s = self.velocity_m_s
        self.downward_motion_seen = (
            self.velocity_m_s <= -self.profile.min_peak_velocity_m_s
        )
        self.bottom_candidate_samples = 0
        self.positive_start_samples = []
        self.negative_start_samples = []
        self.start_buffer_from_bottom = False

    def _handle_up(
        self,
        sample: ImuSample,
        dt: float,
        events: list[MotionEvent],
    ) -> None:
        previous_velocity = self.velocity_m_s
        self.velocity_m_s += 0.5 * (
            self.previous_acceleration_m_s2
            + self.filtered_acceleration_m_s2
        ) * dt
        self.displacement_m += 0.5 * (
            previous_velocity + self.velocity_m_s
        ) * dt
        elapsed = self.sensor_time_s - self.phase_started_s
        self.phase_points.append(
            MotionPoint(elapsed, self.velocity_m_s, self.displacement_m)
        )
        self.phase_peak_velocity_m_s = max(
            self.phase_peak_velocity_m_s,
            self.velocity_m_s,
        )
        if self.phase_reference_quaternion is not None:
            self.phase_orientation_excursion_deg = max(
                self.phase_orientation_excursion_deg,
                self.orientation_change_deg,
                self._quaternion_angle_deg(
                    self.phase_reference_quaternion,
                    sample.quaternion_xyzw,
                ),
            )
        if self.filtered_acceleration_m_s2 >= self.start_threshold_m_s2:
            self.phase_propulsion_samples += 1
        if self.filtered_acceleration_m_s2 <= -self.start_threshold_m_s2:
            self.deceleration_seen = True
            self.phase_braking_samples += 1

        established = (
            self.phase_peak_velocity_m_s
            >= START_MIN_VELOCITY_M_S
            and elapsed >= self.profile.min_duration_s
        )
        end_velocity = max(
            END_MIN_VELOCITY_M_S,
            END_PEAK_FRACTION * self.phase_peak_velocity_m_s,
        )
        close_to_top = (
            established
            and self.deceleration_seen
            and self.velocity_m_s <= end_velocity
        )
        self.end_candidate_samples = (
            self.end_candidate_samples + 1 if close_to_top else 0
        )
        crossed_zero = (
            established
            and self.deceleration_seen
            and previous_velocity > 0.0
            and self.velocity_m_s <= 0.0
        )
        stable_top = (
            established
            and self.deceleration_seen
            and self.rest_confirmed
            and self.velocity_m_s <= max(end_velocity, 0.15)
        )
        if (
            crossed_zero
            or stable_top
            or self.end_candidate_samples >= END_CONFIRM_SAMPLES
        ):
            self._finish_up(events)
            return

        if self.rest_confirmed:
            assert self.gravity_baseline_g is not None
            baseline_shift_m_s2 = (
                abs(
                    statistics.median(self.rest_vertical_g)
                    - self.gravity_baseline_g
                )
                * GRAVITY_M_S2
            )
            if (
                not self.deceleration_seen
                and elapsed >= 0.50
                and baseline_shift_m_s2 >= self.start_threshold_m_s2
            ):
                self._adopt_rest_baseline()
                self.state = self.REST
                self._clear_motion()
                events.append(
                    MotionEvent(
                        "rest",
                        "stable gravity baseline reacquired",
                    )
                )
                return
            if elapsed >= 0.75:
                if self._finish_rest_orientation_fallback(events):
                    return
            if self.phase_started_from_bottom and elapsed >= 0.75:
                events.append(
                    MotionEvent(
                        "rejected",
                        (
                            "bottom braking impulse settled without an "
                            "upward top; fallback not accepted: "
                            f"{self.fallback_rejection_evidence or 'requirements not met'}; "
                            "detector re-armed"
                        ),
                        quality=self._phase_quality(
                            "rejected",
                            evidence=self.fallback_rejection_evidence,
                        ),
                    )
                )
                self._adopt_rest_baseline()
                self._enter_rest(upward_armed=True)
                events.append(
                    MotionEvent(
                        "rest",
                        "confirmed rest after bottom braking",
                    )
                )
                return

        if elapsed >= self.profile.max_duration_s:
            fallback_detail = (
                "; fallback not accepted: "
                f"{self.fallback_rejection_evidence}"
                if self.fallback_rejection_evidence
                else ""
            )
            events.append(
                MotionEvent(
                    "rejected",
                    (
                        "upward phase had no valid top"
                        f"{fallback_detail}; waiting for rest"
                    ),
                    quality=self._phase_quality(
                        "rejected",
                        evidence=self.fallback_rejection_evidence,
                    ),
                )
            )
            self._enter_recovery()

    def _finish_up(self, events: list[MotionEvent]) -> None:
        metrics, corrected_trace, drift = self._corrected_phase_metrics()
        failures = self._metric_failures(metrics)
        quality = self._phase_quality(
            "accepted" if not failures else "rejected",
            drift,
            top_detection="velocity",
            evidence="velocity peak + braking + zero return",
        )
        if failures:
            events.append(
                MotionEvent(
                    "rejected",
                    "; ".join(failures),
                    metrics=metrics,
                    quality=quality,
                    trace=corrected_trace,
                )
            )
        else:
            events.append(
                MotionEvent(
                    "rep",
                    metrics=metrics,
                    quality=quality,
                    trace=corrected_trace,
                )
            )
        events.append(
            MotionEvent(
                "top",
                "upward velocity returned to zero",
                metrics=metrics,
                quality=quality,
            )
        )
        self._enter_down_after_top()

    def _finish_rest_orientation_fallback(
        self,
        events: list[MotionEvent],
    ) -> bool:
        """Recover a physically complete rep whose integrated velocity drifted."""
        orientation_threshold_deg = max(
            FALLBACK_MIN_ORIENTATION_DEG,
            (
                FALLBACK_REST_JITTER_MULTIPLIER
                * self.rest_orientation_jitter_deg
            ),
        )
        valid_start = (
            self.phase_started_from_confirmed_rest
            or self.phase_started_from_bottom
        )
        valid_shape = (
            self.phase_propulsion_samples >= START_CONFIRM_SAMPLES
            and self.deceleration_seen
            and (
                self.phase_braking_samples
                >= FALLBACK_BRAKING_CONFIRM_SAMPLES
            )
        )
        failures: list[str] = []
        if not valid_start:
            failures.append("movement did not start from rest or bottom")
        if not valid_shape:
            failures.append("propulsion-and-braking shape was incomplete")
        if self.phase_missed_samples:
            failures.append(
                f"{self.phase_missed_samples} sensor sample(s) missing"
            )
        if (
            self.phase_orientation_excursion_deg
            < orientation_threshold_deg
        ):
            failures.append(
                "orientation change "
                f"{self.phase_orientation_excursion_deg:.1f}° below "
                f"{orientation_threshold_deg:.1f}°"
            )
        if failures:
            self.fallback_rejection_evidence = "; ".join(failures)
            return False

        metrics, corrected_trace, drift = self._corrected_phase_metrics()
        metric_failures = self._metric_failures(metrics)
        if metric_failures:
            self.fallback_rejection_evidence = "; ".join(metric_failures)
            return False

        rest_duration_s = (
            self.config.rest_window_samples * SAMPLE_INTERVAL_S
        )
        evidence = (
            "velocity peak + braking + "
            f"{self.phase_orientation_excursion_deg:.1f}° orientation + "
            f"{rest_duration_s:.2f} s rest"
        )
        quality = self._phase_quality(
            "recovered_top",
            drift,
            top_detection="rest_orientation_fallback",
            evidence=evidence,
        )
        reason = "top recovered from confirmed rest and orientation change"
        events.append(
            MotionEvent(
                "rep",
                reason,
                metrics=metrics,
                quality=quality,
                trace=corrected_trace,
            )
        )
        events.append(
            MotionEvent(
                "top",
                reason,
                metrics=metrics,
                quality=quality,
            )
        )
        self._adopt_rest_baseline()
        self.active_motion_bout_id = None
        self._enter_rest(
            upward_armed=(
                self.profile.movement_pattern != "down_then_up"
            )
        )
        events.append(
            MotionEvent(
                "rest",
                "confirmed rest after recovered top",
            )
        )
        return True

    def _corrected_phase_metrics(
        self,
    ) -> tuple[dict[str, float], list[MotionPoint], float]:
        duration = self.phase_points[-1].elapsed_s
        final_raw_velocity = self.phase_points[-1].velocity_m_s
        corrected_points: list[MotionPoint] = []
        previous_time = 0.0
        previous_velocity = 0.0
        displacement = 0.0
        peak_velocity = 0.0
        for point in self.phase_points:
            progress = point.elapsed_s / duration if duration > 0.0 else 0.0
            corrected_velocity = max(
                0.0,
                point.velocity_m_s - final_raw_velocity * progress,
            )
            dt = point.elapsed_s - previous_time
            displacement += 0.5 * (
                previous_velocity + corrected_velocity
            ) * dt
            corrected_points.append(
                MotionPoint(
                    point.elapsed_s,
                    corrected_velocity,
                    displacement,
                )
            )
            previous_time = point.elapsed_s
            previous_velocity = corrected_velocity
            peak_velocity = max(peak_velocity, corrected_velocity)
        metrics = {
            "duration_s": duration,
            "displacement_m": displacement,
            "average_speed_m_s": (
                displacement / duration if duration > 0.0 else 0.0
            ),
            "peak_speed_m_s": peak_velocity,
        }
        return metrics, corrected_points, final_raw_velocity

    def _metric_failures(self, metrics: dict[str, float]) -> list[str]:
        failures: list[str] = []
        if metrics["duration_s"] < self.profile.min_duration_s:
            failures.append(
                f"duration below {self.profile.min_duration_s:.2f} s"
            )
        if metrics["duration_s"] > self.profile.max_duration_s:
            failures.append(
                f"duration above {self.profile.max_duration_s:.2f} s"
            )
        if metrics["displacement_m"] < self.profile.min_displacement_m:
            failures.append(
                f"displacement below {self.profile.min_displacement_m:.2f} m"
            )
        if metrics["peak_speed_m_s"] < self.profile.min_peak_velocity_m_s:
            failures.append(
                "peak velocity below "
                f"{self.profile.min_peak_velocity_m_s:.2f} m/s"
            )
        return failures

    def _phase_quality(
        self,
        status: str,
        drift_correction_m_s: float = 0.0,
        *,
        top_detection: str = "not_detected",
        evidence: str = "",
    ) -> dict[str, bool | float | int | str]:
        return {
            "quality_status": status,
            "top_detection": top_detection,
            "evidence": evidence,
            "missing_samples": self.phase_missed_samples,
            "vertical_noise_m_s2": self.noise_m_s2,
            "drift_correction_m_s": drift_correction_m_s,
            "raw_final_velocity_m_s": drift_correction_m_s,
            "orientation_excursion_deg": (
                self.phase_orientation_excursion_deg
            ),
            "orientation_threshold_deg": max(
                FALLBACK_MIN_ORIENTATION_DEG,
                (
                    FALLBACK_REST_JITTER_MULTIPLIER
                    * self.rest_orientation_jitter_deg
                ),
            ),
            "confirmed_rest_duration_s": (
                self.config.rest_window_samples * SAMPLE_INTERVAL_S
                if self.rest_confirmed
                else 0.0
            ),
            "phase_started_s": self.phase_started_s,
            "phase_ended_s": self.sensor_time_s,
            "phase_started_from_bottom": self.phase_started_from_bottom,
            "phase_started_from_confirmed_rest": (
                self.phase_started_from_confirmed_rest
            ),
            "exercise_profile": self.profile.name,
            "motion_bout_id": self.active_motion_bout_id or 0,
        }

    def _begin_motion_bout(self) -> None:
        if self.active_motion_bout_id is not None:
            return
        self.motion_bout_id += 1
        self.active_motion_bout_id = self.motion_bout_id

    def _enter_down_after_top(self) -> None:
        self.state = self.DOWN
        self.upward_armed = False
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        self.phase_started_s = self.sensor_time_s
        self.phase_points = []
        self.phase_peak_velocity_m_s = 0.0
        self.phase_missed_samples = 0
        self.downward_motion_seen = False
        self.down_peak_velocity_m_s = 0.0
        self.bottom_candidate_samples = 0
        self.deceleration_seen = False
        self.end_candidate_samples = 0

    def _handle_down(self, dt: float, events: list[MotionEvent]) -> None:
        previous_velocity = self.velocity_m_s
        self.velocity_m_s += 0.5 * (
            self.previous_acceleration_m_s2
            + self.filtered_acceleration_m_s2
        ) * dt
        elapsed = self.sensor_time_s - self.phase_started_s
        self.down_peak_velocity_m_s = min(
            self.down_peak_velocity_m_s,
            self.velocity_m_s,
        )
        if (
            self.velocity_m_s
            <= -self.profile.min_peak_velocity_m_s
        ):
            self.downward_motion_seen = True

        bottom_velocity = max(
            END_MIN_VELOCITY_M_S,
            END_PEAK_FRACTION * abs(self.down_peak_velocity_m_s),
        )
        close_to_bottom = (
            self.downward_motion_seen
            and self.filtered_acceleration_m_s2
            >= self.start_threshold_m_s2
            and self.velocity_m_s >= -bottom_velocity
        )
        self.bottom_candidate_samples = (
            self.bottom_candidate_samples + 1 if close_to_bottom else 0
        )
        crossed_bottom = (
            self.downward_motion_seen
            and previous_velocity < 0.0
            and self.velocity_m_s >= 0.0
        )
        if (
            crossed_bottom
            or self.bottom_candidate_samples >= END_CONFIRM_SAMPLES
        ):
            positive_acceleration = self.filtered_acceleration_m_s2
            self._enter_rest(
                upward_armed=(
                    self.profile.movement_pattern != "up_from_rest"
                )
            )
            if positive_acceleration >= self.start_threshold_m_s2:
                self.positive_start_samples = [positive_acceleration]
                self.start_buffer_from_bottom = True
            events.append(
                MotionEvent(
                    "bottom",
                    "downward velocity returned to zero",
                )
            )
            return

        if self.rest_confirmed and elapsed >= 0.50:
            self._adopt_rest_baseline()
            self._enter_rest(upward_armed=True)
            events.append(
                MotionEvent(
                    "bottom",
                    "confirmed rest after downward movement",
                )
            )
            return

        if elapsed >= DOWN_MAX_DURATION_S:
            events.append(
                MotionEvent(
                    "rejected",
                    "downward phase had no valid bottom; waiting for rest",
                    quality=self._phase_quality("rejected"),
                )
            )
            self._enter_recovery()

    def _handle_recovery(self, events: list[MotionEvent]) -> None:
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        if not self.rest_confirmed:
            return
        self._adopt_rest_baseline()
        self.active_motion_bout_id = None
        self._enter_rest(upward_armed=True)
        events.append(
            MotionEvent(
                "rest",
                "confirmed rest; detector re-armed",
            )
        )

    def _adopt_rest_baseline(self) -> None:
        if not self.rest_vertical_g:
            return
        self.gravity_baseline_g = statistics.median(self.rest_vertical_g)
        self.gravity_sign = (
            1.0 if self.gravity_baseline_g >= 0.0 else -1.0
        )
        self._reset_filter()

    def _enter_rest(self, upward_armed: bool) -> None:
        self.state = self.REST
        self.upward_armed = upward_armed
        self._clear_motion()

    def _enter_recovery(self) -> None:
        self.state = self.RECOVERY
        self.upward_armed = False
        self._clear_motion()

    def _clear_motion(self) -> None:
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        self.phase_points = []
        self.phase_peak_velocity_m_s = 0.0
        self.phase_missed_samples = 0
        self.deceleration_seen = False
        self.phase_propulsion_samples = 0
        self.phase_braking_samples = 0
        self.phase_reference_quaternion = None
        self.phase_orientation_excursion_deg = 0.0
        self.phase_started_from_confirmed_rest = False
        self.fallback_rejection_evidence = ""
        self.end_candidate_samples = 0
        self.downward_motion_seen = False
        self.down_peak_velocity_m_s = 0.0
        self.bottom_candidate_samples = 0
        self.positive_start_samples = []
        self.negative_start_samples = []
        self.start_buffer_from_bottom = False
        self.phase_started_from_bottom = False

    def _record(
        self,
        sample: ImuSample,
        state_before: str,
        sequence_delta: int,
        sample_dt: float,
        raw_acceleration_m_s2: float = 0.0,
    ) -> dict:
        return {
            "type": "sample",
            "host_timestamp": sample.host_timestamp,
            "sensor_time_s": self.sensor_time_s,
            "sequence": sample.sequence,
            "sequence_delta": sequence_delta,
            "sample_dt_s": sample_dt,
            "packet_hex": sample.packet_hex,
            "quaternion_xyzw": list(sample.quaternion_xyzw),
            "acceleration_g": list(sample.acceleration_g),
            "world_acceleration_g": list(sample.world_acceleration_g),
            "vertical_g": sample.vertical_g,
            "gravity_baseline_g": self.gravity_baseline_g,
            "raw_vertical_acceleration_m_s2": raw_acceleration_m_s2,
            "filtered_acceleration_m_s2": self.filtered_acceleration_m_s2,
            "start_threshold_m_s2": self.start_threshold_m_s2,
            "stationary_threshold_m_s2": self.stationary_threshold_m_s2,
            "rest_confidence": self.rest_confidence,
            "rest_confirmed": self.rest_confirmed,
            "rest_acceleration_variation_m_s2": (
                self.rest_acceleration_variation_m_s2
            ),
            "orientation_change_deg": self.orientation_change_deg,
            "exercise_profile": self.profile.name,
            "state_before": state_before,
            "state_after": self.state,
            "velocity_m_s": self.velocity_m_s,
            "displacement_m": self.displacement_m,
        }


class SessionRecorder:
    def __init__(self, path: Path, exercise: str = "generic") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.file = path.open("w", encoding="utf-8")
        self.pending_records = 0
        self.write(
            {
                "type": "metadata",
                "created_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "algorithm_version": ALGORITHM_VERSION,
                "exercise_profile": exercise,
                "sample_interval_s": SAMPLE_INTERVAL_S,
                "packet_layout": (
                    "<Hhhhhhhh: sequence, qx, qy, qw, qz, ax, ay, az"
                ),
            }
        )

    def write(self, record: dict) -> None:
        self.file.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.pending_records += 1
        if self.pending_records >= 50:
            self.file.flush()
            self.pending_records = 0

    def mark_tracker_reset(self, host_timestamp: float) -> None:
        self.write({"type": "tracker_reset", "host_timestamp": host_timestamp})

    def close(self) -> None:
        self.file.flush()
        self.file.close()


def recording_metadata(path: Path) -> dict:
    with path.open(encoding="utf-8") as recording:
        for line in recording:
            if not line.strip():
                continue
            record = json.loads(line)
            return record if record.get("type") == "metadata" else {}
    return {}


def replay_items(path: Path) -> Iterable[ImuSample | None]:
    with path.open("r", encoding="utf-8") as recording:
        for line_number, line in enumerate(recording, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("type") == "tracker_reset":
                yield None
                continue
            if record.get("type") != "sample":
                continue
            try:
                packet = bytes.fromhex(record["packet_hex"])
                host_timestamp = float(
                    record.get("host_timestamp", record.get("timestamp", 0.0))
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid recording sample on line {line_number}: {exc}"
                ) from exc
            sample = decode_imu_packet(packet, host_timestamp)
            if sample is not None:
                yield sample
