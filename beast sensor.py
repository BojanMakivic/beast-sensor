import argparse
import asyncio
import json
import queue
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table, TableStyleInfo

from beast_motion import (
    ALGORITHM_VERSION,
    EXERCISE_PROFILES,
    MotionEvent,
    ReversalRepTracker,
    SessionRecorder,
    decode_imu_packet,
    recording_metadata,
    replay_items,
    tracker_config_for,
)


DEVICE_ADDRESS = "BE:A5:7F:30:78:68"
IMU_CHARACTERISTIC_UUID = "bea5760d-503d-4920-b000-101e7306b003"

PROJECT_DIRECTORY = Path(__file__).resolve().parent
REPETITION_DATA_FILE = PROJECT_DIRECTORY / "beast_repetitions.json"
EXCEL_WORKBOOK = PROJECT_DIRECTORY / "outputs" / "beast_tracker" / "Beast Workout.xlsx"
RECORDINGS_DIRECTORY = PROJECT_DIRECTORY / "outputs" / "recordings"


def load_repetitions() -> list[dict]:
    if not REPETITION_DATA_FILE.exists():
        return []
    try:
        data = json.loads(REPETITION_DATA_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def store_repetition(repetition: dict) -> None:
    repetitions = load_repetitions()
    repetitions.append(repetition)
    temporary_file = REPETITION_DATA_FILE.with_suffix(".json.tmp")
    temporary_file.write_text(json.dumps(repetitions, indent=2), encoding="utf-8")
    temporary_file.replace(REPETITION_DATA_FILE)


def build_excel_workbook() -> None:
    """Rebuild the portable Excel training log from accepted repetitions."""
    repetitions = load_repetitions()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Repetitions"
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "A8"

    navy = "172554"
    blue = "2563EB"
    pale_blue = "DBEAFE"
    very_pale_blue = "EFF6FF"
    white = "FFFFFF"
    dark_blue = "1E3A8A"
    light_border = Side(style="thin", color="DCE6F1")

    sheet.merge_cells("A1:H1")
    title = sheet["A1"]
    title.value = "Beast Bar-Velocity Training Log"
    title.fill = PatternFill("solid", fgColor=navy)
    title.font = Font(bold=True, color=white, size=18)
    title.alignment = Alignment(vertical="center")
    sheet.row_dimensions[1].height = 32

    sheet["A3"] = "Completed repetitions"
    sheet["A4"] = "Average speed"
    sheet["A5"] = "Best peak speed"
    for row in range(3, 6):
        sheet[f"A{row}"].fill = PatternFill("solid", fgColor=pale_blue)
        sheet[f"A{row}"].font = Font(bold=True, color=dark_blue)
        sheet[f"B{row}"].fill = PatternFill("solid", fgColor=very_pale_blue)
        sheet[f"B{row}"].font = Font(bold=True, color=navy)

    sheet["B3"] = len(repetitions)
    headers = [
        "Repetition",
        "Completed at",
        "Time (s)",
        "Displacement (m)",
        "Average speed (m/s)",
        "Peak speed (m/s)",
        "Algorithm",
        "Quality",
    ]
    for column, header in enumerate(headers, 1):
        cell = sheet.cell(row=7, column=column, value=header)
        cell.fill = PatternFill("solid", fgColor=blue)
        cell.font = Font(bold=True, color=white)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row_number, repetition in enumerate(repetitions, 8):
        completed_at = datetime.fromisoformat(repetition["completed_at"])
        if completed_at.tzinfo is not None:
            completed_at = completed_at.replace(tzinfo=None)
        quality = repetition.get("signal_quality", {})
        quality_status = str(
            quality.get("quality_status", "legacy")
        ).replace("_", " ")
        values = [
            repetition["rep"],
            completed_at,
            repetition["duration_s"],
            repetition["displacement_m"],
            repetition["average_speed_m_s"],
            repetition["peak_speed_m_s"],
            repetition.get("algorithm_version", "legacy"),
            quality_status,
        ]
        for column, value in enumerate(values, 1):
            cell = sheet.cell(row=row_number, column=column, value=value)
            cell.border = Border(bottom=light_border)
        sheet.cell(row=row_number, column=1).number_format = "0"
        sheet.cell(row=row_number, column=2).number_format = "yyyy-mm-dd hh:mm:ss"
        for column in range(3, 7):
            sheet.cell(row=row_number, column=column).number_format = "0.000"

    if repetitions:
        last_row = 7 + len(repetitions)
        sheet["B4"] = f"=IFERROR(AVERAGE(E8:E{last_row}),0)"
        sheet["B5"] = f"=IFERROR(MAX(F8:F{last_row}),0)"
        table = Table(displayName="RepetitionsTable", ref=f"A7:H{last_row}")
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        sheet.add_table(table)
    else:
        sheet["B4"] = 0
        sheet["B5"] = 0

    sheet["B4"].number_format = '0.000 "m/s"'
    sheet["B5"].number_format = '0.000 "m/s"'
    widths = {
        "A": 22,
        "B": 22,
        "C": 16,
        "D": 20,
        "E": 22,
        "F": 20,
        "G": 24,
        "H": 30,
    }
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    for row in range(3, max(8, 8 + len(repetitions))):
        sheet.row_dimensions[row].height = 20

    EXCEL_WORKBOOK.parent.mkdir(parents=True, exist_ok=True)
    temporary_workbook = EXCEL_WORKBOOK.with_name("Beast Workout.tmp.xlsx")
    workbook.save(temporary_workbook)
    temporary_workbook.replace(EXCEL_WORKBOOK)


class ExcelWorkbookUpdater:
    """Serialize background workbook rebuilds so repetitions cannot race."""

    def __init__(self) -> None:
        self.requests: queue.Queue[bool | None] = queue.Queue()
        self.worker = threading.Thread(target=self._run, daemon=False)
        self.worker.start()

    def request_update(self) -> None:
        self.requests.put(True)

    def close(self) -> None:
        self.requests.put(None)
        self.worker.join(timeout=45.0)

    def _run(self) -> None:
        while True:
            request = self.requests.get()
            if request is None:
                return
            stop_after_update = False
            while True:
                try:
                    next_request = self.requests.get_nowait()
                except queue.Empty:
                    break
                if next_request is None:
                    stop_after_update = True
                    break
            try:
                build_excel_workbook()
            except Exception as exc:
                print(f"Excel update failed: {exc}")
            if stop_after_update:
                return


class TrackerSession:
    def __init__(
        self,
        updater: ExcelWorkbookUpdater | None,
        recorder: SessionRecorder | None,
        diagnostic: bool,
        persist: bool,
        exercise: str = "generic",
    ) -> None:
        self.updater = updater
        self.recorder = recorder
        self.diagnostic = diagnostic
        self.persist = persist
        self.exercise = exercise
        self.config = tracker_config_for(exercise)
        self.tracker = ReversalRepTracker(self.config)
        self.accepted = 0
        self.rejected = 0
        self.rep_number = (
            max(
                (int(rep.get("rep", 0)) for rep in load_repetitions()),
                default=0,
            )
            if persist
            else 0
        )

    def reset_tracker(self, mark_recording: bool = True) -> None:
        self.tracker = ReversalRepTracker(self.config)
        if mark_recording and self.recorder is not None:
            self.recorder.mark_tracker_reset(time.perf_counter())

    def handle_packet(self, _sender, data: bytearray) -> None:
        sample = decode_imu_packet(data, time.perf_counter())
        if sample is None:
            if self.diagnostic:
                print(f"REJECTED PACKET: expected 16 bytes, received {len(data)}")
            return
        events, record = self.tracker.process(sample)
        if self.recorder is not None:
            self.recorder.write(record)
        self.handle_events(events)

    def handle_events(self, events: list[MotionEvent]) -> None:
        for event in events:
            if event.kind == "ready":
                quality = event.quality or {}
                print(
                    "Ready | "
                    f"gravity {quality.get('gravity_baseline_g', 0.0):.3f} g | "
                    f"noise {quality.get('vertical_noise_m_s2', 0.0):.3f} m/s^2 | "
                    f"start {quality.get('start_threshold_m_s2', 0.0):.3f} m/s^2 | "
                    f"{quality.get('sample_rate_hz', 0.0):.1f} Hz"
                )
            elif event.kind == "rep" and event.metrics is not None:
                self.accepted += 1
                self.rep_number += 1
                repetition = {
                    "rep": self.rep_number,
                    "completed_at": datetime.now().astimezone().isoformat(
                        timespec="seconds"
                    ),
                    "algorithm_version": ALGORITHM_VERSION,
                    "signal_quality": event.quality or {},
                    **event.metrics,
                }
                if self.persist:
                    store_repetition(repetition)
                    if self.updater is not None:
                        self.updater.request_update()
                prefix = "REP" if self.persist else "REPLAY REP"
                top_detection = (event.quality or {}).get(
                    "top_detection",
                    "velocity",
                )
                recovered_suffix = (
                    " | recovered top"
                    if top_detection == "rest_orientation_fallback"
                    else ""
                )
                print(
                    f"{prefix} {self.rep_number:02d} | "
                    f"time {event.metrics['duration_s']:.2f} s | "
                    f"displacement {event.metrics['displacement_m']:.3f} m | "
                    f"average speed {event.metrics['average_speed_m_s']:.3f} m/s | "
                    f"peak speed {event.metrics['peak_speed_m_s']:.3f} m/s"
                    f"{recovered_suffix}"
                )
            elif event.kind == "rejected":
                self.rejected += 1
                if self.diagnostic:
                    print(f"REJECTED: {event.reason}")
            elif self.diagnostic and event.kind in {
                "up_started",
                "top",
                "down_started",
                "bottom",
                "gap",
                "duplicate",
                "rest",
            }:
                event_time_s = float(
                    (event.quality or {}).get(
                        "phase_ended_s",
                        self.tracker.sensor_time_s,
                    )
                )
                print(
                    f"{event.kind.upper()} at {event_time_s:.2f} s: "
                    f"{event.reason or ''}"
                )


async def scan() -> None:
    devices = await BleakScanner.discover(timeout=15.0)
    for device in devices:
        print(f"Name: {device.name or 'Unknown'} | Address: {device.address}")


async def stream_sensor(session: TrackerSession) -> None:
    disconnected = asyncio.Event()

    def disconnected_callback(_client: BleakClient) -> None:
        disconnected.set()

    async with BleakClient(
        DEVICE_ADDRESS,
        disconnected_callback=disconnected_callback,
    ) as client:
        characteristic = client.services.get_characteristic(IMU_CHARACTERISTIC_UUID)
        if not characteristic or "notify" not in characteristic.properties:
            raise RuntimeError("The Beast IMU notification characteristic is unavailable.")
        session.reset_tracker()
        print(f"Connected to Beast. Excel: {EXCEL_WORKBOOK}")
        print("Keep the sensor still until calibration reports Ready.\n")
        await client.start_notify(IMU_CHARACTERISTIC_UUID, session.handle_packet)
        await disconnected.wait()


def default_recording_path() -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    return RECORDINGS_DIRECTORY / f"beast-{stamp}.jsonl"


async def run_live(args: argparse.Namespace) -> None:
    exercise = args.exercise or "generic"
    updater = ExcelWorkbookUpdater()
    if not EXCEL_WORKBOOK.exists():
        updater.request_update()
    recorder = (
        SessionRecorder(args.record, exercise)
        if args.record is not None
        else None
    )
    if recorder is not None:
        print(f"Raw recording: {recorder.path}")
    session = TrackerSession(
        updater,
        recorder,
        args.diagnostic,
        persist=True,
        exercise=exercise,
    )
    try:
        while True:
            try:
                await stream_sensor(session)
                print("Beast disconnected. Reconnecting...")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"Connection error: {exc}. Retrying...")
            await asyncio.sleep(2.0)
    finally:
        if recorder is not None:
            recorder.close()
        updater.close()


