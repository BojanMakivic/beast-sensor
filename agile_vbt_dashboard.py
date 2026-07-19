"""Live local Streamlit dashboard for Agile VBT recordings."""

from __future__ import annotations

import argparse
from pathlib import Path

import streamlit as st

from agile_vbt_live_display import agile_vbt_live_display
from agile_vbt_motion import ALGORITHM_VERSION, EXERCISE_PROFILES


PROJECT_DIRECTORY = Path(__file__).resolve().parent
RECORDINGS_DIRECTORY = PROJECT_DIRECTORY / "outputs" / "recordings"


def parse_dashboard_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--recording", type=Path)
    parser.add_argument("--exercise", choices=tuple(EXERCISE_PROFILES))
    parser.add_argument("--refresh-ms", type=int)
    parser.add_argument("--history-seconds", type=int, default=90)
    arguments, _unknown = parser.parse_known_args()
    return arguments


arguments = parse_dashboard_arguments()
st.set_page_config(
    page_title="Agile VBT live movement",
    page_icon=":material/monitoring:",
    layout="wide",
)

available_recordings = sorted(
    RECORDINGS_DIRECTORY.glob("*.jsonl"),
    key=lambda path: (path.stat().st_mtime_ns, path.name),
    reverse=True,
)
explicit_recording = (
    arguments.recording.resolve()
    if arguments.recording is not None
    else None
)
if (
    explicit_recording is not None
    and explicit_recording not in available_recordings
):
    available_recordings.insert(0, explicit_recording)

with st.sidebar:
    st.header("Live controls")
    follow_newest = st.toggle(
        "Follow newest recording",
        value=arguments.recording is None,
        help="Switch automatically when the next sensor recording is created.",
    )
    selected_recording = explicit_recording
    if not follow_newest:
        if available_recordings:
            selected_recording = st.selectbox(
                "Recording",
                available_recordings,
                index=(
                    available_recordings.index(explicit_recording)
                    if explicit_recording in available_recordings
                    else 0
                ),
                format_func=lambda path: path.name,
            )
        else:
            selected_recording = None
            st.caption("No recordings are available yet.")

    exercise_options = ["Recording metadata", *EXERCISE_PROFILES]
    exercise_index = (
        exercise_options.index(arguments.exercise)
        if arguments.exercise in EXERCISE_PROFILES
        else 0
    )
    exercise_choice = st.selectbox(
        "Exercise profile",
        exercise_options,
        index=exercise_index,
    )
    exercise_override = (
        None
        if exercise_choice == "Recording metadata"
        else exercise_choice
    )
    history_seconds = st.slider(
        "Visible history (seconds)",
        min_value=15,
        max_value=300,
        value=min(300, max(15, arguments.history_seconds)),
        step=15,
    )
    paused = st.toggle("Pause display", value=False)
    st.caption("Live updates: WebSocket · approximately 200 ms")
    st.caption(f"Detector: {ALGORITHM_VERSION}")

agile_vbt_live_display(
    source_mode="latest" if follow_newest else "file",
    recording_path=None if follow_newest else selected_recording,
    exercise=exercise_override,
    history_seconds=history_seconds,
    paused=paused,
)
