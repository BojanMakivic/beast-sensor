"""Incremental replay support for the local Beast Streamlit dashboard."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from beast_motion import (
    EXERCISE_PROFILES,
    MotionEvent,
    ReversalRepTracker,
    decode_imu_packet,
    tracker_config_for,
)


@dataclass(frozen=True)
class LiveTimedEvent:
    sensor_time_s: float
    event: MotionEvent


class LiveRecordingTail:
    """Incrementally decode an append-only JSONL recording.

    Raw packet bytes are always processed with the current detector. Calculated
    fields stored by an older live process are deliberately ignored.
    """

    def __init__(
        self,
        path: Path,
        exercise: str | None = None,
        *,
        max_records: int = 50_000,
        max_events: int = 10_000,
    ) -> None:
        self.path = Path(path)
        self.exercise_override = exercise
        self.exercise = (
            exercise if exercise in EXERCISE_PROFILES else "generic"
        )
        self.max_records = max_records
        self.max_events = max_events
        self.records: deque[dict] = deque(maxlen=max_records)
        self.events: deque[LiveTimedEvent] = deque(maxlen=max_events)
        self.tracker = ReversalRepTracker(
            tracker_config_for(self.exercise)
        )
        self.offset = 0
        self.pending_bytes = b""
        self.sample_count = 0
        self.accepted_reps = 0
        self.rejected_candidates = 0
        self.metadata: dict = {}
        self.revision = 0

    def reset(self) -> None:
        self.records.clear()
        self.events.clear()
        self.tracker = ReversalRepTracker(
            tracker_config_for(self.exercise)
        )
        self.offset = 0
        self.pending_bytes = b""
        self.sample_count = 0
        self.accepted_reps = 0
        self.rejected_candidates = 0
        self.metadata = {}
        self.revision += 1

    def read_new(self) -> int:
        """Read complete new lines and return the number of decoded samples."""
        try:
            file_size = self.path.stat().st_size
        except FileNotFoundError:
            return 0
        if file_size < self.offset:
            self.reset()

        with self.path.open("rb") as recording:
            recording.seek(self.offset)
            new_bytes = recording.read()
            self.offset = recording.tell()
        if not new_bytes:
            return 0

        combined = self.pending_bytes + new_bytes
        lines = combined.split(b"\n")
        if combined.endswith(b"\n"):
            self.pending_bytes = b""
        else:
            self.pending_bytes = lines.pop()

        decoded_samples = 0
        for raw_line in lines:
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            decoded_samples += self._process_record(record)
        if decoded_samples:
            self.revision += 1
        return decoded_samples

    def _process_record(self, record: dict) -> int:
        record_type = record.get("type")
        if record_type == "metadata":
            self.metadata = record
            if self.exercise_override is None:
                recorded_exercise = record.get("exercise_profile")
                if recorded_exercise in EXERCISE_PROFILES:
                    self.exercise = str(recorded_exercise)
                    self.tracker = ReversalRepTracker(
                        tracker_config_for(self.exercise)
                    )
            return 0
        if record_type == "tracker_reset":
            self.tracker = ReversalRepTracker(
                tracker_config_for(self.exercise)
            )
            return 0
        if record_type != "sample":
            return 0

        try:
            packet = bytes.fromhex(str(record["packet_hex"]))
            host_timestamp = float(
                record.get("host_timestamp", record.get("timestamp", 0.0))
            )
        except (KeyError, TypeError, ValueError):
            return 0
        sample = decode_imu_packet(packet, host_timestamp)
        if sample is None:
            return 0

        sample_events, current_record = self.tracker.process(sample)
        self.records.append(current_record)
        self.sample_count += 1
        for event in sample_events:
            timed = LiveTimedEvent(self.tracker.sensor_time_s, event)
            self.events.append(timed)
            if event.kind == "rep":
                self.accepted_reps += 1
            elif event.kind == "rejected":
                self.rejected_candidates += 1
        return 1

    def records_since(self, history_seconds: float | None) -> list[dict]:
        records = list(self.records)
        if not records or history_seconds is None:
            return records
        cutoff = float(records[-1]["sensor_time_s"]) - history_seconds
        return [
            record
            for record in records
            if float(record["sensor_time_s"]) >= cutoff
        ]

    def events_since(
        self,
        history_seconds: float | None,
    ) -> list[LiveTimedEvent]:
        events = list(self.events)
        if not self.records or history_seconds is None:
            return events
        cutoff = float(self.records[-1]["sensor_time_s"]) - history_seconds
        return [
            event for event in events if event.sensor_time_s >= cutoff
        ]


def latest_recording(directory: Path) -> Path | None:
    candidates = list(Path(directory).glob("*.jsonl"))
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
