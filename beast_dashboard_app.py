"""ASGI entry point for the Streamlit dashboard and its live WebSocket."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import streamlit as st
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from beast_dashboard_stream import DashboardBroker, PROTOCOL_VERSION


PROJECT_DIRECTORY = Path(__file__).resolve().parent
DASHBOARD_SCRIPT = PROJECT_DIRECTORY / "beast_dashboard.py"
RECORDINGS_DIRECTORY = PROJECT_DIRECTORY / "outputs" / "recordings"


def _launcher_recording() -> Path | None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--recording", type=Path)
    arguments, _unknown = parser.parse_known_args()
    if arguments.recording is None:
        return None
    return arguments.recording.resolve()


async def _send_messages(
    websocket: WebSocket,
    client,
) -> None:
    while True:
        message = await client.queue.get()
        await websocket.send_json(message)


async def beast_live_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    broker: DashboardBroker = app.state["broker"]
    client = None
    sender: asyncio.Task | None = None
    try:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                await websocket.send_json(
                    {
                        "type": "error",
                        "protocol": PROTOCOL_VERSION,
                        "message": "WebSocket messages must be JSON objects.",
                    }
                )
                continue
            if message.get("type") == "subscribe":
                try:
                    if client is None:
                        client = await broker.register(message)
                        sender = asyncio.create_task(
                            _send_messages(websocket, client),
                            name=f"beast-dashboard-client-{client.identifier}",
                        )
                    else:
                        await broker.update_subscription(
                            client.identifier,
                            message,
                        )
                except ValueError as exc:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "protocol": PROTOCOL_VERSION,
                            "message": str(exc),
                        }
                    )
            elif message.get("type") == "ping":
                await websocket.send_json(
                    {
                        "type": "pong",
                        "protocol": PROTOCOL_VERSION,
                    }
                )
            else:
                await websocket.send_json(
                    {
                        "type": "error",
                        "protocol": PROTOCOL_VERSION,
                        "message": "Unsupported WebSocket message type.",
                    }
                )
    except WebSocketDisconnect:
        pass
    finally:
        if client is not None:
            broker.unregister(client.identifier)
        if sender is not None:
            sender.cancel()
            try:
                await sender
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass


@asynccontextmanager
async def lifespan(_app):
    recording = _launcher_recording()
    broker = DashboardBroker(
        RECORDINGS_DIRECTORY,
        allowed_recordings=(() if recording is None else (recording,)),
    )
    await broker.start()
    try:
        yield {"broker": broker}
    finally:
        await broker.stop()


app = st.App(
    DASHBOARD_SCRIPT,
    lifespan=lifespan,
    routes=[WebSocketRoute("/api/beast/live", beast_live_socket)],
)
