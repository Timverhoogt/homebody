"""Loopback proxy regressions for generation-bound visual continuations."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_bridge import load_bridge_module
from test_everyday_workspace_bridge import HEADERS, ctx


@pytest.mark.parametrize("event_type", ["response.output_item.done", "response.function_call_arguments.done"])
@pytest.mark.parametrize("superseded", [False, True])
@pytest.mark.parametrize("tagged", [False, True])
def test_proxy_matches_visual_result_to_original_generation(monkeypatch, event_type, superseded, tagged):
    module = load_bridge_module()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    received, creates = [], []
    lifetime = module.RealtimeResponseLifecycle

    class ObservedLifetime(lifetime):
        def __init__(self, send, **kwargs):
            super().__init__(send, settle_seconds=0)

        async def request_create(self, generation=None):
            creates.append(generation)
            return await super().request_create(generation)

    monkeypatch.setattr(module, "RealtimeResponseLifecycle", ObservedLifetime)

    async def scenario():
        barge, marker, created = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def upstream_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.receive_json()  # initial session.update
            await ws.send_json(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "user-1",
                    "transcript": "Read this letter",
                }
            )
            if event_type == "response.output_item.done":
                event = {
                    "type": event_type,
                    "item": {
                        "type": "function_call",
                        "status": "completed",
                        "name": "capture_reachy_camera",
                        "call_id": "look-1",
                        "arguments": json.dumps({"purpose": "Read this letter"}),
                    },
                }
            else:
                event = {
                    "type": event_type,
                    "name": "capture_reachy_camera",
                    "call_id": "look-1",
                    "arguments": json.dumps({"purpose": "Read this letter"}),
                }
            await ws.send_json(event)
            if superseded:
                await barge.wait()
                await ws.send_json({"type": "input_audio_buffer.speech_started"})
                await ws.send_json({"type": "input_audio_buffer.speech_stopped"})
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    break
                data = json.loads(message.data)
                received.append(data)
                assert "homebody_tool_call_id" not in data  # private protocol never forwarded upstream
                if data.get("type") == "input_audio_buffer.append":
                    marker.set()
                if data.get("type") == "response.create":
                    created.set()
            return ws

        upstream_app = web.Application()
        upstream_app.router.add_get("/v1/realtime", upstream_handler)
        upstream = TestServer(upstream_app)
        await upstream.start_server()
        url = str(upstream.make_url("/v1/realtime")).replace("http", "ws", 1)

        class Redirect(aiohttp.ClientSession):
            def ws_connect(self, target, **kwargs):
                assert target.startswith("wss://api.openai.com/v1/realtime")
                return super().ws_connect(url, **kwargs)

        monkeypatch.setattr(module, "ClientSession", Redirect)
        bridge = module.Bridge(api_key="bridge-secret", hermes_url="http://127.0.0.1:1")
        bridge.http = aiohttp.ClientSession()
        app = web.Application()
        app.router.add_get("/v1/realtime", bridge.realtime)
        try:
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/v1/realtime", headers=HEADERS)
                await ws.send_json(
                    {
                        "type": "session.start",
                        "assistant_mode": "everyday",
                        "camera_enabled": True,
                        "robot_tools_enabled": False,
                        "agent_context": ctx(1, "conversation"),
                    }
                )
                while (await asyncio.wait_for(ws.receive_json(), 3)).get("type") != event_type:
                    pass
                if superseded:
                    barge.set()
                    while (await asyncio.wait_for(ws.receive_json(), 3)).get(
                        "type"
                    ) != "input_audio_buffer.speech_stopped":
                        pass
                await ws.send_json(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": "look-1",
                            "output": json.dumps({"description": "Monday"}),
                        },
                    }
                )
                await ws.send_json(
                    {"type": "response.create", **({"homebody_tool_call_id": "look-1"} if tagged else {})}
                )
                await ws.send_json({"type": "input_audio_buffer.append", "audio": "fixture-marker"})
                await asyncio.wait_for(marker.wait(), 3)
                if not superseded:
                    await asyncio.wait_for(created.wait(), 3)
                await ws.send_json({"type": "session.stop"})
                await ws.close()
        finally:
            await bridge.http.close()
            await upstream.close()

    asyncio.run(scenario())
    results = [r for r in received if r.get("item", {}).get("type") == "function_call_output"]
    assert len(results) == (0 if superseded else 1)
    if superseded:
        assert creates == []
        assert not [r for r in received if r.get("type") == "response.create"]
    else:
        assert len(creates) == 1 and creates[0] is not None
        assert len([r for r in received if r.get("type") == "response.create"]) == 1
