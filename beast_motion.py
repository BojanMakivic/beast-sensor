"""Signal processing for generic Beast Sensor vertical repetitions."""

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


ALGORITHM_VERSION = "generic-reversal-v1"
GRAVITY_M_S2 = 9.80665
SAMPLE_INTERVAL_S = 0.020

# The live detector uses motion/noise thresholds, never a configured distance.
CALIBRATION_SAMPLES = 100
CALIBRATION_MAX_VERTICAL_STD_G = 0.010
CALIBRATION_MIN_VERTICAL_G = 0.85
FILTER_CUTOFF_HZ = 8.0
MIN_START_ACCELERATION_M_S2 = 0.08
START_CONFIRM_SAMPLES = 3
STATIONARY_CONFIRM_SAMPLES = 8
STATIONARY_VERTICAL_STD_G = 0.003
STATIONARY_NORM_TOLERANCE_G = 0.08
MIN_PEAK_VELOCITY_M_S = 0.035
MIN_REP_DURATION_S = 0.12
MIN_STATIONARY_PHASE_S = 0.40
MAX_PHASE_DURATION_S = 5.0
MAX_MISSING_SAMPLES = 1


@dataclass(frozen=True)
class ImuSample:
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
    quality: dict[str, float | int | str] | None = None


def rotate_body_to_world(
    quaternion_xyzw: tuple[float, float, float, float],
    vector: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Rotate a body-frame vector using the sensor's x,y,z,w quaternion."""
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
    """Decode sequence, x/y/z/w quaternion, and acceleration from one packet."""
    if len(data) < 16:
        return None

    sequence, qx, qy, qz, qw, ax, ay, az = struct.unpack(
        "<Hhhhhhhh", bytes(data[:16])
    )
    quaternion_norm = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if quaternion_norm == 0.0:
        return None

    quaternion = (
        qx / quaternion_norm,
        qy / quaternion_norm,
        qz / quaternion_norm,
        qw / quaternion_norm,
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
    """Count upward phases from bottom velocity reversal to top reversal."""

    CALIBRATING = "calibrating"
    REST = "rest"
    UP = "up"
    DOWN = "down"

    def __init__(self) -> None:
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

        self.last_sequence: int | None = None
        self.sensor_time_s = 0.0
        self.filtered_acceleration_m_s2 = 0.0
        self.previous_acceleration_m_s2 = 0.0
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        self.phase_started_s = 0.0
        self.phase_points: list[MotionPoint] = []
        self.phase_peak_velocity_m_s = 0.0
        self.phase_missed_samples = 0
        self.deceleration_seen = False
        self.downward_motion_seen = False

        self.positive_start_samples: list[float] = []
        self.negative_start_samples: list[float] = []
        self.stationary_samples = 0
        self.stationary_vertical_g: deque[float] = deque(
            maxlen=STATIONARY_CONFIRM_SAMPLES
        )
        self.stationary_norm_g: deque[float] = deque(
            maxlen=STATIONARY_CONFIRM_SAMPLES
        )
        self.total_missing_samples = 0
        self.duplicate_packets = 0

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
                self._reset_to_rest()
                events.append(
                    MotionEvent("rejected", "motion interrupted by missing samples")
                )

        sample_dt = sequence_delta * SAMPLE_INTERVAL_S
        self.sensor_time_s += sample_dt

        if self.state == self.CALIBRATING:
            ready = self._calibrate(sample)
            if ready is not None:
                events.append(ready)
            return events, self._record(
                sample, state_before, sequence_delta, sample_dt
            )

        assert self.gravity_baseline_g is not None
        raw_acceleration = (
            self.gravity_sign * (sample.vertical_g - self.gravity_baseline_g)
            * GRAVITY_M_S2
        )
        alpha = 1.0 - math.exp(
            -2.0 * math.pi * FILTER_CUTOFF_HZ * SAMPLE_INTERVAL_S
        )
        self.filtered_acceleration_m_s2 += alpha * (
            raw_acceleration - self.filtered_acceleration_m_s2
        )
        self.stationary_vertical_g.append(sample.vertical_g)
        self.stationary_norm_g.append(
            math.sqrt(sum(value * value for value in sample.acceleration_g))
        )
        stationary = (
            len(self.stationary_vertical_g) == STATIONARY_CONFIRM_SAMPLES
            and statistics.pstdev(self.stationary_vertical_g)
            <= STATIONARY_VERTICAL_STD_G
            and abs(statistics.median(self.stationary_norm_g) - 1.0)
            <= STATIONARY_NORM_TOLERANCE_G
        )
        self.stationary_samples = self.stationary_samples + 1 if stationary else 0

        if self.state == self.REST:
            self._handle_rest(sample, events)
        elif self.state == self.UP:
            self._handle_up(sample_dt, events)
        else:
            self._handle_down(sample_dt, events)

        self.previous_acceleration_m_s2 = self.filtered_acceleration_m_s2
        return events, self._record(
            sample,
            state_before,
            sequence_delta,
            sample_dt,
            raw_acceleration,
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
        self.filtered_acceleration_m_s2 = 0.0
        self.previous_acceleration_m_s2 = 0.0
        return MotionEvent(
            "ready",
            quality={
                "gravity_baseline_g": baseline,
                "vertical_noise_m_s2": self.noise_m_s2,
                "start_threshold_m_s2": self.start_threshold_m_s2,
                "sample_rate_hz": 1.0 / SAMPLE_INTERVAL_S,
            },
        )

    def _handle_rest(
        self,
        sample: ImuSample,
        events: list[MotionEvent],
    ) -> None:
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        if self.stationary_samples > 0:
            assert self.gravity_baseline_g is not None
            self.gravity_baseline_g = statistics.median(
                self.stationary_vertical_g
            )
            self.filtered_acceleration_m_s2 = 0.0
            self.previous_acceleration_m_s2 = 0.0

        acceleration = self.filtered_acceleration_m_s2
        if acceleration >= self.start_threshold_m_s2:
            self.positive_start_samples.append(acceleration)
            self.negative_start_samples = []
            if len(self.positive_start_samples) >= START_CONFIRM_SAMPLES:
                self._start_up_from_rest()
                events.append(MotionEvent("up_started", "upward acceleration"))
        elif acceleration <= -self.start_threshold_m_s2:
            self.negative_start_samples.append(acceleration)
            self.positive_start_samples = []
            if len(self.negative_start_samples) >= START_CONFIRM_SAMPLES:
                self._start_down_from_rest()
                events.append(
                    MotionEvent("down_started", "initial movement is downward")
                )
        else:
            self.positive_start_samples = []
            self.negative_start_samples = []

    def _start_up_from_rest(self) -> None:
        buffered = list(self.positive_start_samples)
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
            (point.velocity_m_s for point in self.phase_points),
            default=0.0,
        )
        self.phase_missed_samples = 0
        self.deceleration_seen = False
        self.positive_start_samples = []
        self.negative_start_samples = []
        self.stationary_samples = 0

    def _start_down_from_rest(self) -> None:
        self.state = self.DOWN
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        self.phase_started_s = self.sensor_time_s
        self.phase_missed_samples = 0
        self.downward_motion_seen = False
        self.previous_acceleration_m_s2 = self.negative_start_samples[-1]
        self.positive_start_samples = []
        self.negative_start_samples = []
        self.stationary_samples = 0

    def _handle_up(
        self,
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
        if self.filtered_acceleration_m_s2 <= -self.start_threshold_m_s2:
            self.deceleration_seen = True

        established = (
            self.phase_peak_velocity_m_s >= MIN_PEAK_VELOCITY_M_S
            and elapsed >= MIN_REP_DURATION_S
        )
        top_crossing = (
            established
            and self.deceleration_seen
            and previous_velocity > 0.0
            and self.velocity_m_s <= 0.0
        )
        if top_crossing:
            metrics = self._metrics_at_zero_crossing(
                previous_velocity,
                self.velocity_m_s,
                dt,
            )
            quality: dict[str, float | int | str] = {
                "quality_status": (
                    "accepted"
                    if self.phase_missed_samples == 0
                    else "accepted_with_missing_samples"
                ),
                "missing_samples": self.phase_missed_samples,
                "vertical_noise_m_s2": self.noise_m_s2,
            }
            events.append(
                MotionEvent("rep", metrics=metrics, quality=quality)
            )
            events.append(MotionEvent("top", "velocity changed from up to down"))
            self._enter_down_after_top()
            return

        if self.stationary_samples > 0 and elapsed >= MIN_STATIONARY_PHASE_S:
            assert self.gravity_baseline_g is not None
            self.gravity_baseline_g = statistics.median(
                self.stationary_vertical_g
            )
            events.append(
                MotionEvent("rest", "stationary before a top reversal")
            )
            self._reset_to_rest()
            return

        if elapsed >= MAX_PHASE_DURATION_S:
            events.append(
                MotionEvent("rejected", "upward phase had no top reversal")
            )
            self._reset_to_rest()

    def _metrics_at_zero_crossing(
        self,
        previous_velocity: float,
        current_velocity: float,
        dt: float,
    ) -> dict[str, float]:
        denominator = previous_velocity - current_velocity
        fraction = (
            previous_velocity / denominator if denominator > 0.0 else 1.0
        )
        fraction = min(1.0, max(0.0, fraction))
        top_time = self.sensor_time_s - dt + fraction * dt
        duration = top_time - self.phase_started_s
        displacement = self.phase_points[-2].displacement_m
        displacement += 0.5 * previous_velocity * fraction * dt
        return {
            "duration_s": duration,
            "displacement_m": max(0.0, displacement),
            "average_speed_m_s": (
                max(0.0, displacement) / duration if duration > 0.0 else 0.0
            ),
            "peak_speed_m_s": self.phase_peak_velocity_m_s,
        }

    def _enter_down_after_top(self) -> None:
        self.state = self.DOWN
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        self.phase_started_s = self.sensor_time_s
        self.phase_points = []
        self.phase_peak_velocity_m_s = 0.0
        self.phase_missed_samples = 0
        self.downward_motion_seen = False
        self.deceleration_seen = False
        self.stationary_samples = 0

    def _handle_down(
        self,
        dt: float,
        events: list[MotionEvent],
    ) -> None:
        previous_velocity = self.velocity_m_s
        self.velocity_m_s += 0.5 * (
            self.previous_acceleration_m_s2
            + self.filtered_acceleration_m_s2
        ) * dt
        elapsed = self.sensor_time_s - self.phase_started_s
        if self.velocity_m_s <= -MIN_PEAK_VELOCITY_M_S:
            self.downward_motion_seen = True

        bottom_crossing = (
            self.downward_motion_seen
            and previous_velocity < 0.0
            and self.velocity_m_s >= 0.0
        )
        if bottom_crossing:
            positive_acceleration = self.filtered_acceleration_m_s2
            self._reset_to_rest()
            if positive_acceleration >= self.start_threshold_m_s2:
                self.positive_start_samples = [positive_acceleration]
            events.append(
                MotionEvent("bottom", "velocity changed from down to up")
            )
            return

        if (
            self.downward_motion_seen
            and self.stationary_samples > 0
            and elapsed >= MIN_STATIONARY_PHASE_S
        ):
            assert self.gravity_baseline_g is not None
            self.gravity_baseline_g = statistics.median(
                self.stationary_vertical_g
            )
            self._reset_to_rest()
            events.append(MotionEvent("bottom", "stationary after downward movement"))
            return

        if elapsed >= MAX_PHASE_DURATION_S and not self.downward_motion_seen:
            self._reset_to_rest()
            events.append(MotionEvent("rest", "no downward movement detected"))

    def _reset_to_rest(self) -> None:
        self.state = self.REST
        self.velocity_m_s = 0.0
        self.displacement_m = 0.0
        self.phase_points = []
        self.phase_peak_velocity_m_s = 0.0
        self.phase_missed_samples = 0
        self.deceleration_seen = False
        self.downward_motion_seen = False
        self.positive_start_samples = []
        self.negative_start_samples = []
        self.stationary_samples = 0

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
            "raw_vertical_acceleration_m_s2": raw_acceleration_m_s2,
            "filtered_acceleration_m_s2": self.filtered_acceleration_m_s2,
            "state_before": state_before,
            "state_after": self.state,
            "velocity_m_s": self.velocity_m_s,
            "displacement_m": self.displacement_m,
        }


class SessionRecorder:
    def __init__(self, path: Path) -> None:
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
                "sample_interval_s": SAMPLE_INTERVAL_S,
                "packet_layout": (
                    "<Hhhhhhhh: sequence, qx, qy, qz, qw, ax, ay, az"
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
