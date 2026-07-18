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


def _orientation_regions(
    records: list[dict],
) -> list[tuple[int, float, float, bool, float, float]]:
    """Return adaptive orientation regions for report shading."""
    regions: list[tuple[int, float, float, bool, float, float]] = []
    active_start: float | None = None
    active_id = 0
    for record in records:
        time_s = float(record["sensor_time_s"])
        if record.get("orientation_region_started"):
            active_start = time_s
            active_id = int(record.get("orientation_region_id", 0))
        if record.get("orientation_region_ended") and active_start is not None:
            regions.append(
                (
                    active_id,
                    active_start,
                    time_s,
                    bool(record.get("orientation_region_confirmed")),
                    float(
                        record.get(
                            "orientation_region_prominence_deg",
                            0.0,
                        )
                    ),
                    float(
                        record.get(
                            "orientation_region_excess_area_deg_s",
                            0.0,
                        )
                    ),
                )
            )
            active_start = None
    if active_start is not None and records:
        last = records[-1]
        regions.append(
            (
                active_id,
                active_start,
                float(last["sensor_time_s"]),
                False,
                float(last.get("orientation_region_prominence_deg", 0.0)),
                float(
                    last.get(
                        "orientation_region_excess_area_deg_s",
                        0.0,
                    )
                ),
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
            "orientation_velocity_boundary": (
                "Orientation + velocity boundary"
            ),
            "not_detected": "Not detected",
        }.get(top_detection, top_detection.replace("_", " ").title())
        recovered = top_detection in {
            "rest_orientation_fallback",
            "orientation_velocity_boundary",
        }
        quality_status = str(
            quality.get(
                "quality_status",
                "accepted" if event.kind == "rep" else "rejected",
            )
        )
        rows.append(
            [
                f"{timed.sensor_time_s:.2f}",
                (
                    "Accepted (short)"
                    if quality.get("short_distance")
                    else "Accepted (recovered)"
                    if recovered
                    else "Accepted"
                    if event.kind == "rep"
                    else "Rejected"
                ),
                top_label,
                quality_status.replace("_", " ").title(),
                str(quality.get("evidence") or "—"),
                str(
                    quality.get("resynchronization_reason")
                    or "—"
                ),
                event.reason or "—",
                _format_metric(metrics.get("duration_s")),
                _format_metric(metrics.get("displacement_m")),
                _format_metric(metrics.get("average_speed_m_s")),
                _format_metric(metrics.get("peak_speed_m_s")),
                _format_metric(quality.get("drift_correction_m_s")),
                str(quality.get("missing_samples", 0)),
                _format_metric(
                    quality.get("estimated_sample_rate_hz")
                ),
                str(quality.get("rate_confidence") or "—"),
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
                in {
                    "rest_orientation_fallback",
                    "orientation_velocity_boundary",
                }
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
            if quality.get("top_detection") in {
                "rest_orientation_fallback",
                "orientation_velocity_boundary",
            }:
                details.append(
                    "<br>Raw final velocity: "
                    f"{float(quality.get('raw_final_velocity_m_s', 0.0)):.3f} m/s"
                )
                details.append(
                    "<br>Drift correction: "
                    f"{float(quality.get('drift_correction_m_s', 0.0)):.3f} m/s"
                )
                details.append(
                    "<br>Orientation prominence: "
                    f"{float(quality.get('orientation_region_prominence_deg', 0.0)):.1f}°"
                )
                details.append(
                    "<br>Orientation excess area: "
                    f"{float(quality.get('orientation_region_excess_area_deg_s', 0.0)):.1f} degree-seconds"
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
        candidates = [["—"] * 15]
    table_height = max(280, 56 + 24 * len(candidates))
    plot_row_heights = [285, 225, 185, 205, 145]
    report_height = sum(plot_row_heights) + table_height + 305

    figure = make_subplots(
        rows=6,
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
            [{}],
            [{"type": "table"}],
        ],
        subplot_titles=(
            "World-up acceleration",
            "Velocity",
            "Upward displacement",
            "Rest and orientation",
            "Adaptive sample clock",
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
                        in {
                            "rest_orientation_fallback",
                            "orientation_velocity_boundary",
                        }
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
                        in {
                            "rest_orientation_fallback",
                            "orientation_velocity_boundary",
                        }
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
    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[
                float(record.get("orientation_baseline_lower_deg", 0.0))
                for record in records
            ],
            mode="lines",
            name="Orientation baseline lower band",
            line={"color": "#A16207", "width": 0.8, "dash": "dot"},
        ),
        row=4,
        col=1,
        secondary_y=True,
    )
    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[
                float(record.get("orientation_baseline_upper_deg", 0.0))
                for record in records
            ],
            mode="lines",
            name="Orientation baseline upper band",
            fill="tonexty",
            fillcolor="rgba(217,119,6,0.10)",
            line={"color": "#A16207", "width": 0.8, "dash": "dot"},
        ),
        row=4,
        col=1,
        secondary_y=True,
    )
    figure.add_trace(
        go.Scattergl(
            x=times,
            y=[
                float(record.get("orientation_start_threshold_deg", 0.0))
                for record in records
            ],
            mode="lines",
            name="Orientation region start threshold",
            line={"color": "#B91C1C", "width": 1.0, "dash": "dash"},
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
            mode="lines",
            name="Estimated sample rate",
            line={"color": "#4F46E5", "width": 1.5},
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

    candidate_columns = list(map(list, zip(*candidates)))
    figure.add_trace(
        go.Table(
            columnwidth=[
                55,
                95,
                130,
                90,
                190,
                180,
                210,
                65,
                75,
                75,
                75,
                75,
                55,
                65,
                75,
            ],
            header={
                "values": [
                    "Time (s)",
                    "Result",
                    "Top detection",
                    "Quality",
                    "Evidence",
                    "Resynchronization",
                    "Reason",
                    "Duration (s)",
                    "Distance (m)",
                    "Mean v (m/s)",
                    "Peak v (m/s)",
                    "Drift (m/s)",
                    "Missing",
                    "Rate (Hz)",
                    "Rate confidence",
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
                * 15,
                "font": {"color": "#0F172A", "size": 10},
                "align": "left",
                "height": 24,
            },
        ),
        row=6,
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
    for (
        region_id,
        start,
        end,
        confirmed,
        prominence,
        area,
    ) in _orientation_regions(records):
        if end <= start:
            continue
        figure.add_shape(
            type="rect",
            x0=start,
            x1=end,
            y0=0,
            y1=1,
            xref="x4",
            yref="y5 domain",
            fillcolor=(
                "rgba(217,119,6,0.18)"
                if confirmed
                else "rgba(148,163,184,0.12)"
            ),
            opacity=1.0,
            line={
                "width": 1,
                "color": (
                    "rgba(217,119,6,0.45)"
                    if confirmed
                    else "rgba(100,116,139,0.35)"
                ),
            },
            layer="below",
        )
        figure.add_annotation(
            x=start,
            y=1,
            xref="x4",
            yref="y5 domain",
            text=(
                f"R{region_id} · {prominence:.1f}° · "
                f"{area:.1f}°s"
            ),
            showarrow=False,
            xanchor="left",
            yanchor="top",
            font={"size": 9, "color": "#92400E"},
        )
    _add_event_markers(figure, records, events)
    provisional_tops = [
        timed for timed in events if timed.event.kind == "provisional_top"
    ]
    if provisional_tops:
        figure.add_trace(
            go.Scatter(
                x=[
                    float(
                        (timed.event.quality or {}).get(
                            "provisional_top_s",
                            timed.sensor_time_s,
                        )
                    )
                    for timed in provisional_tops
                ],
                y=[
                    float(
                        (timed.event.quality or {}).get(
                            "raw_velocity_m_s",
                            0.0,
                        )
                    )
                    for timed in provisional_tops
                ],
                mode="markers",
                name="Provisional velocity minimum",
                marker={
                    "color": "#7C3AED",
                    "symbol": "triangle-down",
                    "size": 8,
                },
                customdata=[
                    [
                        float(
                            (timed.event.quality or {}).get(
                                "peak_velocity_m_s",
                                0.0,
                            )
                        ),
                        float(
                            (timed.event.quality or {}).get(
                                "velocity_drop_fraction",
                                0.0,
                            )
                        ),
                    ]
                    for timed in provisional_tops
                ],
                hovertemplate=(
                    "%{x:.2f} s<br>Minimum: %{y:.3f} m/s"
                    "<br>Peak: %{customdata[0]:.3f} m/s"
                    "<br>Drop: %{customdata[1]:.0%}<extra></extra>"
                ),
            ),
            row=2,
            col=1,
        )

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
            "x": 0.5,
            "xanchor": "center",
            "y": 0.995,
            "yanchor": "top",
            "font": {"size": 22},
        },
        template="plotly_white",
        height=report_height,
        margin={"l": 75, "r": 75, "t": 250, "b": 40},
        hovermode="x unified",
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.0,
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
    figure.update_yaxes(
        title_text="Rate (Hz)",
        range=[43.0, 52.0],
        row=5,
        col=1,
    )
    figure.update_xaxes(
        title_text="Sensor time (s)",
        row=5,
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
    html = figure.to_html(
        include_plotlyjs=True,
        full_html=True,
        config={
            "displaylogo": False,
            "responsive": True,
            "scrollZoom": True,
        },
    )
    html = html.replace(
        "<head>",
        (
            "<head><style>"
            "html,body{margin:0;overflow-x:auto;}"
            ".plotly-graph-div{min-width:1900px;}"
            "</style>"
        ),
        1,
    )
    report_path.write_text(html, encoding="utf-8")
    return AnalysisResult(
        report_path=report_path,
        exercise=selected_exercise,
        sample_count=len(records),
        accepted_reps=accepted,
        rejected_candidates=rejected,
        expected_reps=expected_reps,
    )