def run_replay(args: argparse.Namespace) -> None:
    try:
        metadata = recording_metadata(args.recording)
    except FileNotFoundError as exc:
        raise SystemExit(f"Recording not found: {args.recording}") from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"Could not read recording metadata '{args.recording}': {exc}"
        ) from exc
    exercise = (
        args.exercise
        or metadata.get("exercise_profile")
        or "generic"
    )
    if exercise not in EXERCISE_PROFILES:
        print(
            f"Unknown recorded exercise profile '{exercise}'; "
            "using generic."
        )
        exercise = "generic"
    session = TrackerSession(
        None,
        None,
        args.diagnostic,
        persist=False,
        exercise=exercise,
    )
    sample_count = 0
    try:
        for item in replay_items(args.recording):
            if item is None:
                session.reset_tracker(mark_recording=False)
                continue
            sample_count += 1
            events, _record = session.tracker.process(item)
            session.handle_events(events)
    except FileNotFoundError as exc:
        raise SystemExit(f"Recording not found: {args.recording}") from exc
    except OSError as exc:
        raise SystemExit(f"Could not open recording '{args.recording}': {exc}") from exc
    print(
        f"Replay complete | samples {sample_count} | "
        f"repetitions {session.accepted} | rejected {session.rejected} | "
        f"missing sensor samples {session.tracker.total_missing_samples}"
    )


