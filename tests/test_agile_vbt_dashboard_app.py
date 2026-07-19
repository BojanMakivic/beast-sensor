import asyncio
import unittest

from starlette.websockets import WebSocketDisconnect

import agile_vbt_dashboard_app


class _FakeClient:
    def __init__(self) -> None:
        self.identifier = 7
        self.queue: asyncio.Queue[dict] = asyncio.Queue()


class _FakeBroker:
    def __init__(self) -> None:
        self.client = _FakeClient()
        self.unregistered: list[int] = []

    async def register(self, message):
        self.client.queue.put_nowait(
            {
                "type": "snapshot",
                "protocol": 1,
                "revision": 0,
                "server_time_ms": 0,
                "source": None,
                "samples": [],
                "events": [],
                "summary": {},
            }
        )
        return self.client

    async def update_subscription(self, identifier, message):
        return None

    def unregister(self, identifier):
        self.unregistered.append(identifier)


class _FakeWebSocket:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = list(messages)
        self.sent: list[dict] = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        if self.messages:
            return self.messages.pop(0)
        await asyncio.sleep(0.01)
        raise WebSocketDisconnect()

    async def send_json(self, message):
        self.sent.append(message)


class DashboardWebSocketRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_websocket_subscribes_sends_snapshot_and_disconnects_cleanly(
        self,
    ):
        broker = _FakeBroker()
        agile_vbt_dashboard_app.app.state["broker"] = broker
        websocket = _FakeWebSocket(
            [
                {
                    "type": "subscribe",
                    "source": {"mode": "latest", "path": None},
                    "exercise": "bench",
                    "history_seconds": 90,
                }
            ]
        )
        await agile_vbt_dashboard_app.agile_vbt_live_socket(websocket)
        self.assertTrue(websocket.accepted)
        self.assertEqual(websocket.sent[0]["type"], "snapshot")
        self.assertEqual(broker.unregistered, [7])

    async def test_websocket_ping_receives_pong(self):
        broker = _FakeBroker()
        agile_vbt_dashboard_app.app.state["broker"] = broker
        websocket = _FakeWebSocket([{"type": "ping"}])

        await agile_vbt_dashboard_app.agile_vbt_live_socket(websocket)

        self.assertTrue(websocket.accepted)
        self.assertTrue(
            any(message["type"] == "pong" for message in websocket.sent)
        )


if __name__ == "__main__":
    unittest.main()
