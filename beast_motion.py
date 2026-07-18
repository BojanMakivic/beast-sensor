"""Velocity-based signal processing for Beast Sensor repetitions."""

from __future__ import annotations

import json
import math
import queue
import statistics
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi


ALGORITHM_VERSION = "generic-velocity-v4"
GRAVITY_M_S2 = 9.80665
SAMPLE_RATE_HZ = 47.6
SAMPLE_INTERVAL_S = 1.0 / SAMPLE_RATE_HZ
MIN_SAMPLE_RATE_HZ = 43.0
MAX_SAMPLE_RATE_HZ = 52.0
CLOCK_UPDATE_INTERVAL_S = 1.0
CLOCK_MIN_WINDOW_S = 2.0
CLOCK_MAX_WINDOW_S = 8.0
CLOCK_EWMA_ALPHA = 0.25
CLOCK_MAX_UPDATE_HZ = 0.5

CALIBRATION_SAMPLES = 100
CALIBRATION_MAX_VERTICAL_STD_G = 0.010
CALIBRATION_MIN_VERTICAL_G = 0.85
FILTER_CUTOFF_HZ = 5.0
FILTER_ORDER = 4
HAMPEL_WINDOW_SAMPLES = 5
HAMPEL_SIGMA = 3.0
REST_WINDOW_SAMPLES = round(0.3 * SAMPLE_RATE_HZ)
REST_ORIENTATION_MAX_DEG = 4.0
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
ORIENTATION_BASELINE_WINDOW_S = 0.30
ORIENTATION_SETTLE_DURATION_S = 0.12
ORIENTATION_SETTLE_RANGE_DEG = 4.0
ORIENTATION_START_CONFIRM_SAMPLES = 3
ORIENTATION_MIN_RISE_DEG = 5.0
ORIENTATION_MAD_MULTIPLIER = 4.0
ORIENTATION_MIN_PROMINENCE_DEG = 10.0
ORIENTATION_MIN_EXCESS_AREA_DEG_S = 8.0
PROVISIONAL_TOP_DROP_FRACTION = 0.70
PROVISIONAL_TOP_BRAKING_SAMPLES = 3
BENCH_SHORT_DISTANCE_MIN_M = 0.03
RECORDING_FLUSH_INTERVAL_S = 0.2
RECORDING_QUEUE_MAX_RECORDS = 4096


