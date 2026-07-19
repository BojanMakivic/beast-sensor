import asyncio
import json
import struct
import tempfile
import unittest
from pathlib import Path

from agile_vbt_dashboard_stream import DashboardBroker


def _sample_record(sequence: int) -> dict:
    packet = struct.pack(
        "<Hhhhhhhh",
        sequence,
        0,
        0,
        32767,
        0,
        0,
        0,
        1000,
    )
    return {
        "type": "sample",
        "host_timestamp": sequence / 47.6,
        "packet_hex": packet.hex(),
    }


def _write_records(path: Path, records: list[dict], mode: str = "w") -> None:
    with path.open(mode, encoding="utf-8") as recording:
        for record in records:
            recording.write(json.dumps(record) + "\n")


def _subscription(
    *,
    mode: str = "latest",
    path: Path | None = None,
    exercise: str | None = "bench",
) -> dict:
    return {
        "type": "subscribe",
        "source": {
            "mode": mode,
            "path": None if path is None else str(path),
        },
        "exercise": exercise,
        "history_seconds": 90,
    }


class DashboardBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_and_stop_close_the_filesystem_observer(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = DashboardBroker(Path(directory))
            await broker.start()
            observer = broker._observer
            self.assertIsNotNone(observer)
            self.assertTrue(observer.is_alive())

            await broker.stop()

            self.assertFalse(observer.is_alive())
            self.assertEqual(broker.clients, {})
            self.assertEqual(broker.tails, {})

    async def test_one_tail_feeds_multiple_clients_with_deltas(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recording = root / "live.jsonl"
            _write_records(
                recording,
                [
                    {"type": "metadata", "exercise_profile": "bench"},
                    _sample_record(1),
                ],
            )
            broker = DashboardBroker(root)
            first = await broker.register(_subscription())
            second = await broker.register(_subscription())
            first_snapshot = first.queue.get_nowait()
            second_snapshot = second.queue.get_nowait()
            self.assertEqual(first_snapshot["type"], "snapshot")
            self.assertEqual(second_snapshot["type"], "snapshot")
            self.assertEqual(len(broker.tails), 1)
            self.assertEqual(len(first_snapshot["samples"]), 1)

            _write_records(recording, [_sample_record(2)], mode="a")
            await broker.process_changes()
            first_delta = first.queue.get_nowait()
            second_delta = second.queue.get_nowait()
            self.assertEqual(first_delta["type"], "delta")
            self.assertEqual(second_delta["type"], "delta")
            self.assertEqual(
                first_delta["samples"],
                second_delta["samples"],
            )
            self.assertEqual(len(first_delta["samples"]), 1)

    async def test_polling_recovers_when_no_filesystem_event_arrives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recording = root / "live.jsonl"
            _write_records(recording, [_sample_record(1)])
            broker = DashboardBroker(root)
            await broker.start()
            try:
                client = await broker.register(_subscription())
                client.queue.get_nowait()
                _write_records(recording, [_sample_record(2)], mode="a")

                delta = None
                for _attempt in range(10):
                    await asyncio.sleep(0.1)
                    while not client.queue.empty():
                        message = client.queue.get_nowait()
                        if message["type"] == "delta":
                            delta = message
                            break
                    if delta is not None:
                        break

                self.assertIsNotNone(delta)
                self.assertEqual(len(delta["samples"]), 1)
            finally:
                await broker.stop()

    async def test_latest_subscription_resets_once_for_a_new_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_path = root / "first.jsonl"
            _write_records(first_path, [_sample_record(1)])
            broker = DashboardBroker(root)
            client = await broker.register(_subscription())
            client.queue.get_nowait()

            await asyncio.sleep(0.01)
            second_path = root / "second.jsonl"
            _write_records(second_path, [_sample_record(1)])
            await broker.process_changes()
            messages = []
            while not client.queue.empty():
                messages.append(client.queue.get_nowait())
            self.assertEqual(
                [message["type"] for message in messages],
                ["reset", "snapshot"],
            )
            self.assertEqual(
                messages[-1]["summary"]["source_name"],
                second_path.name,
            )

    async def test_subscription_rejects_paths_outside_allowed_recordings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = DashboardBroker(root / "recordings")
            outside = root / "outside.jsonl"
            with self.assertRaisesRegex(ValueError, "outside"):
                await broker.register(
                    _subscription(mode="file", path=outside)
                )

    async def test_partial_line_is_not_published_until_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recording = root / "partial.jsonl"
            line = json.dumps(_sample_record(1)).encode("utf-8")
            recording.write_bytes(line[: len(line) // 2])
            broker = DashboardBroker(root)
            client = await broker.register(_subscription())
            snapshot = client.queue.get_nowait()
            self.assertEqual(snapshot["samples"], [])

            with recording.open("ab") as output:
                output.write(line[len(line) // 2 :] + b"\n")
            await broker.process_changes()
            delta = client.queue.get_nowait()
            self.assertEqual(delta["type"], "delta")
            self.assertEqual(len(delta["samples"]), 1)

    async def test_malformed_line_is_skipped_without_losing_next_packet(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recording = root / "malformed.jsonl"
            _write_records(recording, [_sample_record(1)])
            broker = DashboardBroker(root)
            client = await broker.register(_subscription())
            client.queue.get_nowait()

            with recording.open("a", encoding="utf-8") as output:
                output.write("{not valid json}\n")
                output.write(json.dumps(_sample_record(2)) + "\n")
            await broker.process_changes()

            delta = client.queue.get_nowait()
            self.assertEqual(delta["type"], "delta")
            self.assertEqual(len(delta["samples"]), 1)

    async def test_truncated_recording_resets_then_sends_one_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recording = root / "truncated.jsonl"
            _write_records(recording, [_sample_record(1), _sample_record(2)])
            broker = DashboardBroker(root)
            client = await broker.register(_subscription())
            client.queue.get_nowait()

            recording.write_text("", encoding="utf-8")
            await broker.process_changes()

            messages = []
            while not client.queue.empty():
                messages.append(client.queue.get_nowait())
            self.assertEqual(
                [message["type"] for message in messages],
                ["reset", "snapshot"],
            )
            self.assertEqual(messages[-1]["samples"], [])


if __name__ == "__main__":
    unittest.main()
