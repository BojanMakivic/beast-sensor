"""Persistent Beast Sensor dashboard component."""

from __future__ import annotations

from pathlib import Path

import streamlit as st


_COMPONENT = st.components.v2.component(
    "beast-live-display.beast_live_display",
    html='<div class="beast-live-root"></div>',
    css="index-*.css",
    js="index-*.js",
)


def beast_live_display(
    *,
    source_mode: str,
    recording_path: Path | None,
    exercise: str | None,
    history_seconds: int,
    paused: bool,
    key: str = "beast-live-display",
):
    """Mount the persistent WebSocket dashboard.

    The component owns its WebSocket and updates Plotly in the browser without
    triggering Streamlit reruns.
    """
    if source_mode not in {"latest", "file"}:
        raise ValueError("source_mode must be 'latest' or 'file'.")
    return _COMPONENT(
        key=key,
        data={
            "websocketPath": "/api/beast/live",
            "source": {
                "mode": source_mode,
                "path": (
                    str(recording_path.resolve())
                    if recording_path is not None
                    else None
                ),
            },
            "exercise": exercise,
            "historySeconds": int(history_seconds),
            "paused": bool(paused),
        },
        width="stretch",
        height=1540,
    )