def run_analyze(args: argparse.Namespace) -> None:
    from beast_analysis import analyze_recording

    try:
        result = analyze_recording(
            args.recording,
            exercise=args.exercise,
            expected_reps=args.expected_reps,
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"Recording not found: {args.recording}") from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"Could not analyze recording '{args.recording}': {exc}"
        ) from exc
    print(
        f"Analysis complete | profile {result.exercise} | "
        f"samples {result.sample_count} | repetitions {result.accepted_reps} | "
        f"rejected {result.rejected_candidates}"
    )
    if result.expected_reps is not None:
        status = "PASS" if result.accepted_reps == result.expected_reps else "FAIL"
        print(
            f"Expected repetitions {result.expected_reps} | "
            f"detected {result.accepted_reps} | {status}"
        )
    print(f"Interactive report: {result.report_path}")
    if args.open:
        webbrowser.open(result.report_path.resolve().as_uri())


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track and analyze Beast Sensor bar-velocity repetitions."
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("live", "replay", "analyze"),
        default="live",
    )
    parser.add_argument(
        "recording",
        nargs="?",
        type=Path,
        help="JSONL recording to replay or analyze.",
    )
    recording_options = parser.add_mutually_exclusive_group()
    recording_options.add_argument(
        "--record",
        nargs="?",
        type=Path,
        const=default_recording_path(),
        help=(
            "Record live raw samples, optionally to a selected JSONL file. "
            "Live mode records automatically."
        ),
    )
    recording_options.add_argument(
        "--no-record",
        action="store_true",
        help="Run live tracking without saving raw sensor packets.",
    )
    parser.add_argument(
        "--exercise",
        choices=tuple(EXERCISE_PROFILES),
        help=(
            "Movement profile. A command-line value overrides recording "
            "metadata; otherwise generic is used."
        ),
    )
    parser.add_argument(
        "--expected-reps",
        type=int,
        help="Expected accepted repetitions for analyze mode.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated offline HTML report after analysis.",
    )
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="Print direction transitions, rejected motion, and true packet gaps.",
    )
    args = parser.parse_args()
    if args.mode in {"replay", "analyze"} and args.recording is None:
        parser.error(
            f"{args.mode} requires the actual path of a JSONL recording."
        )
    if args.mode != "live" and args.record is not None:
        parser.error("--record can only be used in live mode.")
    if args.mode != "live" and args.no_record:
        parser.error("--no-record can only be used in live mode.")
    if args.mode != "analyze" and args.expected_reps is not None:
        parser.error("--expected-reps can only be used in analyze mode.")
    if args.expected_reps is not None and args.expected_reps < 0:
        parser.error("--expected-reps must be zero or greater.")
    if args.mode != "analyze" and args.open:
        parser.error("--open can only be used in analyze mode.")
    if args.mode == "live" and args.record is None and not args.no_record:
        args.record = default_recording_path()
    return args


def main() -> None:
    args = parse_arguments()
    if args.mode == "replay":
        run_replay(args)
    elif args.mode == "analyze":
        run_analyze(args)
    else:
        asyncio.run(run_live(args))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user.")
