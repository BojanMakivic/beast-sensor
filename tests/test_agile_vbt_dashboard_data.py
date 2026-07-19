import json
import math
import os
import struct
import tempfile
import unittest
from pathlib import Path

from agile_vbt_dashboard_data import LiveRecordingTail, latest_recording
from agile_vbt_motion import (
    GRAVITY_M_S2,
    ReversalRepTracker,
    SAMPLE_INTERVAL_S,
    decode_imu_packet,
    tracker_config_for,
)


DT = SAMPLE_INTERVAL_S


def _packet(sequence: int, acceleration_m_s2: float) -> bytes:
    vertical_g = 1.0 + acceleration_m_s2 / GRAVITY_M_S2
    return struct.pack(
        "<Hhhhhhhh",
        sequence & 0xFFFF,
        0,
        0,
        32767,
        0,
        0,
        0,
        round(vertical_g * 1000.0),
    )


def _phase(
    distance_m: float,
    duration_s: float,
    direction: float,
) -> list[float]:
    return [
        direction
        * distance_m
        * math.pi**2
        / (2.0 * duration_s**2)
        * math.cos(math.pi * index * DT / duration_s)
        for index in range(round(duration_s / DT) + 1)
    ]


def _sample_record(sequence: int, acceleration_m_s2: float) -> dict:
    return {
        "type": "sample",
        "host_timestamp": sequence * DT,
        "packet_hex": _packet(sequence, acceleration_m_s2).hex(),
        "velocity_m_s": 9999.0,
        "state_after": "poisoned-old-result",
    }


class LiveRecordingTailTests(unittest.TestCase):
    def test_incremental_tail_matches_current_detector_without_duplicates(self):
        values = (
            [0.0] * 120
            + _phase(0.35, 0.9, -1.0)
            + _phase(0.35, 0.8, 1.0)
            + [0.0] * 40
        )
        records = [
            {
                "type": "metadata",
                "exercise_profile": "bench",
            },
            *[
                _sample_record(sequence, value)
                for sequence, value in enumerate(values, 1)
            ],
        ]
        midpoint = len(records) // 2
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.jsonl"
            first_text = "".join(
                json.dumps(record) + "\n"
                for record in records[:midpoint]
            )
            path.write_text(first_text, encoding="utf-8")

            tail = LiveRecordingTail(path)
            first_samples = tail.read_new()
            self.assertGreater(first_samples, 0)
            first_total = tail.sample_count
            self.assertEqual(tail.read_new(), 0)
            self.assertEqual(tail.sample_count, first_total)

            with path.open("a", encoding="utf-8") as recording:
                recording.writelines(
                    json.dumps(record) + "\n"
                    for record in records[midpoint:]
                )
            second_samples = tail.read_new()
            self.assertGreater(second_samples, 0)
            self.assertEqual(tail.sample_count, len(values))
            self.assertEqual(tail.exercise, "bench")
            self.assertTrue(
                all(
                    record["velocity_m_s"] != 9999.0
                    for record in tail.records
                )
            )

            direct = ReversalRepTracker(tracker_config_for("bench"))
            direct_reps = 0
            for sequence, value in enumerate(values, 1):
                sample = decode_imu_packet(
                    _packet(sequence, value),
                    sequence * DT,
                )
                events, _record = direct.process(sample)
                direct_reps += sum(event.kind == "rep" for event in events)
            self.assertEqual(tail.accepted_reps, direct_reps)

    def test_partial_json_line_waits_until_the_next_refresh(self):
        metadata = json.dumps(
            {"type": "metadata", "exercise_profile": "generic"}
        )
        sample_line = json.dumps(_sample_record(1, 0.0)).encode("utf-8")
        split = len(sample_line) // 2
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.jsonl"
            path.write_bytes(
                (metadata + "\n").encode("utf-8") + sample_line[:split]
            )
            tail = LiveRecordingTail(path)
            self.assertEqual(tail.read_new(), 0)
            self.assertEqual(tail.sample_count, 0)

            with path.open("ab") as recording:
                recording.write(sample_line[split:] + b"\n")
            self.assertEqual(tail.read_new(), 1)
            self.assertEqual(tail.sample_count, 1)

    def test_latest_recording_uses_file_modification_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            older = root / "older.jsonl"
            newer = root / "newer.jsonl"
            older.write_text("{}\n", encoding="utf-8")
            newer.write_text("{}\n", encoding="utf-8")
            os.utime(older, (1.0, 1.0))
            os.utime(newer, (2.0, 2.0))
            self.assertEqual(latest_recording(root), newer)


if __name__ == "__main__":
    unittest.main()
