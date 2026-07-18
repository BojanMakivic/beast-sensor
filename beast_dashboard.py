"""Live local Streamlit dashboard for Beast Sensor recordings."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from beast_dashboard_data import LiveRecordingTail, latest_recording
from beast_motion import ALGORITHM_VERSION, EXERCISE_PROFILES


PROJECT_DIRECTORY = Path(__file__).resolve().parent
RECORDINGS_DIRECTORY = PROJECT_DIRECTORY / "outputs" / "recordings"

COLORS = {
    "raw": "#94A3B8",
    "filtered": "#2563EB",
    "threshold": "#DC2626",
    "baseline": "#7C3AED",
    "velocity": "#64748B",
    "corrected": "#059669",
    "rest": "#0F766E",
    "orientation": "#D97706",
    "rate": "#4F46E5",
    "rep": "#16A34A",
    "rejected": "#DC2626",
    "gap": "#111827",
}

STATE_COLORS = {
    "calibrating": "#E2E8F0",
    "rest": "#CCFBF1",
    "up": "#DBEAFE",
    "down": "#FEF3C7",
    "recovery": "#FEE2E2",
}


def parse_dashboard_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--recording", type=Path)
    parser.add_argument("--exercise", choices=tuple(EXERCISE_PROFILES))
    parser.add_argument("--refresh-ms", type=int, default=750)
    parser.add_argument("--history-seconds", type=int, default=90)
    arguments, _unknown = parser.parse_known_args()
    return arguments


@st.cache_resource(show_spinner=False)
def recording_tail(
    path_text: str,
    exercise: str | None,
) -> LiveRecordingTail:
    return LiveRecordingTail(Path(path_text), exercise)


def _state_regions(records: list[dict]) -> list[tuple[str, float, float]]:
    if not records:
        return []
    regions: list[tuple[str, float, float]] = []
    current = str(records[0]["state_after"])
    started = float(records[0]["sensor_time_s"])
    for record in records[1:]:
        state = str(record["state_after"])
        time_s = float(record["sensor_time_s"])
        if state == current:
            continue
        regions.append((current, started, time_s))
        current = state
        started = time_s
    regions.append((current, started, float(records[-1]["sensor_time_s"])))
    return regions


def _orientation_regions(
    records: list[dict],
) -> list[tuple[float, float, bool, int]]:
    regions: list[tuple[float, float, bool, int]] = []
    started: float | None = None
    region_id = 0
    for record in records:
        time_s = float(record["sensor_time_s"])
        if record.get("orientation_region_started"):
            started = time_s
            region_id = int(record.get("orientation_region_id", 0))
        if record.get("orientation_region_ended") and started is not None:
            regions.append(
                (
                    started,
                    time_s,
                    bool(record.get("orientation_region_confirmed")),
                    region_id,
                )
            )
            started = None
    if started is not None and records:
        regions.append(
            (
                started,
                float(records[-1]["sensor_time_s"]),
                False,
                region_id,
            )
        )
    return regions


def _nearest_record_value(
    records: list[dict],
    sensor_time_s: float,
    field: str,
) -> float:
    if not records:
        return 0.0
    record = min(
        records,
        key=lambda item: abs(
            float(item["sensor_time_s"]) - sensor_time_s
        ),
    )
    return float(record.get(field, 0.0))


def build_live_figure(records: list[dict], events: list) -> go.Figure:
    times = [float(record["sensor_time_s"]) for record in records]
    figure = make_subplots(
        rows=5,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=[0.28, 0.23, 0.17, 0.21, 0.11],
        specs=[
            [{"secondary_y": True}],
            [{}],
            [{}],
            [{"secondary_y": True}],
            [{}],
        ],
        subplot_titles=(
            "World-up acceleration",
            "Velocity",
            "Upward displacement",
            "Rest and adaptive orientation",
            "Adaptive sample clock",
        ),
    )

    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[
                float(record["raw_vertical_acceleration_m_s2"])
                for record in records
            ],
            name="Raw acceleration",
            mode="lines",
            line={"color": COLORS["raw"], "width": 1},
        ),
        row=1,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[
                float(record["filtered_acceleration_m_s2"])
                for record in records
            ],
            name="Filtered acceleration",
            mode="lines",
            line={"color": COLORS["filtered"], "width": 1.5},
        ),
        row=1,
        col=1,
        secondary_y=False,
    )
    threshold = [
        float(record["start_threshold_m_s2"]) for record in records
    ]
    for sign, name in ((1.0, "Movement threshold"), (-1.0, None)):
        figure.add_trace(
            go.Scattergl(
                x=times,
                y=[sign * value for value in threshold],
                name=name,
                showlegend=name is not None,
                mode="lines",
                line={
                    "color": COLORS["threshold"],
                    "width": 1,
                    "dash": "dot",
                },
                hoverinfo="skip",
            ),
            row=1,
            col=1,
            secondary_y=False,
        )
    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[
                record["gravity_baseline_g"]
                if record["gravity_baseline_g"] is not None
                else None
                for record in records
            ],
            name="Gravity baseline",
            mode="lines",
            line={"color": COLORS["baseline"], "width": 1.1},
        ),
        row=1,
        col=1,
        secondary_y=True,
    )

    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[float(record["velocity_m_s"]) for record in records],
            name="Provisional velocity",
            mode="lines",
            line={"color": COLORS["velocity"], "width": 1.2},
        ),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[float(record["displacement_m"]) for record in records],
            name="Provisional displacement",
            mode="lines",
            line={"color": COLORS["velocity"], "width": 1.1},
        ),
        row=3,
        col=1,
    )

    corrected_legend = False
    for timed in events:
        event = timed.event
        if event.kind != "rep" or not event.trace:
            continue
        quality = event.quality or {}
        phase_started = float(
            quality.get(
                "phase_started_s",
                timed.sensor_time_s - event.trace[-1].elapsed_s,
            )
        )
        corrected_times = [
            phase_started + point.elapsed_s for point in event.trace
        ]
        color = (
            COLORS["orientation"]
            if quality.get("top_detection") != "velocity"
            else COLORS["corrected"]
        )
        figure.add_trace(
            go.Scattergl(
                x=corrected_times,
                y=[point.velocity_m_s for point in event.trace],
                name="Corrected velocity",
                showlegend=not corrected_legend,
                legendgroup="corrected",
                mode="lines",
                line={"color": color, "width": 2.2},
            ),
            row=2,
            col=1,
        )
        figure.add_trace(
            go.Scattergl(
                x=corrected_times,
                y=[point.displacement_m for point in event.trace],
                name="Corrected displacement",
                showlegend=False,
                legendgroup="corrected",
                mode="lines",
                line={"color": color, "width": 2.2},
            ),
            row=3,
            col=1,
        )
        corrected_legend = True

    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[float(record["rest_confidence"]) for record in records],
            name="Rest confidence",
            mode="lines",
            fill="tozeroy",
            fillcolor="rgba(15,118,110,0.10)",
            line={"color": COLORS["rest"], "width": 1.3},
        ),
        row=4,
        col=1,
        secondary_y=False,
    )
    for field, name, color, dash in (
        (
            "orientation_change_deg",
            "Orientation change",
            COLORS["orientation"],
            "solid",
        ),
        (
            "orientation_baseline_lower_deg",
            "Orientation baseline lower",
            "#A16207",
            "dot",
        ),
        (
            "orientation_baseline_upper_deg",
            "Orientation baseline upper",
            "#A16207",
            "dot",
        ),
        (
            "orientation_start_threshold_deg",
            "Orientation start threshold",
            COLORS["threshold"],
            "dash",
        ),
    ):
        figure.add_trace(
            go.Scattergl(
                x=times,
                y=[float(record.get(field, 0.0)) for record in records],
                name=name,
                mode="lines",
                line={"color": color, "width": 1.1, "dash": dash},
            ),
            row=4,
            col=1,
            secondary_y=True,
        )

    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[
                float(record.get("estimated_sample_rate_hz", 47.6))
                for record in records
            ],
            name="Estimated sample rate",
            mode="lines",
            line={"color": COLORS["rate"], "width": 1.5},
            customdata=[
                str(record.get("rate_confidence", "fallback"))
                for record in records
            ],
            hovertemplate=(
                "%{x:.2f} s<br>%{y:.3f} Hz"
                "<br>Confidence: %{customdata}<extra></extra>"
            ),
        ),
        row=5,
        col=1,
    )

    shapes: list[dict] = []
    state_axes = (
        ("x", "y domain"),
        ("x2", "y3 domain"),
        ("x3", "y4 domain"),
        ("x4", "y5 domain"),
        ("x5", "y7 domain"),
    )
    for state, start, end in _state_regions(records):
        for xref, yref in state_axes:
            shapes.append(
                {
                    "type": "rect",
                    "x0": start,
                    "x1": end,
                    "y0": 0,
                    "y1": 1,
                    "xref": xref,
                    "yref": yref,
                    "fillcolor": STATE_COLORS.get(state, "#F8FAFC"),
                    "opacity": 0.13,
                    "line": {"width": 0},
                    "layer": "below",
                }
            )
    for start, end, confirmed, _region_id in _orientation_regions(records):
        shapes.append(
            {
                "type": "rect",
                "x0": start,
                "x1": end,
                "y0": 0,
                "y1": 1,
                "xref": "x4",
                "yref": "y5 domain",
                "fillcolor": (
                    "rgba(217,119,6,0.18)"
                    if confirmed
                    else "rgba(148,163,184,0.10)"
                ),
                "line": {
                    "width": 1,
                    "color": "rgba(217,119,6,0.35)",
                },
                "layer": "below",
            }
        )

    marker_config = {
        "rep": ("Accepted rep", COLORS["rep"], "star"),
        "rejected": ("Rejected", COLORS["rejected"], "x"),
        "gap": ("Packet gap", COLORS["gap"], "line-ns"),
        "top": ("Top", "#9333EA", "triangle-down"),
        "bottom": ("Bottom", "#92400E", "triangle-up"),
    }
    for kind, (name, color, symbol) in marker_config.items():
        selected = [timed for timed in events if timed.event.kind == kind]
        if not selected:
            continue
        figure.add_trace(
            go.Scatter(
                x=[timed.sensor_time_s for timed in selected],
                y=[
                    _nearest_record_value(
                        records,
                        timed.sensor_time_s,
                        "filtered_acceleration_m_s2",
                    )
                    for timed in selected
                ],
                name=name,
                mode="markers",
                marker={
                    "color": color,
                    "symbol": symbol,
                    "size": 9,
                    "line": {"color": "#FFFFFF", "width": 1},
                },
                hovertext=[
                    timed.event.reason or name for timed in selected
                ],
                hoverinfo="x+text",
            ),
            row=1,
            col=1,
            secondary_y=False,
        )

    figure.update_layout(
        height=1050,
        template="plotly_white",
        hovermode="x unified",
        margin={"l": 55, "r": 55, "t": 105, "b": 35},
        legend={
            "orientation": "h",
            "y": 1.04,
            "x": 0.0,
            "font": {"size": 10},
        },
        uirevision="beast-live-dashboard",
        shapes=shapes,
    )
    figure.update_yaxes(
        title_text="m/s²",
        row=1,
        col=1,
        secondary_y=False,
    )
    figure.update_yaxes(
        title_text="g",
        row=1,
        col=1,
        secondary_y=True,
    )
    figure.update_yaxes(title_text="m/s", row=2, col=1)
    figure.update_yaxes(title_text="m", row=3, col=1)
    figure.update_yaxes(
        title_text="Rest",
        range=[0.0, 1.05],
        row=4,
        col=1,
        secondary_y=False,
    )
    figure.update_yaxes(
        title_text="Degrees",
        row=4,
        col=1,
        secondary_y=True,
    )
    figure.update_yaxes(
        title_text="Hz",
        range=[43.0, 52.0],
        row=5,
        col=1,
    )
    figure.update_xaxes(title_text="Sensor time (s)", row=5, col=1)
    return figure


def candidate_rows(events: list) -> list[dict]:
    rows: list[dict] = []
    repetition_number = 0
    for timed in events:
        event = timed.event
        if event.kind not in {"rep", "rejected"}:
            continue
        metrics = event.metrics or {}
        quality = event.quality or {}
        if event.kind == "rep":
            repetition_number += 1
        rows.append(
            {
                "Time (s)": round(timed.sensor_time_s, 2),
                "Candidate": (
                    f"Rep {repetition_number}"
                    if event.kind == "rep"
                    else "Rejected"
                ),
                "Quality": str(
                    quality.get(
                        "quality_status",
                        "accepted" if event.kind == "rep" else "rejected",
                    )
                ).replace("_", " "),
                "Top detection": str(
                    quality.get("top_detection", "not detected")
                ).replace("_", " "),
                "Duration (s)": _rounded(metrics.get("duration_s")),
                "Distance (m)": _rounded(metrics.get("displacement_m")),
                "Mean v (m/s)": _rounded(
                    metrics.get("average_speed_m_s")
                ),
                "Peak v (m/s)": _rounded(metrics.get("peak_speed_m_s")),
                "Drift (m/s)": _rounded(
                    quality.get("drift_correction_m_s")
                ),
                "Raw end v (m/s)": _rounded(
                    quality.get("raw_final_velocity_m_s")
                ),
                "Orientation prominence (°)": _rounded(
                    quality.get("orientation_region_prominence_deg")
                ),
                "Orientation area (°s)": _rounded(
                    quality.get(
                        "orientation_region_excess_area_deg_s"
                    )
                ),
                "Missing": int(quality.get("missing_samples", 0)),
                "Rate (Hz)": _rounded(
                    quality.get("estimated_sample_rate_hz")
                ),
                "Rate confidence": str(
                    quality.get("rate_confidence") or "—"
                ),
                "Evidence": str(quality.get("evidence") or "—"),
                "Resynchronization": str(
                    quality.get("resynchronization_reason") or "—"
                ),
                "Reason": event.reason or "—",
            }
        )
    return rows


def _rounded(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return round(float(value), 3)


def main() -> None:
    arguments = parse_dashboard_arguments()
    st.set_page_config(
        page_title="Beast Live Movement",
        page_icon="🏋️",
        layout="wide",
    )
    st.markdown(
        """
        <style>
        .block-container {padding-top: 1.2rem; padding-bottom: 2rem;}
        [data-testid="stMetric"] {
            border: 1px solid #E2E8F0;
            border-radius: 0.65rem;
            padding: 0.65rem 0.8rem;
            background: #FFFFFF;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title("Beast Live Movement")
    st.caption(
        "A read-only live view of the growing raw recording. "
        "Bluetooth tracking and Excel writing stay in the sensor process."
    )

    available_recordings = sorted(
        RECORDINGS_DIRECTORY.glob("*.jsonl"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    with st.sidebar:
        st.header("Live controls")
        follow_newest = st.toggle(
            "Follow newest recording",
            value=arguments.recording is None,
            help=(
                "Automatically switches to the recording created by the "
                "next sensor run."
            ),
        )
        selected_recording: Path | None = arguments.recording
        if not follow_newest:
            names = [path.name for path in available_recordings]
            default_name = (
                arguments.recording.name
                if arguments.recording is not None
                else names[0] if names else ""
            )
            if names:
                selected_name = st.selectbox(
                    "Recording",
                    names,
                    index=(
                        names.index(default_name)
                        if default_name in names
                        else 0
                    ),
                )
                selected_recording = RECORDINGS_DIRECTORY / selected_name
            else:
                selected_recording = None

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
        refresh_ms = st.select_slider(
            "Refresh interval",
            options=[250, 500, 750, 1000, 1500, 2000],
            value=min(
                [250, 500, 750, 1000, 1500, 2000],
                key=lambda value: abs(value - arguments.refresh_ms),
            ),
            format_func=lambda value: f"{value} ms",
        )
        paused = st.toggle("Pause display", value=False)
        st.caption(f"Detector: {ALGORITHM_VERSION}")

    run_every = None if paused else refresh_ms / 1000.0

    @st.fragment(run_every=run_every)
    def live_panel() -> None:
        current_path = (
            latest_recording(RECORDINGS_DIRECTORY)
            if follow_newest
            else selected_recording
        )
        if current_path is None or not current_path.exists():
            st.info(
                "No recording is available yet. Start the sensor in another "
                "terminal with `./run.ps1 --exercise bench`."
            )
            return

        tail = recording_tail(str(current_path.resolve()), exercise_override)
        with st.spinner("Reading new sensor packets…"):
            new_samples = tail.read_new()
        records = tail.records_since(float(history_seconds))
        visible_events = tail.events_since(float(history_seconds))
        if not records:
            st.info(
                f"Waiting for usable packets in `{current_path.name}`."
            )
            return

        file_age_s = max(0.0, time.time() - current_path.stat().st_mtime)
        receiving = file_age_s <= 3.0 and not paused
        status = (
            "Receiving packets"
            if receiving
            else "Display paused"
            if paused
            else "Waiting for new packets"
        )
        current_record = records[-1]
        metric_columns = st.columns(7)
        metric_columns[0].metric("Status", status)
        metric_columns[1].metric("Profile", tail.exercise)
        metric_columns[2].metric("State", current_record["state_after"])
        metric_columns[3].metric("Repetitions", tail.accepted_reps)
        metric_columns[4].metric("Rejected", tail.rejected_candidates)
        metric_columns[5].metric(
            "Sample rate",
            (
                f"{float(current_record['estimated_sample_rate_hz']):.2f} Hz"
            ),
        )
        metric_columns[6].metric(
            "Missing",
            tail.tracker.total_missing_samples,
        )
        st.caption(
            f"Source: `{current_path.name}` · "
            f"{tail.sample_count:,} decoded samples · "
            f"{new_samples} new in this refresh · "
            f"showing the latest {history_seconds} seconds"
        )

        figure = build_live_figure(records, visible_events)
        st.plotly_chart(
            figure,
            width="stretch",
            config={
                "displaylogo": False,
                "scrollZoom": True,
                "responsive": True,
            },
            key="beast-live-figure",
        )

        st.subheader("Movement candidates")
        rows = candidate_rows(list(tail.events))
        if rows:
            st.dataframe(
                rows,
                width="stretch",
                hide_index=True,
                height=min(520, 80 + 35 * len(rows)),
            )
        else:
            st.caption("No movement candidate has finished yet.")

    live_panel()


if __name__ == "__main__":
    main()