class AdaptiveSampleClock:
    """Estimate packet frequency from long sequence-versus-host-time windows."""

    def __init__(self, fallback_rate_hz: float = SAMPLE_RATE_HZ) -> None:
        self.fallback_rate_hz = fallback_rate_hz
        self.rate_hz = fallback_rate_hz
        self.confidence = "fallback"
        self.history: deque[tuple[int, float]] = deque()
        self.last_sequence: int | None = None
        self.unwrapped_sequence = 0
        self.last_valid_host_timestamp: float | None = None
        self.last_update_host_timestamp: float | None = None

    def observe(self, sequence: int, host_timestamp: float) -> int:
        """Observe one packet and return its unwrapped sequence step."""
        if self.last_sequence is None:
            sequence_delta = 1
        else:
            sequence_delta = (sequence - self.last_sequence) & 0xFFFF
        self.last_sequence = sequence
        if sequence_delta == 0:
            return 0
        self.unwrapped_sequence += sequence_delta

        valid_timestamp = (
            math.isfinite(host_timestamp)
            and (
                self.last_valid_host_timestamp is None
                or host_timestamp > self.last_valid_host_timestamp
            )
        )
        if not valid_timestamp:
            return sequence_delta

        if (
            self.last_valid_host_timestamp is not None
            and host_timestamp - self.last_valid_host_timestamp > 30.0
        ):
            self.history.clear()
            self.last_update_host_timestamp = None
            self.confidence = "fallback"
        self.last_valid_host_timestamp = host_timestamp
        self.history.append((self.unwrapped_sequence, host_timestamp))
        while (
            self.history
            and host_timestamp - self.history[0][1]
            > CLOCK_MAX_WINDOW_S + 1.0
        ):
            self.history.popleft()
        self._maybe_update(host_timestamp)
        return sequence_delta

    def _maybe_update(self, host_timestamp: float) -> None:
        if (
            self.last_update_host_timestamp is not None
            and host_timestamp - self.last_update_host_timestamp
            < CLOCK_UPDATE_INTERVAL_S
        ):
            return
        self.last_update_host_timestamp = host_timestamp
        if len(self.history) < 3:
            return

        latest_sequence, latest_time = self.history[-1]
        slopes: list[float] = []
        for target_window_s in (2.0, 3.0, 4.0, 6.0, 8.0):
            candidates = [
                item
                for item in self.history
                if latest_time - item[1] >= target_window_s
            ]
            if not candidates:
                continue
            start_sequence, start_time = candidates[-1]
            duration = latest_time - start_time
            if duration < CLOCK_MIN_WINDOW_S:
                continue
            slope = (latest_sequence - start_sequence) / duration
            if MIN_SAMPLE_RATE_HZ <= slope <= MAX_SAMPLE_RATE_HZ:
                slopes.append(slope)
        if not slopes:
            return

        target = statistics.median(slopes)
        bounded_target = min(
            self.rate_hz + CLOCK_MAX_UPDATE_HZ,
            max(self.rate_hz - CLOCK_MAX_UPDATE_HZ, target),
        )
        self.rate_hz += CLOCK_EWMA_ALPHA * (
            bounded_target - self.rate_hz
        )
        self.rate_hz = min(
            MAX_SAMPLE_RATE_HZ,
            max(MIN_SAMPLE_RATE_HZ, self.rate_hz),
        )
        observed_span = latest_time - self.history[0][1]
        self.confidence = (
            "high" if observed_span >= 6.0 else "medium"
        )

    @property
    def sample_interval_s(self) -> float:
        return 1.0 / self.rate_hz


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
        self.sample_clock = AdaptiveSampleClock()
        self.sample_interval_s = SAMPLE_INTERVAL_S
        self.rest_window_duration_s = (
            self.config.rest_window_samples / SAMPLE_RATE_HZ
        )

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
            maxlen=max(
                self.config.rest_window_samples,
                round(self.rest_window_duration_s * MAX_SAMPLE_RATE_HZ),
            )
        )
        self.rest_world_acceleration_g: deque[
            tuple[float, float, float]
        ] = deque(
            maxlen=max(
                self.config.rest_window_samples,
                round(self.rest_window_duration_s * MAX_SAMPLE_RATE_HZ),
            )
        )
        self.rest_quaternions: deque[tuple[float, float, float, float]] = deque(
            maxlen=max(
                self.config.rest_window_samples,
                round(self.rest_window_duration_s * MAX_SAMPLE_RATE_HZ),
            )
        )
        self.rest_confirmed = False
        self.rest_confidence = 0.0
        self.rest_acceleration_variation_m_s2 = 0.0
        self.orientation_change_deg = 0.0
        self.rest_orientation_jitter_deg = 0.0
        self.orientation_baseline_deg = 0.0
        self.orientation_baseline_lower_deg = 0.0
        self.orientation_baseline_upper_deg = 0.0
        self.orientation_baseline_mad_deg = 0.0
        self.orientation_start_threshold_deg = (
            ORIENTATION_MIN_RISE_DEG
        )
        self.orientation_region_active = False
        self.orientation_region_confirmed = False
        self.orientation_region_id = 0
        self.orientation_region_started_s = 0.0
        self.orientation_region_peak_deg = 0.0
        self.orientation_region_prominence_deg = 0.0
        self.orientation_region_excess_area_deg_s = 0.0
        self.orientation_region_propulsion_samples = 0
        self.orientation_region_braking_samples = 0
        self.orientation_region_start_samples = 0
        self.orientation_region_started_this_sample = False
        self.orientation_region_ended_this_sample = False
        self.orientation_region_end_reason = ""
        self.orientation_baseline_values: deque[float] = deque(
            maxlen=max(
                ORIENTATION_START_CONFIRM_SAMPLES,
                round(ORIENTATION_BASELINE_WINDOW_S * MAX_SAMPLE_RATE_HZ),
            )
        )
        self.orientation_settle_values: deque[float] = deque(
            maxlen=max(
                ORIENTATION_START_CONFIRM_SAMPLES,
                round(
                    ORIENTATION_SETTLE_DURATION_S * MAX_SAMPLE_RATE_HZ
                ),
            )
        )

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
        self.phase_provisional_top_index: int | None = None
        self.phase_provisional_top_velocity_m_s = 0.0
        self.phase_provisional_top_s = 0.0
        self.phase_reacceleration_samples: list[float] = []
        self.phase_orientation_region_id = 0
        self.phase_boundary_s: float | None = None
        self.resynchronization_reason = ""
        self.downward_motion_seen = False
        self.down_peak_velocity_m_s = 0.0
        self.bottom_candidate_samples = 0
        self.down_phase_orientation_region_id = 0
        self.recovery_orientation_region_id = 0
        self.recovery_requires_new_region = False

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
        sequence_delta = self.sample_clock.observe(
            sample.sequence,
            sample.host_timestamp,
        )
        self.sample_interval_s = self.sample_clock.sample_interval_s

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
                self._enter_recovery(require_new_region=True)

        sample_dt = sequence_delta * self.sample_interval_s
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
        self._update_orientation_region(sample_dt, events)

        if self.state == self.REST:
            self._handle_rest(sample, events)
        elif self.state == self.UP:
            self._handle_up(sample, sample_dt, events)
        elif self.state == self.DOWN:
            self._handle_down(sample, sample_dt, events)
        else:
            self._handle_recovery(sample, events)

        self.previous_acceleration_m_s2 = self.filtered_acceleration_m_s2
        return events, self._record(
            sample,
            state_before,
            sequence_delta,
            sample_dt,
            self.raw_acceleration_m_s2,
        )

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
                "sample_rate_hz": self.sample_clock.rate_hz,
                "sample_rate_confidence": self.sample_clock.confidence,
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
        required_samples = max(
            3,
            round(
                self.rest_window_duration_s
                * self.sample_clock.rate_hz
            ),
        )
        if len(self.rest_vertical_g) < required_samples:
            self.rest_confirmed = False
            self.rest_confidence = 0.0
            self.rest_acceleration_variation_m_s2 = 0.0
            self.orientation_change_deg = 0.0
            return

        recent_accelerations = list(self.rest_world_acceleration_g)[
            -required_samples:
        ]
        recent_quaternions = list(self.rest_quaternions)[-required_samples:]
        axis_variances_g2 = [
            statistics.pvariance(
                acceleration[axis]
                for acceleration in recent_accelerations
            )
            for axis in range(3)
        ]
        self.rest_acceleration_variation_m_s2 = (
            math.sqrt(sum(axis_variances_g2)) * GRAVITY_M_S2
        )
        first_quaternion = recent_quaternions[0]
        self.orientation_change_deg = max(
            self._quaternion_angle_deg(first_quaternion, quaternion)
            for quaternion in recent_quaternions
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

    def _update_orientation_region(
        self,
        dt: float,
        events: list[MotionEvent],
    ) -> None:
        """Track prominent activity relative to a moving local angle band."""
        self.orientation_region_started_this_sample = False
        self.orientation_region_ended_this_sample = False
        self.orientation_region_end_reason = ""
        value = self.orientation_change_deg
        settle_samples = max(
            ORIENTATION_START_CONFIRM_SAMPLES,
            round(
                ORIENTATION_SETTLE_DURATION_S
                * self.sample_clock.rate_hz
            ),
        )
        self.orientation_settle_values.append(value)
        recent = list(self.orientation_settle_values)[-settle_samples:]
        stable_band = (
            len(recent) >= settle_samples
            and max(recent) - min(recent)
            <= ORIENTATION_SETTLE_RANGE_DEG
        )

        if not self.orientation_region_active:
            if stable_band:
                self.orientation_baseline_values.extend(recent)
                self._refresh_orientation_baseline()

            if value > self.orientation_start_threshold_deg:
                self.orientation_region_start_samples += 1
            else:
                self.orientation_region_start_samples = 0

            if (
                self.orientation_region_start_samples
                >= ORIENTATION_START_CONFIRM_SAMPLES
            ):
                self.orientation_region_id += 1
                self.orientation_region_active = True
                self.orientation_region_confirmed = False
                self.orientation_region_started_this_sample = True
                self.orientation_region_started_s = self.sensor_time_s - (
                    ORIENTATION_START_CONFIRM_SAMPLES - 1
                ) * dt
                self.orientation_region_peak_deg = value
                self.orientation_region_prominence_deg = max(
                    0.0,
                    value - self.orientation_baseline_upper_deg,
                )
                self.orientation_region_excess_area_deg_s = sum(
                    max(
                        0.0,
                        item - self.orientation_baseline_upper_deg,
                    )
                    * dt
                    for item in list(self.orientation_settle_values)[
                        -ORIENTATION_START_CONFIRM_SAMPLES:
                    ]
                )
                self.orientation_region_propulsion_samples = int(
                    self.filtered_acceleration_m_s2
                    >= self.start_threshold_m_s2
                )
                self.orientation_region_braking_samples = 0
                self.orientation_region_start_samples = 0
                events.append(
                    MotionEvent(
                        "orientation_region_started",
                        "orientation rose above its local baseline band",
                        quality=self._orientation_region_quality(),
                    )
                )
            return

        self.orientation_region_peak_deg = max(
            self.orientation_region_peak_deg,
            value,
        )
        self.orientation_region_prominence_deg = max(
            0.0,
            (
                self.orientation_region_peak_deg
                - self.orientation_baseline_upper_deg
            ),
        )
        self.orientation_region_excess_area_deg_s += (
            max(0.0, value - self.orientation_baseline_upper_deg) * dt
        )
        if self.filtered_acceleration_m_s2 >= self.start_threshold_m_s2:
            self.orientation_region_propulsion_samples += 1
        elif (
            self.orientation_region_propulsion_samples
            >= START_CONFIRM_SAMPLES
            and self.filtered_acceleration_m_s2
            <= -self.start_threshold_m_s2
        ):
            self.orientation_region_braking_samples += 1

        region_duration = (
            self.sensor_time_s - self.orientation_region_started_s
        )
        settled_below_peak = (
            stable_band
            and statistics.median(recent)
            <= (
                self.orientation_region_peak_deg
                - ORIENTATION_MIN_PROMINENCE_DEG
            )
        )
        if (
            region_duration >= self.profile.min_duration_s
            and settled_below_peak
        ):
            self.orientation_region_confirmed = (
                self.orientation_region_prominence_deg
                >= ORIENTATION_MIN_PROMINENCE_DEG
                and self.orientation_region_excess_area_deg_s
                >= ORIENTATION_MIN_EXCESS_AREA_DEG_S
                and self.orientation_region_propulsion_samples
                >= START_CONFIRM_SAMPLES
                and self.orientation_region_braking_samples
                >= PROVISIONAL_TOP_BRAKING_SAMPLES
            )
            self.orientation_region_active = False
            self.orientation_region_ended_this_sample = True
            self.orientation_region_end_reason = "stable local valley"
            events.append(
                MotionEvent(
                    "orientation_region_ended",
                    (
                        "confirmed fused orientation region"
                        if self.orientation_region_confirmed
                        else "orientation region lacked fused motion evidence"
                    ),
                    quality=self._orientation_region_quality(),
                )
            )
            self.orientation_baseline_values.clear()
            self.orientation_baseline_values.extend(recent)
            self._refresh_orientation_baseline()

    def _refresh_orientation_baseline(self) -> None:
        if not self.orientation_baseline_values:
            return
        values = list(self.orientation_baseline_values)
        baseline = statistics.median(values)
        mad = statistics.median(abs(value - baseline) for value in values)
        self.orientation_baseline_deg = baseline
        self.orientation_baseline_mad_deg = mad
        self.orientation_baseline_lower_deg = min(values)
        self.orientation_baseline_upper_deg = max(
            max(values),
            baseline + 2.0 * mad,
        )
        self.orientation_start_threshold_deg = (
            self.orientation_baseline_upper_deg
            + max(
                ORIENTATION_MIN_RISE_DEG,
                ORIENTATION_MAD_MULTIPLIER * mad,
            )
        )

    def _orientation_region_quality(
        self,
    ) -> dict[str, bool | float | int | str]:
        return {
            "orientation_region_id": self.orientation_region_id,
            "orientation_region_confirmed": (
                self.orientation_region_confirmed
            ),
            "orientation_region_started_s": (
                self.orientation_region_started_s
            ),
            "orientation_region_peak_deg": (
                self.orientation_region_peak_deg
            ),
            "orientation_region_prominence_deg": (
                self.orientation_region_prominence_deg
            ),
            "orientation_region_excess_area_deg_s": (
                self.orientation_region_excess_area_deg_s
            ),
            "orientation_baseline_deg": self.orientation_baseline_deg,
            "orientation_baseline_lower_deg": (
                self.orientation_baseline_lower_deg
            ),
            "orientation_baseline_upper_deg": (
                self.orientation_baseline_upper_deg
            ),
            "orientation_start_threshold_deg": (
                self.orientation_start_threshold_deg
            ),
            "propulsion_samples": (
                self.orientation_region_propulsion_samples
            ),
            "braking_samples": self.orientation_region_braking_samples,
        }

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
                sum(self.positive_start_samples) * self.sample_interval_s
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
                sum(self.negative_start_samples) * self.sample_interval_s
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
        ) * self.sample_interval_s
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        self.phase_points = [MotionPoint(0.0, 0.0, 0.0)]
        previous_acceleration = buffered[0]
        for index, acceleration in enumerate(buffered[1:], 1):
            previous_velocity = self.velocity_m_s
            self.velocity_m_s += 0.5 * (
                previous_acceleration + acceleration
            ) * self.sample_interval_s
            self.displacement_m += 0.5 * (
                previous_velocity + self.velocity_m_s
            ) * self.sample_interval_s
            self.phase_points.append(
                MotionPoint(
                    index * self.sample_interval_s,
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
        self.phase_provisional_top_index = None
        self.phase_provisional_top_velocity_m_s = 0.0
        self.phase_provisional_top_s = 0.0
        self.phase_reacceleration_samples = []
        self.phase_orientation_region_id = self.orientation_region_id
        self.phase_boundary_s = None
        self.resynchronization_reason = ""
        self.positive_start_samples = []
        self.negative_start_samples = []
        self.start_buffer_from_bottom = False

    def _start_down_from_rest(self) -> None:
        buffered = list(self.negative_start_samples)
        self._begin_motion_bout()
        self.state = self.DOWN
        self.phase_started_s = self.sensor_time_s - (
            len(buffered) - 1
        ) * self.sample_interval_s
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        previous_acceleration = buffered[0]
        for acceleration in buffered[1:]:
            self.velocity_m_s += 0.5 * (
                previous_acceleration + acceleration
            ) * self.sample_interval_s
            previous_acceleration = acceleration
        self.previous_acceleration_m_s2 = buffered[-1]
        self.phase_missed_samples = 0
        self.down_peak_velocity_m_s = self.velocity_m_s
        self.downward_motion_seen = (
            self.velocity_m_s <= -self.profile.min_peak_velocity_m_s
        )
        self.bottom_candidate_samples = 0
        self.down_phase_orientation_region_id = self.orientation_region_id
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

        provisional_top_ready = (
            self.phase_peak_velocity_m_s
            >= self.profile.min_peak_velocity_m_s
            and self.phase_braking_samples
            >= PROVISIONAL_TOP_BRAKING_SAMPLES
            and self.velocity_m_s
            <= (
                (1.0 - PROVISIONAL_TOP_DROP_FRACTION)
                * self.phase_peak_velocity_m_s
            )
        )
        if provisional_top_ready and (
            self.phase_provisional_top_index is None
            or self.velocity_m_s
            < self.phase_provisional_top_velocity_m_s
        ):
            first_provisional_top = (
                self.phase_provisional_top_index is None
            )
            self.phase_provisional_top_index = len(self.phase_points) - 1
            self.phase_provisional_top_velocity_m_s = self.velocity_m_s
            self.phase_provisional_top_s = self.sensor_time_s
            if first_provisional_top:
                events.append(
                    MotionEvent(
                        "provisional_top",
                        "local velocity minimum after propulsion and braking",
                        quality={
                            "phase_started_s": self.phase_started_s,
                            "provisional_top_s": self.phase_provisional_top_s,
                            "raw_velocity_m_s": self.velocity_m_s,
                            "peak_velocity_m_s": (
                                self.phase_peak_velocity_m_s
                            ),
                            "velocity_drop_fraction": (
                                1.0
                                - self.velocity_m_s
                                / self.phase_peak_velocity_m_s
                            ),
                        },
                    )
                )

        if self.phase_provisional_top_index is not None:
            if self.filtered_acceleration_m_s2 >= self.start_threshold_m_s2:
                self.phase_reacceleration_samples.append(
                    self.filtered_acceleration_m_s2
                )
            elif self.filtered_acceleration_m_s2 < 0.0:
                self.phase_reacceleration_samples = []
            if (
                len(self.phase_reacceleration_samples)
                >= START_CONFIRM_SAMPLES
                and (
                    self.orientation_region_id
                    > self.phase_orientation_region_id
                    or self.orientation_region_active
                )
            ):
                self._finish_up_at_orientation_boundary(sample, events)
                return
            if (
                self.orientation_region_ended_this_sample
                and self.orientation_region_prominence_deg
                >= ORIENTATION_MIN_PROMINENCE_DEG
                and self.orientation_region_excess_area_deg_s
                >= ORIENTATION_MIN_EXCESS_AREA_DEG_S
                and self.phase_propulsion_samples
                >= START_CONFIRM_SAMPLES
                and self.phase_braking_samples
                >= PROVISIONAL_TOP_BRAKING_SAMPLES
                and self.phase_missed_samples == 0
            ):
                self._finish_up_at_settled_orientation_boundary(events)
                return

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

    def _finish_up(
        self,
        events: list[MotionEvent],
        *,
        top_detection: str = "velocity",
        evidence: str = "velocity peak + braking + zero return",
        reason: str = "upward velocity returned to zero",
    ) -> None:
        metrics, corrected_trace, drift = self._corrected_phase_metrics()
        failures = self._metric_failures(metrics)
        short_distance = (
            self.profile.name == "bench"
            and BENCH_SHORT_DISTANCE_MIN_M
            <= metrics["displacement_m"]
            < self.profile.min_displacement_m
        )
        status = (
            "rejected"
            if failures
            else "short_distance"
            if short_distance
            else "recovered_top"
            if top_detection != "velocity"
            else "accepted"
        )
        quality = self._phase_quality(
            status,
            drift,
            top_detection=top_detection,
            evidence=evidence,
        )
        quality["short_distance"] = short_distance
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
                reason,
                metrics=metrics,
                quality=quality,
            )
        )
        self._enter_down_after_top()

    def _finish_up_at_orientation_boundary(
        self,
        sample: ImuSample,
        events: list[MotionEvent],
    ) -> None:
        """Close a drifted lobe at its best velocity minimum and restart."""
        assert self.phase_provisional_top_index is not None
        buffered = list(self.phase_reacceleration_samples)
        boundary_index = self.phase_provisional_top_index
        self.phase_points = self.phase_points[: boundary_index + 1]
        boundary_point = self.phase_points[-1]
        self.velocity_m_s = boundary_point.velocity_m_s
        self.displacement_m = boundary_point.displacement_m
        self.phase_boundary_s = self.phase_provisional_top_s
        self.resynchronization_reason = (
            "new fused orientation and propulsion lobe"
        )
        self._finish_up(
            events,
            top_detection="orientation_velocity_boundary",
            evidence=(
                "velocity peak + braking + 70% fall + "
                "new orientation/propulsion lobe"
            ),
            reason="top recovered at local velocity minimum",
        )
        if not buffered:
            return
        self.positive_start_samples = buffered
        self.start_buffer_from_bottom = True
        self._start_up_from_rest(sample.quaternion_xyzw)
        self.phase_started_from_confirmed_rest = False
        self.phase_started_from_bottom = True
        self.resynchronization_reason = (
            "continued after orientation velocity boundary"
        )
        events.append(
            MotionEvent(
                "bottom",
                "next fused lobe re-armed from local turning point",
            )
        )
        events.append(
            MotionEvent(
                "up_started",
                "continuous repetition started after recovered boundary",
            )
        )

    def _finish_up_at_settled_orientation_boundary(
        self,
        events: list[MotionEvent],
    ) -> None:
        """Close the final lobe when its orientation region settles."""
        assert self.phase_provisional_top_index is not None
        self.phase_points = self.phase_points[
            : self.phase_provisional_top_index + 1
        ]
        boundary_point = self.phase_points[-1]
        self.velocity_m_s = boundary_point.velocity_m_s
        self.displacement_m = boundary_point.displacement_m
        self.phase_boundary_s = self.phase_provisional_top_s
        self.resynchronization_reason = (
            "orientation region settled after a drifted velocity top"
        )
        self._finish_up(
            events,
            top_detection="orientation_velocity_boundary",
            evidence=(
                "velocity peak + braking + 70% fall + "
                "confirmed settled orientation region"
            ),
            reason="top recovered at settled orientation boundary",
        )

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
            self.rest_window_duration_s
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
        quality["short_distance"] = (
            self.profile.name == "bench"
            and metrics["displacement_m"]
            < self.profile.min_displacement_m
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
        minimum_displacement = (
            BENCH_SHORT_DISTANCE_MIN_M
            if self.profile.name == "bench"
            else self.profile.min_displacement_m
        )
        if metrics["displacement_m"] < minimum_displacement:
            failures.append(
                f"displacement below {minimum_displacement:.2f} m"
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
                self.rest_window_duration_s
                if self.rest_confirmed
                else 0.0
            ),
            "phase_started_s": self.phase_started_s,
            "phase_ended_s": (
                self.phase_boundary_s
                if self.phase_boundary_s is not None
                else self.sensor_time_s
            ),
            "phase_started_from_bottom": self.phase_started_from_bottom,
            "phase_started_from_confirmed_rest": (
                self.phase_started_from_confirmed_rest
            ),
            "exercise_profile": self.profile.name,
            "motion_bout_id": self.active_motion_bout_id or 0,
            "estimated_sample_rate_hz": self.sample_clock.rate_hz,
            "rate_confidence": self.sample_clock.confidence,
            "resynchronization_reason": self.resynchronization_reason,
            "orientation_region_id": self.orientation_region_id,
            "orientation_region_prominence_deg": (
                self.orientation_region_prominence_deg
            ),
            "orientation_region_excess_area_deg_s": (
                self.orientation_region_excess_area_deg_s
            ),
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
        self.down_phase_orientation_region_id = self.orientation_region_id
        self.deceleration_seen = False
        self.end_candidate_samples = 0

    def _handle_down(
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

        if self.filtered_acceleration_m_s2 >= self.start_threshold_m_s2:
            self.positive_start_samples.append(
                self.filtered_acceleration_m_s2
            )
        elif self.filtered_acceleration_m_s2 < 0.0:
            self.positive_start_samples = []
        if (
            len(self.positive_start_samples) >= START_CONFIRM_SAMPLES
            and self.downward_motion_seen
            and self.orientation_region_active
        ):
            buffered = list(self.positive_start_samples)
            self._enter_rest(upward_armed=True)
            self.positive_start_samples = buffered
            self.start_buffer_from_bottom = True
            self._start_up_from_rest(sample.quaternion_xyzw)
            self.phase_started_from_confirmed_rest = False
            self.phase_started_from_bottom = True
            self.resynchronization_reason = (
                "new fused lobe closed the preceding downward phase"
            )
            events.append(
                MotionEvent(
                    "bottom",
                    "downward phase re-synchronized at new fused lobe",
                )
            )
            events.append(
                MotionEvent(
                    "up_started",
                    "continuous repetition started after downward resync",
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

    def _handle_recovery(
        self,
        sample: ImuSample,
        events: list[MotionEvent],
    ) -> None:
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        if self.rest_confirmed:
            self._adopt_rest_baseline()
            self.active_motion_bout_id = None
            self._enter_rest(upward_armed=True)
            events.append(
                MotionEvent(
                    "rest",
                    "confirmed rest; detector re-armed",
                )
            )
            return

        if self.filtered_acceleration_m_s2 >= self.start_threshold_m_s2:
            self.positive_start_samples.append(
                self.filtered_acceleration_m_s2
            )
        elif self.filtered_acceleration_m_s2 < 0.0:
            self.positive_start_samples = []
        if (
            len(self.positive_start_samples) < START_CONFIRM_SAMPLES
            or not self.orientation_region_active
            or (
                self.recovery_requires_new_region
                and self.orientation_region_id
                <= self.recovery_orientation_region_id
            )
        ):
            return

        buffered = list(self.positive_start_samples)
        self.state = self.REST
        self.upward_armed = True
        self.positive_start_samples = buffered
        self.start_buffer_from_bottom = True
        self._start_up_from_rest(sample.quaternion_xyzw)
        self.phase_started_from_confirmed_rest = False
        self.phase_started_from_bottom = True
        self.resynchronization_reason = (
            "clean fused region re-armed detector from recovery"
        )
        events.append(
            MotionEvent(
                "up_started",
                "fused orientation and propulsion re-armed recovery",
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

    def _enter_recovery(
        self,
        *,
        require_new_region: bool = False,
    ) -> None:
        self.state = self.RECOVERY
        self.upward_armed = False
        self.recovery_orientation_region_id = self.orientation_region_id
        self.recovery_requires_new_region = require_new_region
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
        self.phase_provisional_top_index = None
        self.phase_provisional_top_velocity_m_s = 0.0
        self.phase_provisional_top_s = 0.0
        self.phase_reacceleration_samples = []
        self.phase_orientation_region_id = 0
        self.phase_boundary_s = None
        self.resynchronization_reason = ""
        self.downward_motion_seen = False
        self.down_peak_velocity_m_s = 0.0
        self.bottom_candidate_samples = 0
        self.down_phase_orientation_region_id = 0
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
            "orientation_baseline_deg": self.orientation_baseline_deg,
            "orientation_baseline_lower_deg": (
                self.orientation_baseline_lower_deg
            ),
            "orientation_baseline_upper_deg": (
                self.orientation_baseline_upper_deg
            ),
            "orientation_baseline_mad_deg": (
                self.orientation_baseline_mad_deg
            ),
            "orientation_start_threshold_deg": (
                self.orientation_start_threshold_deg
            ),
            "orientation_region_active": self.orientation_region_active,
            "orientation_region_confirmed": (
                self.orientation_region_confirmed
            ),
            "orientation_region_id": self.orientation_region_id,
            "orientation_region_peak_deg": (
                self.orientation_region_peak_deg
            ),
            "orientation_region_prominence_deg": (
                self.orientation_region_prominence_deg
            ),
            "orientation_region_excess_area_deg_s": (
                self.orientation_region_excess_area_deg_s
            ),
            "orientation_region_started": (
                self.orientation_region_started_this_sample
            ),
            "orientation_region_ended": (
                self.orientation_region_ended_this_sample
            ),
            "orientation_region_end_reason": (
                self.orientation_region_end_reason
            ),
            "estimated_sample_rate_hz": self.sample_clock.rate_hz,
            "rate_confidence": self.sample_clock.confidence,
            "exercise_profile": self.profile.name,
            "state_before": state_before,
            "state_after": self.state,
            "velocity_m_s": self.velocity_m_s,
            "displacement_m": self.displacement_m,
        }


class SessionRecorder:
    _STOP = object()

    def __init__(
        self,
        path: Path,
        exercise: str = "generic",
        *,
        flush_interval_s: float = RECORDING_FLUSH_INTERVAL_S,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.file = path.open("w", encoding="utf-8")
        self.flush_interval_s = max(0.01, float(flush_interval_s))
        self._queue: queue.Queue[dict | object] = queue.Queue(
            maxsize=RECORDING_QUEUE_MAX_RECORDS
        )
        self._worker_error: BaseException | None = None
        self._worker_done = threading.Event()
        self._closed = False
        self._worker = threading.Thread(
            target=self._writer_loop,
            name="beast-recording-writer",
            daemon=False,
        )
        self._worker.start()
        self.write(
            {
                "type": "metadata",
                "created_at": datetime.now().astimezone().isoformat(
                    timespec="seconds"
                ),
                "algorithm_version": ALGORITHM_VERSION,
                "exercise_profile": exercise,
                "sample_interval_s": SAMPLE_INTERVAL_S,
                "fallback_sample_rate_hz": SAMPLE_RATE_HZ,
                "sample_rate_mode": "adaptive_sequence_host_clock",
                "packet_layout": (
                    "<Hhhhhhhh: sequence, qx, qy, qw, qz, ax, ay, az"
                ),
            }
        )

    def write(self, record: dict) -> None:
        if self._closed:
            raise RuntimeError("Cannot write to a closed recording.")
        self._raise_worker_error()
        try:
            self._queue.put_nowait(record)
        except queue.Full as exc:
            raise RuntimeError(
                "The recording writer cannot keep up with sensor packets."
            ) from exc

    def mark_tracker_reset(self, host_timestamp: float) -> None:
        self.write({"type": "tracker_reset", "host_timestamp": host_timestamp})

    def close(self) -> None:
        if self._closed:
            self._raise_worker_error()
            return
        self._closed = True
        if self._worker_error is None:
            while not self._worker_done.is_set():
                try:
                    self._queue.put(self._STOP, timeout=0.1)
                    break
                except queue.Full:
                    self._raise_worker_error()
        self._worker.join(timeout=10.0)
        if self._worker.is_alive():
            raise RuntimeError("Timed out while closing the recording writer.")
        self._raise_worker_error()

    def _writer_loop(self) -> None:
        pending = False
        flush_deadline = 0.0
        try:
            while True:
                timeout = (
                    max(0.0, flush_deadline - time.monotonic())
                    if pending
                    else None
                )
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    self.file.flush()
                    pending = False
                    continue

                if item is self._STOP:
                    if pending:
                        self.file.flush()
                    break

                self.file.write(
                    json.dumps(item, separators=(",", ":")) + "\n"
                )
                if not pending:
                    pending = True
                    flush_deadline = time.monotonic() + self.flush_interval_s
                elif time.monotonic() >= flush_deadline:
                    self.file.flush()
                    pending = False
        except BaseException as exc:
            self._worker_error = exc
        finally:
            try:
                self.file.flush()
            except BaseException as exc:
                if self._worker_error is None:
                    self._worker_error = exc
            self._worker_done.set()
            try:
                self.file.close()
            except BaseException as exc:
                if self._worker_error is None:
                    self._worker_error = exc

    def _raise_worker_error(self) -> None:
        if self._worker_error is not None:
            raise RuntimeError("The recording writer failed.") from self._worker_error


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
