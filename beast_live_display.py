"""Repository-local wrapper for the Beast Streamlit component.

The production dashboard loads the committed frontend build directly. This
keeps dashboard startup independent from editable-package discovery while the
template-generated component project remains available for frontend builds.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st


PROJECT_DIRECTORY = Path(__file__).resolve().parent
BUILD_DIRECTORY = (
    PROJECT_DIRECTORY
    / "components"
    / "beast-live-display"
    / "beast_live_display"
    / "frontend"
    / "build"
)


def _read_single_asset(pattern: str) -> str:
    matches = list(BUILD_DIRECTORY.glob(pattern))
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one dashboard asset matching {pattern!r} in "
            f"{BUILD_DIRECTORY}, found {len(matches)}. Rebuild the component."
        )
    return matches[0].read_text(encoding="utf-8")


_COMPONENT = st.components.v2.component(
    "beast_live_display_local",
    html='<div class="beast-live-root"></div>',
    css=_read_single_asset("index-*.css"),
    js=_read_single_asset("index-*.js"),
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
    """Mount the persistent WebSocket dashboard."""
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
