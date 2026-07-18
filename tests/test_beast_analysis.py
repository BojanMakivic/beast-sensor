import json
import math
import struct
import tempfile
import unittest
from pathlib import Path

from beast_analysis import analyze_recording
from beast_motion import (
    GRAVITY_M_S2,
    ReversalRepTracker,
    SessionRecorder,
    decode_imu_packet,
)


DT = 0.020


def _packet(sequence: int, acceleration_m_s2: float) -> bytes:
    vertical_g = 1.0 + acceleration_m_s2 / GRAVITY_M_S2
    return struct.pack(
        "<Hhhhhhhh",
        sequence,
        0,
        0,
        32767,
        0,
        0,
        0,
        round(vertical_g * 1000.0),
    )


def _phase(distance_m: float, duration_s: float, direction: float) -> list[float]:
    return [
        direction
        * distance_m
        * math.pi**2
        / (2.0 * duration_s**2)
        * math.cos(math.pi * index * DT / duration_s)
        for index in range(round(duration_s / DT) + 1)
    ]


class AnalysisReportTests(unittest.TestCase):
    def test_report_reprocesses_packets_and_writes_offline_html(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            recording = directory_path / "bench.jsonl"
            tracker = ReversalRepTracker()
            recorder = SessionRecorder(recording, exercise="bench")
            values = (
                [0.0] * 120
                + _phase(0.35, 0.9, -1.0)
                + _phase(0.35, 0.8, 1.0)
                + [0.0] * 40
            )
            for sequence, value in enumerate(values, 1):
                sample = decode_imu_packet(
                    _packet(sequence, value),
                    sequence * DT,
                )
                events, record = tracker.process(sample)
                record["velocity_m_s"] = 9999.0
                record["state_after"] = "poisoned-old-result"
                recorder.write(record)
            recorder.close()

            result = analyze_recording(
                recording,
                expected_reps=1,
                output_directory=directory_path / "analysis",
            )

            self.assertEqual(result.exercise, "bench")
            self.assertEqual(result.accepted_reps, 1)
            self.assertTrue(result.report_path.exists())
            html = result.report_path.read_text(encoding="utf-8")
            for expected_text in (
                "Beast Movement Analysis",
                "plotly.js",
                "Raw acceleration",
                "Filtered acceleration",
                "Drift-corrected velocity",
                "Rest confidence",
                "Orientation change",
                "Movement candidates",
                "Accepted rep",
                "State: up",
            ):
                self.assertIn(expected_text, html)
            self.assertNotIn("poisoned-old-result", html)
            self.assertNotIn("9999.0", html)

            plot_call = html.rindex("Plotly.newPlot(")
            data_start = html.index("[", plot_call)
            decoder = json.JSONDecoder()
            traces, consumed = decoder.raw_decode(html[data_start:])
            layout_start = data_start + consumed
            layout_start = html.index("{", layout_start)
            layout, _consumed = decoder.raw_decode(html[layout_start:])
            table = next(trace for trace in traces if trace["type"] == "table")
            self.assertEqual(
                table["header"]["values"],
                [
                    "Time (s)",
                    "Result",
                    "Reason",
                    "Duration (s)",
                    "Distance (m)",
                    "Mean v (m/s)",
                    "Peak v (m/s)",
                    "Drift (m/s)",
                    "Missing",
                ],
            )
            self.assertNotIn("rangeslider", layout["xaxis4"])
            self.assertGreaterEqual(layout["height"], 1445)
            self.assertGreaterEqual(layout["margin"]["t"], 220)
            self.assertEqual(layout["legend"]["xanchor"], "left")
            self.assertEqual(layout["legend"]["yanchor"], "bottom")
            self.assertGreater(layout["legend"]["y"], 1.0)


if __name__ == "__main__":
    unittest.main()
