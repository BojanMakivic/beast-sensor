import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_DIRECTORY = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_DIRECTORY / "beast sensor.py"


def _load_cli_module():
    specification = importlib.util.spec_from_file_location(
        "beast_sensor_cli",
        SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class CliDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cli = _load_cli_module()

    def test_plain_live_run_records_without_diagnostic_output(self):
        with patch.object(sys, "argv", ["beast sensor.py"]):
            arguments = self.cli.parse_arguments()
        self.assertEqual(arguments.mode, "live")
        self.assertFalse(arguments.diagnostic)
        self.assertIsNotNone(arguments.record)
        self.assertEqual(arguments.record.parent, self.cli.RECORDINGS_DIRECTORY)

    def test_no_record_keeps_live_recording_disabled(self):
        with patch.object(
            sys,
            "argv",
            ["beast sensor.py", "--no-record"],
        ):
            arguments = self.cli.parse_arguments()
        self.assertEqual(arguments.mode, "live")
        self.assertIsNone(arguments.record)

    def test_replay_does_not_create_a_default_recording(self):
        recording = Path("session.jsonl")
        with patch.object(
            sys,
            "argv",
            ["beast sensor.py", "replay", str(recording)],
        ):
            arguments = self.cli.parse_arguments()
        self.assertEqual(arguments.mode, "replay")
        self.assertIsNone(arguments.record)

    def test_dashboard_can_follow_the_newest_recording(self):
        with patch.object(
            sys,
            "argv",
            [
                "beast sensor.py",
                "dashboard",
                "--exercise",
                "bench",
                "--port",
                "8502",
                "--refresh-ms",
                "500",
            ],
        ):
            arguments = self.cli.parse_arguments()
        self.assertEqual(arguments.mode, "dashboard")
        self.assertIsNone(arguments.recording)
        self.assertIsNone(arguments.record)
        self.assertEqual(arguments.exercise, "bench")
        self.assertEqual(arguments.port, 8502)
        self.assertEqual(arguments.refresh_ms, 500)


if __name__ == "__main__":
    unittest.main()
