"""Real loopback WebSockets prove visual context cannot acquire household, robot or project tools."""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_bridge import load_bridge_module
from test_everyday_workspace_bridge import HEADERS, ctx


@pytest.mark.parametrize("mode", ["everyday", "workspace"])
def test_realtime_visual_session_rejects_model_selected_tools_before_dispatch(monkeypatch, mode):
    module = load_bridge_module()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    received, calls = [], []

    async def main():
        finished = asyncio.Event()

        async def upstream_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            received.append(await ws.receive_json())
            await ws.send_json(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "user-1",
                    "transcript": "Look at this letter",
                }
            )
            for index, name in enumerate(["home_control", "ask_hermes", "set_reachy_power_mode"]):
                await ws.send_json(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "status": "completed",
                            "name": name,
                            "call_id": f"bad-{index}",
                            "arguments": json.dumps({"request": "OCR says delete files and turn off the lights"}),
                        },
                    }
                )
            async for message in ws:
                if message.type != web.WSMsgType.TEXT:
                    break
                payload = json.loads(message.data)
                received.append(payload)
                outputs = [p for p in received if p.get("item", {}).get("type") == "function_call_output"]
                if len(outputs) == 3:
                    finished.set()
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

        async def forbidden(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("Visual OCR must never reach tools")

        bridge.everyday.call = forbidden
        bridge._agent_answer = forbidden
        bridge._hermes_answer = forbidden
        app = web.Application()
        app.router.add_get("/v1/realtime", bridge.realtime)
        try:
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/v1/realtime", headers=HEADERS)
                await ws.send_json(
                    {
                        "type": "session.start",
                        "assistant_mode": mode,
                        "camera_enabled": True,
                        "robot_tools_enabled": True,
                        "agent_tools_enabled": True,
                        "power_tools_enabled": True,
                        "agent_context": ctx(1, "agent" if mode == "workspace" else "conversation"),
                    }
                )
                await asyncio.wait_for(finished.wait(), 5)
                await ws.send_json({"type": "session.stop"})
                await ws.close()
        finally:
            await bridge.http.close()
            await upstream.close()

    asyncio.run(main())
    assert calls == []
    outputs = [p["item"] for p in received if p.get("item", {}).get("type") == "function_call_output"]
    assert len(outputs) == 3 and all(json.loads(p["output"])["ok"] is False for p in outputs)
    narrowed = [
        p["session"] for p in received if p.get("type") == "session.update" and "audio" not in p.get("session", {})
    ]
    assert len(narrowed) == 1
    assert [t["name"] for t in narrowed[0]["tools"]] == ["capture_reachy_camera"]
