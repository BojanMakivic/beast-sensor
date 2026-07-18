"""Push-based live data service for the Beast Streamlit dashboard."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from beast_dashboard_data import LiveRecordingTail, LiveTailDelta, LiveTimedEvent
from beast_motion import ALGORITHM_VERSION, EXERCISE_PROFILES


PROTOCOL_VERSION = 1
HEARTBEAT_INTERVAL_S = 1.0
CLIENT_QUEUE_SIZE = 256
CHART_RECORD_FIELDS = (
    "sensor_time_s",
    "state_after",
    "raw_vertical_acceleration_m_s2",
    "filtered_acceleration_m_s2",
    "start_threshold_m_s2",
    "gravity_baseline_g",
    "velocity_m_s",
    "displacement_m",
    "rest_confidence",
    "orientation_change_deg",
    "orientation_baseline_lower_deg",
    "orientation_baseline_upper_deg",
    "orientation_start_threshold_deg",
    "orientation_region_started",
    "orientation_region_ended",
    "orientation_region_confirmed",
    "orientation_region_id",
    "estimated_sample_rate_hz",
    "rate_confidence",
)


@dataclass(frozen=True)
class DashboardSubscription:
    mode: str
    recording: Path | None
    exercise: str | None
    history_seconds: int


@dataclass
class DashboardClient:
    identifier: int
    subscription: DashboardSubscription
    queue: asyncio.Queue[dict[str, Any]]
    path: Path | None = None

    @property
    def key(self) -> tuple[Path, str | None] | None:
        if self.path is None:
            return None
        return self.path, self.subscription.exercise


class _RecordingEventHandler(FileSystemEventHandler):
    def __init__(self, callback) -> None:
        super().__init__()
        self.callback = callback

    def on_created(self, event: FileSystemEvent) -> None:
        self._emit(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._emit(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._emit(event)

    def _emit(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        source_path = Path(event.src_path)
        if source_path.suffix.lower() == ".jsonl":
            self.callback(source_path)
        destination = getattr(event, "dest_path", None)
        if destination:
            destination_path = Path(destination)
            if destination_path.suffix.lower() == ".jsonl":
                self.callback(destination_path)


class DashboardBroker:
    """Tail recordings once and publish compact snapshots and deltas."""

    def __init__(
        self,
        recordings_directory: Path,
        *,
        allowed_recordings: tuple[Path, ...] = (),
    ) -> None:
        self.recordings_directory = Path(recordings_directory).resolve()
        self.allowed_recordings = {
            Path(path).resolve() for path in allowed_recordings
        }
        self.clients: dict[int, DashboardClient] = {}
        self.tails: dict[tuple[Path, str | None], LiveRecordingTail] = {}
        self._next_client_identifier = 1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._changes: asyncio.Queue[Path] = asyncio.Queue()
        self._observer: Observer | None = None
        self._watch_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self.recordings_directory.mkdir(parents=True, exist_ok=True)
        self._loop = asyncio.get_running_loop()
        handler = _RecordingEventHandler(self.notify_path_changed)
        observer = Observer()
        watched_directories = {self.recordings_directory}
        watched_directories.update(
            path.parent for path in self.allowed_recordings if path.parent.exists()
        )
        for directory in watched_directories:
            observer.schedule(handler, str(directory), recursive=False)
        observer.start()
        self._observer = observer
        self._watch_task = asyncio.create_task(
            self._watch_changes(),
            name="beast-dashboard-file-watcher",
        )
        self._heartbeat_task = asyncio.create_task(
            self._send_heartbeats(),
            name="beast-dashboard-heartbeat",
        )

    async def stop(self) -> None:
        for task in (self._watch_task, self._heartbeat_task):
            if task is not None:
                task.cancel()
        for task in (self._watch_task, self._heartbeat_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._observer is not None:
            self._observer.stop()
            await asyncio.to_thread(self._observer.join, 5.0)
        self.clients.clear()
        self.tails.clear()

    def notify_path_changed(self, path: Path) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._changes.put_nowait, Path(path).resolve())

    async def register(
        self,
        raw_subscription: dict[str, Any],
    ) -> DashboardClient:
        subscription = self.parse_subscription(raw_subscription)
        async with self._lock:
            client = DashboardClient(
                identifier=self._next_client_identifier,
                subscription=subscription,
                queue=asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE),
            )
            self._next_client_identifier += 1
            client.path = self._resolve_path(subscription)
            if client.key is not None:
                delta = await self._read_key(client.key)
                if delta is not None:
                    self._broadcast_delta(client.key, delta)
            self.clients[client.identifier] = client
            self._enqueue(client, self._snapshot_message(client))
            return client

    async def update_subscription(
        self,
        client_identifier: int,
        raw_subscription: dict[str, Any],
    ) -> None:
        subscription = self.parse_subscription(raw_subscription)
        async with self._lock:
            client = self.clients[client_identifier]
            client.subscription = subscription
            client.path = self._resolve_path(subscription)
            if client.key is not None:
                delta = await self._read_key(client.key)
                if delta is not None:
                    self._broadcast_delta(client.key, delta)
            self._enqueue(
                client,
                {
                    "type": "reset",
                    "protocol": PROTOCOL_VERSION,
                    "reason": "subscription_changed",
                },
            )
            self._enqueue(client, self._snapshot_message(client))

    def unregister(self, client_identifier: int) -> None:
        self.clients.pop(client_identifier, None)

    def parse_subscription(
        self,
        message: dict[str, Any],
    ) -> DashboardSubscription:
        if message.get("type") != "subscribe":
            raise ValueError("The first WebSocket message must be 'subscribe'.")
        source = message.get("source")
        if not isinstance(source, dict):
            raise ValueError("Subscription source must be an object.")
        mode = str(source.get("mode", ""))
        if mode not in {"latest", "file"}:
            raise ValueError("Source mode must be 'latest' or 'file'.")

        recording: Path | None = None
        if mode == "file":
            raw_path = source.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError("A recording path is required for file mode.")
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = self.recordings_directory / candidate
            recording = candidate.resolve()
            if (
                not recording.is_relative_to(self.recordings_directory)
                and recording not in self.allowed_recordings
            ):
                raise ValueError("The recording path is outside the allowed files.")
            if recording.suffix.lower() != ".jsonl":
                raise ValueError("The recording must be a JSONL file.")

        raw_exercise = message.get("exercise")
        exercise = None if raw_exercise in {None, ""} else str(raw_exercise)
        if exercise is not None and exercise not in EXERCISE_PROFILES:
            raise ValueError(f"Unknown exercise profile '{exercise}'.")

        try:
            history_seconds = int(message.get("history_seconds", 90))
        except (TypeError, ValueError) as exc:
            raise ValueError("History length must be an integer.") from exc
        if not 15 <= history_seconds <= 300:
            raise ValueError("History length must be between 15 and 300 seconds.")
        return DashboardSubscription(
            mode=mode,
            recording=recording,
            exercise=exercise,
            history_seconds=history_seconds,
        )

    async def process_changes(self) -> None:
        """Catch every active tail up to its current file size."""
        async with self._lock:
            changed_clients: list[DashboardClient] = []
            for client in self.clients.values():
                resolved = self._resolve_path(client.subscription)
                if resolved != client.path:
                    client.path = resolved
                    changed_clients.append(client)
                    self._enqueue(
                        client,
                        {
                            "type": "reset",
                            "protocol": PROTOCOL_VERSION,
                            "reason": "new_recording",
                        },
                    )

            keys = {
                client.key
                for client in self.clients.values()
                if client.key is not None
            }
            changed_identifiers = {
                client.identifier for client in changed_clients
            }
            for key in keys:
                delta = await self._read_key(key)
                if delta is None:
                    continue
                if delta.reset:
                    for client in self._clients_for_key(key):
                        if client.identifier in changed_identifiers:
                            continue
                        self._enqueue(
                            client,
                            {
                                "type": "reset",
                                "protocol": PROTOCOL_VERSION,
                                "reason": "recording_restarted",
                            },
                        )
                        self._enqueue(client, self._snapshot_message(client))
                    continue
                self._broadcast_delta(
                    key,
                    delta,
                    exclude=changed_identifiers,
                )

            for client in changed_clients:
                self._enqueue(client, self._snapshot_message(client))

            for client in self.clients.values():
                if client.path is not None and client.key in keys:
                    continue
                if client.queue.empty():
                    self._enqueue(client, self._snapshot_message(client))

    async def _watch_changes(self) -> None:
        while True:
            await self._changes.get()
            await asyncio.sleep(0.03)
            while not self._changes.empty():
                self._changes.get_nowait()
            try:
                await self.process_changes()
            except Exception as exc:
                async with self._lock:
                    for client in self.clients.values():
                        self._enqueue(
                            client,
                            {
                                "type": "error",
                                "protocol": PROTOCOL_VERSION,
                                "message": (
                                    "Could not process the recording update: "
                                    f"{exc}"
                                ),
                            },
                        )

    async def _send_heartbeats(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            async with self._lock:
                for client in self.clients.values():
                    self._enqueue(
                        client,
                        {
                            "type": "heartbeat",
                            "protocol": PROTOCOL_VERSION,
                            "server_time_ms": round(time.time() * 1000),
                            "summary": self._summary_for(client),
                        },
                    )

    async def _read_key(
        self,
        key: tuple[Path, str | None],
    ) -> LiveTailDelta | None:
        tail = self.tails.get(key)
        if tail is None:
            tail = LiveRecordingTail(key[0], key[1])
            self.tails[key] = tail
        delta = await asyncio.to_thread(tail.read_delta)
        if (
            not delta.reset
            and not delta.records
            and not delta.events
        ):
            return None
        return delta

    def _broadcast_delta(
        self,
        key: tuple[Path, str | None],
        delta: LiveTailDelta,
        *,
        exclude: set[int] | None = None,
    ) -> None:
        tail = self.tails[key]
        message = {
            "type": "delta",
            "protocol": PROTOCOL_VERSION,
            "revision": tail.revision,
            "server_time_ms": round(time.time() * 1000),
            "samples": [
                serialize_chart_record(record) for record in delta.records
            ],
            "events": [
                serialize_timed_event(event) for event in delta.events
            ],
            "summary": self._summary(tail, key[0]),
        }
        for client in self._clients_for_key(key):
            if exclude is not None and client.identifier in exclude:
                continue
            self._enqueue(client, message)

    def _snapshot_message(self, client: DashboardClient) -> dict[str, Any]:
        if client.key is None:
            return {
                "type": "snapshot",
                "protocol": PROTOCOL_VERSION,
                "revision": 0,
                "server_time_ms": round(time.time() * 1000),
                "source": None,
                "samples": [],
                "events": [],
                "summary": self._empty_summary(),
            }
        tail = self.tails[client.key]
        return {
            "type": "snapshot",
            "protocol": PROTOCOL_VERSION,
            "revision": tail.revision,
            "server_time_ms": round(time.time() * 1000),
            "source": str(client.path),
            "samples": [
                serialize_chart_record(record)
                for record in tail.records_since(
                    float(client.subscription.history_seconds)
                )
            ],
            "events": [
                serialize_timed_event(event)
                for event in tail.events_since(
                    float(client.subscription.history_seconds)
                )
            ],
            "summary": self._summary(tail, client.path),
        }

    def _summary_for(self, client: DashboardClient) -> dict[str, Any]:
        if client.key is None or client.key not in self.tails:
            return self._empty_summary()
        return self._summary(self.tails[client.key], client.path)

    def _summary(
        self,
        tail: LiveRecordingTail,
        path: Path,
    ) -> dict[str, Any]:
        current = tail.records[-1] if tail.records else {}
        try:
            file_age_s = max(0.0, time.time() - path.stat().st_mtime)
        except FileNotFoundError:
            file_age_s = None
        receiving = file_age_s is not None and file_age_s <= 1.5
        return {
            "status": "receiving" if receiving else "waiting",
            "source_name": path.name,
            "exercise": tail.exercise,
            "algorithm": ALGORITHM_VERSION,
            "state": current.get("state_after", "waiting"),
            "accepted_reps": tail.accepted_reps,
            "rejected_candidates": tail.rejected_candidates,
            "sample_count": tail.sample_count,
            "sample_rate_hz": current.get("estimated_sample_rate_hz", 47.6),
            "rate_confidence": current.get("rate_confidence", "fallback"),
            "missing_samples": tail.tracker.total_missing_samples,
            "file_age_s": file_age_s,
        }

    @staticmethod
    def _empty_summary() -> dict[str, Any]:
        return {
            "status": "waiting",
            "source_name": None,
            "exercise": "generic",
            "algorithm": ALGORITHM_VERSION,
            "state": "waiting",
            "accepted_reps": 0,
            "rejected_candidates": 0,
            "sample_count": 0,
            "sample_rate_hz": 47.6,
            "rate_confidence": "fallback",
            "missing_samples": 0,
            "file_age_s": None,
        }

    def _resolve_path(
        self,
        subscription: DashboardSubscription,
    ) -> Path | None:
        if subscription.mode == "file":
            return subscription.recording
        candidates: list[tuple[int, str, Path]] = []
        for path in self.recordings_directory.glob("*.jsonl"):
            try:
                candidates.append(
                    (path.stat().st_mtime_ns, path.name, path.resolve())
                )
            except FileNotFoundError:
                continue
        if not candidates:
            return None
        return max(candidates)[2]

    def _clients_for_key(
        self,
        key: tuple[Path, str | None],
    ) -> list[DashboardClient]:
        return [
            client for client in self.clients.values() if client.key == key
        ]

    def _enqueue(
        self,
        client: DashboardClient,
        message: dict[str, Any],
    ) -> None:
        try:
            client.queue.put_nowait(message)
        except asyncio.QueueFull:
            while not client.queue.empty():
                client.queue.get_nowait()
            client.queue.put_nowait(
                {
                    "type": "reset",
                    "protocol": PROTOCOL_VERSION,
                    "reason": "client_backpressure",
                }
            )
            client.queue.put_nowait(self._snapshot_message(client))


def serialize_chart_record(record: dict) -> dict[str, Any]:
    return {
        field: record.get(field)
        for field in CHART_RECORD_FIELDS
    }


def serialize_timed_event(timed: LiveTimedEvent) -> dict[str, Any]:
    event = timed.event
    return {
        "sensor_time_s": timed.sensor_time_s,
        "kind": event.kind,
        "reason": event.reason,
        "metrics": event.metrics or {},
        "quality": event.quality or {},
        "trace": [
            {
                "elapsed_s": point.elapsed_s,
                "velocity_m_s": point.velocity_m_s,
                "displacement_m": point.displacement_m,
            }
            for point in (event.trace or [])
        ],
    }
