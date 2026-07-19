"""Incremental replay support for the local Agile VBT Streamlit dashboard."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from agile_vbt_motion import (
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


@dataclass(frozen=True)
class LiveTailDelta:
    decoded_samples: int
    records: tuple[dict, ...]
    events: tuple[LiveTimedEvent, ...]
    reset: bool = False


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
        return self.read_delta().decoded_samples

    def read_delta(self) -> LiveTailDelta:
        """Read complete new lines and return only newly decoded output."""
        try:
            file_size = self.path.stat().st_size
        except FileNotFoundError:
            return LiveTailDelta(0, (), ())
        reset = False
        if file_size < self.offset:
            self.reset()
            reset = True

        try:
            with self.path.open("rb") as recording:
                recording.seek(self.offset)
                new_bytes = recording.read()
                self.offset = recording.tell()
        except FileNotFoundError:
            return LiveTailDelta(0, (), (), reset=reset)
        if not new_bytes:
            return LiveTailDelta(0, (), (), reset=reset)

        combined = self.pending_bytes + new_bytes
        lines = combined.split(b"\n")
        if combined.endswith(b"\n"):
            self.pending_bytes = b""
        else:
            self.pending_bytes = lines.pop()

        decoded_samples = 0
        new_records: list[dict] = []
        new_events: list[LiveTimedEvent] = []
        for raw_line in lines:
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            current_record, sample_events = self._process_record(record)
            if current_record is not None:
                decoded_samples += 1
                new_records.append(current_record)
            new_events.extend(sample_events)
        if decoded_samples:
            self.revision += 1
        return LiveTailDelta(
            decoded_samples,
            tuple(new_records),
            tuple(new_events),
            reset=reset,
        )

    def _process_record(
        self,
        record: dict,
    ) -> tuple[dict | None, tuple[LiveTimedEvent, ...]]:
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
            return None, ()
        if record_type == "tracker_reset":
            self.tracker = ReversalRepTracker(
                tracker_config_for(self.exercise)
            )
            return None, ()
        if record_type != "sample":
            return None, ()

        try:
            packet = bytes.fromhex(str(record["packet_hex"]))
            host_timestamp = float(
                record.get("host_timestamp", record.get("timestamp", 0.0))
            )
        except (KeyError, TypeError, ValueError):
            return None, ()
        sample = decode_imu_packet(packet, host_timestamp)
        if sample is None:
            return None, ()

        sample_events, current_record = self.tracker.process(sample)
        self.records.append(current_record)
        self.sample_count += 1
        timed_events: list[LiveTimedEvent] = []
        for event in sample_events:
            timed = LiveTimedEvent(self.tracker.sensor_time_s, event)
            self.events.append(timed)
            timed_events.append(timed)
            if event.kind == "rep":
                self.accepted_reps += 1
            elif event.kind == "rejected":
                self.rejected_candidates += 1
        return current_record, tuple(timed_events)

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
