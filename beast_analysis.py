"""Offline, interactive diagnostics for Beast Sensor JSONL recordings."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from beast_motion import (
    ALGORITHM_VERSION,
    EXERCISE_PROFILES,
    MotionEvent,
    ReversalRepTracker,
    recording_metadata,
    replay_items,
    tracker_config_for,
)


PROJECT_DIRECTORY = Path(__file__).resolve().parent
ANALYSIS_DIRECTORY = PROJECT_DIRECTORY / "outputs" / "analysis"

COLORS = {
    "raw": "#94A3B8",
    "filtered": "#2563EB",
    "baseline": "#7C3AED",
    "threshold": "#DC2626",
    "provisional": "#64748B",
    "corrected": "#059669",
    "rest": "#0F766E",
    "orientation": "#D97706",
    "accepted": "#16A34A",
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


@dataclass(frozen=True)
class AnalysisResult:
    report_path: Path
    exercise: str
    sample_count: int
    accepted_reps: int
    rejected_candidates: int
    expected_reps: int | None


@dataclass(frozen=True)
class TimedEvent:
    sensor_time_s: float
    event: MotionEvent


def _resolve_exercise(recording: Path, exercise: str | None) -> str:
    metadata = recording_metadata(recording)
    selected = exercise or metadata.get("exercise_profile") or "generic"
    if selected not in EXERCISE_PROFILES:
        return "generic"
    return selected


def _state_regions(records: list[dict]) -> list[tuple[str, float, float]]:
    if not records:
        return []
    regions: list[tuple[str, float, float]] = []
    current_state = str(records[0]["state_after"])
    started = float(records[0]["sensor_time_s"])
    for record in records[1:]:
        state = str(record["state_after"])
        time_s = float(record["sensor_time_s"])
        if state == current_state:
            continue
        regions.append((current_state, started, time_s))
        current_state = state
        started = time_s
    regions.append(
        (
            current_state,
            started,
            float(records[-1]["sensor_time_s"]),
        )
    )
    return regions


def _candidate_rows(
    events: list[TimedEvent],
) -> tuple[list[list[str]], int, int]:
    rows: list[list[str]] = []
    accepted = 0
    rejected = 0
    for timed in events:
        event = timed.event
        if event.kind not in {"rep", "rejected"}:
            continue
        accepted += event.kind == "rep"
        rejected += event.kind == "rejected"
        metrics = event.metrics or {}
        quality = event.quality or {}
        top_detection = str(
            quality.get("top_detection", "not_detected")
        )
        top_label = {
            "velocity": "Velocity",
            "rest_orientation_fallback": "Rest + orientation",
            "not_detected": "Not detected",
        }.get(top_detection, top_detection.replace("_", " ").title())
        recovered = top_detection == "rest_orientation_fallback"
        rows.append(
            [
                f"{timed.sensor_time_s:.2f}",
                (
                    "Accepted (recovered)"
                    if recovered
                    else "Accepted"
                    if event.kind == "rep"
                    else "Rejected"
                ),
                top_label,
                str(quality.get("evidence") or "—"),
                event.reason or "—",
                _format_metric(metrics.get("duration_s")),
                _format_metric(metrics.get("displacement_m")),
                _format_metric(metrics.get("average_speed_m_s")),
                _format_metric(metrics.get("peak_speed_m_s")),
                _format_metric(quality.get("drift_correction_m_s")),
                str(quality.get("missing_samples", 0)),
            ]
        )
    return rows, accepted, rejected


def _format_metric(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{float(value):.3f}"


def _nearest_value(
    records: list[dict],
    sensor_time_s: float,
    field: str,
) -> float:
    if not records:
        return 0.0
    index = min(
        range(len(records)),
        key=lambda item: abs(
            float(records[item]["sensor_time_s"]) - sensor_time_s
        ),
    )
    value = records[index].get(field, 0.0)
    return float(value) if isinstance(value, (int, float)) else 0.0


def _add_event_markers(
    figure: go.Figure,
    records: list[dict],
    events: list[TimedEvent],
) -> None:
    marker_styles = {
        "ready": ("Ready", "#7C3AED", "diamond"),
        "rest": ("Rest", COLORS["rest"], "circle"),
        "up_started": ("Up", "#2563EB", "triangle-up"),
        "top": ("Top", "#9333EA", "triangle-down"),
        "down_started": ("Down", "#D97706", "triangle-down"),
        "bottom": ("Bottom", "#92400E", "triangle-up"),
        "rep": ("Accepted rep", COLORS["accepted"], "star"),
        "recovered_rep": (
            "Recovered rep",
            COLORS["orientation"],
            "diamond",
        ),
        "rejected": ("Rejected", COLORS["rejected"], "x"),
        "gap": ("Packet gap", COLORS["gap"], "line-ns"),
    }
    grouped: dict[str, list[TimedEvent]] = {}
    for timed in events:
        if timed.event.kind in marker_styles:
            style_kind = timed.event.kind
            if (
                timed.event.kind == "rep"
                and (timed.event.quality or {}).get("top_detection")
                == "rest_orientation_fallback"
            ):
                style_kind = "recovered_rep"
            grouped.setdefault(style_kind, []).append(timed)
    for kind, timed_events in grouped.items():
        label, color, symbol = marker_styles[kind]
        x_values = [item.sensor_time_s for item in timed_events]
        y_values = [
            _nearest_value(
                records,
                item.sensor_time_s,
                "filtered_acceleration_m_s2",
            )
            for item in timed_events
        ]
        hover = []
        for item in timed_events:
            quality = item.event.quality or {}
            details = [f"{label}<br>{item.sensor_time_s:.2f} s"]
            if item.event.reason:
                details.append(f"<br>{item.event.reason}")
            if quality.get("evidence"):
                details.append(f"<br>{quality['evidence']}")
            if quality.get("top_detection") == "rest_orientation_fallback":
                details.append(
                    "<br>Raw final velocity: "
                    f"{float(quality.get('raw_final_velocity_m_s', 0.0)):.3f} m/s"
                )
                details.append(
                    "<br>Drift correction: "
                    f"{float(quality.get('drift_correction_m_s', 0.0)):.3f} m/s"
                )
            hover.append("".join(details))
        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=y_values,
                mode="markers",
                name=label,
                marker={
                    "color": color,
                    "symbol": symbol,
                    "size": (
                        10
                        if kind in {"rep", "recovered_rep", "rejected"}
                        else 8
                    ),
                    "line": {"width": 1, "color": "#FFFFFF"},
                },
                hovertext=hover,
                hoverinfo="text",
            ),
            row=1,
            col=1,
            secondary_y=False,
        )


def _build_figure(
    recording: Path,
    exercise: str,
    expected_reps: int | None,
    records: list[dict],
    events: list[TimedEvent],
) -> tuple[go.Figure, int, int]:
    times = [float(record["sensor_time_s"]) for record in records]
    candidates, accepted, rejected = _candidate_rows(events)
    if not candidates:
        candidates = [["—"] * 11]
    table_height = max(280, 56 + 24 * len(candidates))
    plot_row_heights = [285, 225, 185, 165]
    report_height = sum(plot_row_heights) + table_height + 305

    figure = make_subplots(
        rows=5,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=[
            *plot_row_heights,
            table_height,
        ],
        specs=[
            [{"secondary_y": True}],
            [{}],
            [{}],
            [{"secondary_y": True}],
            [{"type": "table"}],
        ],
        subplot_titles=(
            "World-up acceleration",
            "Velocity",
            "Upward displacement",
            "Rest and orientation",
            "Movement candidates",
        ),
    )

    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[
                float(record["raw_vertical_acceleration_m_s2"])
                for record in records
            ],
            mode="lines",
            name="Raw acceleration",
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
            mode="lines",
            name="Filtered acceleration",
            line={"color": COLORS["filtered"], "width": 1.6},
        ),
        row=1,
        col=1,
        secondary_y=False,
    )
    thresholds = [
        float(record["start_threshold_m_s2"]) for record in records
    ]
    for sign, name in ((1.0, "Start threshold"), (-1.0, None)):
        figure.add_trace(
            go.Scattergl(
                x=times,
                y=[sign * value for value in thresholds],
                mode="lines",
                name=name,
                showlegend=name is not None,
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
            mode="lines",
            name="Adaptive gravity baseline",
            line={"color": COLORS["baseline"], "width": 1.2},
        ),
        row=1,
        col=1,
        secondary_y=True,
    )

    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[float(record["velocity_m_s"]) for record in records],
            mode="lines",
            name="Provisional velocity",
            line={"color": COLORS["provisional"], "width": 1.2},
        ),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[float(record["displacement_m"]) for record in records],
            mode="lines",
            name="Provisional displacement",
            line={"color": COLORS["provisional"], "width": 1.1},
        ),
        row=3,
        col=1,
    )
    corrected_legend_shown = False
    for timed in events:
        event = timed.event
        if event.kind not in {"rep", "rejected"} or not event.trace:
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
        figure.add_trace(
            go.Scattergl(
                x=corrected_times,
                y=[point.velocity_m_s for point in event.trace],
                mode="lines",
                name="Drift-corrected velocity",
                legendgroup="corrected",
                showlegend=not corrected_legend_shown,
                line={
                    "color": (
                        COLORS["orientation"]
                        if quality.get("top_detection")
                        == "rest_orientation_fallback"
                        else COLORS["corrected"]
                        if event.kind == "rep"
                        else COLORS["rejected"]
                    ),
                    "width": 2.2,
                },
            ),
            row=2,
            col=1,
        )
        figure.add_trace(
            go.Scattergl(
                x=corrected_times,
                y=[point.displacement_m for point in event.trace],
                mode="lines",
                name="Corrected upward displacement",
                legendgroup="corrected",
                showlegend=False,
                line={
                    "color": (
                        COLORS["orientation"]
                        if quality.get("top_detection")
                        == "rest_orientation_fallback"
                        else COLORS["corrected"]
                        if event.kind == "rep"
                        else COLORS["rejected"]
                    ),
                    "width": 2.2,
                },
            ),
            row=3,
            col=1,
        )
        corrected_legend_shown = True

    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[float(record["rest_confidence"]) for record in records],
            mode="lines",
            name="Rest confidence",
            fill="tozeroy",
            fillcolor="rgba(15,118,110,0.10)",
            line={"color": COLORS["rest"], "width": 1.4},
        ),
        row=4,
        col=1,
        secondary_y=False,
    )
    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[
                float(record["orientation_change_deg"])
                for record in records
            ],
            mode="lines",
            name="Orientation change (degrees)",
            line={"color": COLORS["orientation"], "width": 1.2},
        ),
        row=4,
        col=1,
        secondary_y=True,
    )

    candidate_columns = list(map(list, zip(*candidates)))
    figure.add_trace(
        go.Table(
            columnwidth=[
                55,
                95,
                100,
                205,
                210,
                65,
                75,
                75,
                75,
                75,
                55,
            ],
            header={
                "values": [
                    "Time (s)",
                    "Result",
                    "Top detection",
                    "Evidence",
                    "Reason",
                    "Duration (s)",
                    "Distance (m)",
                    "Mean v (m/s)",
                    "Peak v (m/s)",
                    "Drift (m/s)",
                    "Missing",
                ],
                "fill_color": "#172554",
                "font": {"color": "#FFFFFF", "size": 11},
                "align": "left",
                "height": 28,
            },
            cells={
                "values": candidate_columns,
                "fill_color": [
                    [
                        (
                            "#F0FDF4"
                            if row[1].startswith("Accepted")
                            else "#FEF2F2"
                        )
                        for row in candidates
                    ]
                ]
                * 11,
                "font": {"color": "#0F172A", "size": 10},
                "align": "left",
                "height": 24,
            },
        ),
        row=5,
        col=1,
    )

    for state, color in STATE_COLORS.items():
        figure.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                name=f"State: {state}",
                marker={"symbol": "square", "size": 10, "color": color},
                hoverinfo="skip",
            ),
            row=1,
            col=1,
            secondary_y=False,
        )
    for state, start, end in _state_regions(records):
        if end <= start:
            continue
        for row in range(1, 5):
            figure.add_vrect(
                x0=start,
                x1=end,
                fillcolor=STATE_COLORS.get(state, "#F8FAFC"),
                opacity=0.16,
                line_width=0,
                layer="below",
                exclude_empty_subplots=False,
                row=row,
                col=1,
            )
    _add_event_markers(figure, records, events)

    expected_text = ""
    if expected_reps is not None:
        status = "PASS" if accepted == expected_reps else "FAIL"
        expected_text = (
            f" · expected {expected_reps}, detected {accepted} ({status})"
        )
    figure.update_layout(
        title={
            "text": (
                "Beast Movement Analysis"
                f"<br><sup>{recording.name} · {exercise} · "
                f"{ALGORITHM_VERSION}{expected_text}</sup>"
            ),
            "x": 0.02,
            "xanchor": "left",
            "y": 0.985,
            "yanchor": "top",
            "font": {"size": 22},
        },
        template="plotly_white",
        height=report_height,
        margin={"l": 75, "r": 75, "t": 220, "b": 40},
        hovermode="x unified",
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "left",
            "x": 0.0,
            "font": {"size": 10},
            "itemsizing": "constant",
        },
        font={"family": "Arial, sans-serif", "color": "#0F172A"},
    )
    figure.update_yaxes(
        title_text="Acceleration (m/s²)",
        row=1,
        col=1,
        secondary_y=False,
        zeroline=True,
        zerolinecolor="#CBD5E1",
    )
    figure.update_yaxes(
        title_text="Baseline (g)",
        row=1,
        col=1,
        secondary_y=True,
    )
    figure.update_yaxes(
        title_text="Velocity (m/s)",
        row=2,
        col=1,
        zeroline=True,
        zerolinecolor="#CBD5E1",
    )
    figure.update_yaxes(title_text="Distance (m)", row=3, col=1)
    figure.update_yaxes(
        title_text="Rest confidence",
        range=[0.0, 1.05],
        row=4,
        col=1,
        secondary_y=False,
    )
    figure.update_yaxes(
        title_text="Angle (°)",
        row=4,
        col=1,
        secondary_y=True,
    )
    figure.update_xaxes(
        title_text="Sensor time (s)",
        row=4,
        col=1,
    )
    return figure, accepted, rejected


def analyze_recording(
    recording: Path,
    exercise: str | None = None,
    expected_reps: int | None = None,
    output_directory: Path = ANALYSIS_DIRECTORY,
) -> AnalysisResult:
    """Reprocess raw packet bytes and write a self-contained Plotly report."""
    recording = Path(recording)
    selected_exercise = _resolve_exercise(recording, exercise)
    tracker = ReversalRepTracker(tracker_config_for(selected_exercise))
    records: list[dict] = []
    events: list[TimedEvent] = []

    for item in replay_items(recording):
        if item is None:
            tracker = ReversalRepTracker(
                tracker_config_for(selected_exercise)
            )
            continue
        item_events, record = tracker.process(item)
        records.append(record)
        events.extend(
            TimedEvent(tracker.sensor_time_s, event)
            for event in item_events
        )

    if not records:
        raise ValueError("The recording contains no usable sensor packets.")
    figure, accepted, rejected = _build_figure(
        recording,
        selected_exercise,
        expected_reps,
        records,
        events,
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    report_path = output_directory / (
        f"{recording.stem}-{ALGORITHM_VERSION}-{selected_exercise}.html"
    )
    figure.write_html(
        report_path,
        include_plotlyjs=True,
        full_html=True,
        config={
            "displaylogo": False,
            "responsive": True,
            "scrollZoom": True,
        },
    )
    return AnalysisResult(
        report_path=report_path,
        exercise=selected_exercise,
        sample_count=len(records),
        accepted_reps=accepted,
        rejected_candidates=rejected,
        expected_reps=expected_reps,
    )
