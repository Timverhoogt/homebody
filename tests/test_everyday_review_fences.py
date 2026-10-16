"""Regress independently reproduced cross-transport admission gaps; loopback only."""

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_bridge import load_bridge_module
from test_everyday_local_transport import run_host
from test_everyday_workspace_bridge import HEADERS, ctx, session
from test_live_transport import FakeLive, collect, delta

from companion import live_transport


@pytest.mark.parametrize("name,allowed", [("set_reachy_volume", 1), ("get_reachy_status", 3), ("public_search", 0)])
def test_realtime_shares_budget_and_preserves_valid_bounded_host_json(monkeypatch, name, allowed):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    module = load_bridge_module()

    async def main():
        results, start_messages = [], []
        call_count = 1 if name == "public_search" else 4

        async def provider(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            start_messages.append(await ws.receive_json())
            await ws.send_json({"type": "session.updated"})

            async def send_call(index):
                args = (
                    {"volume": 30}
                    if name == "set_reachy_volume"
                    else {"query": "news"}
                    if name == "public_search"
                    else {}
                )
                await ws.send_json(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "status": "completed",
                            "call_id": f"call-{index}",
                            "name": name,
                            "arguments": json.dumps(args),
                        },
                    }
                )

            await send_call(0)
            async for message in ws:
                payload = json.loads(message.data)
                if payload.get("item", {}).get("type") == "function_call_output":
                    output = payload["item"]["output"]
                    assert len(output) <= 2000
                    results.append(json.loads(output))
                    if len(results) < call_count:
                        await send_call(len(results))
                    else:
                        await ws.send_json({"type": "fixture.done"})
            return ws

        upstream_app = web.Application()
        upstream_app.router.add_get("/realtime", provider)
        async with TestServer(upstream_app) as upstream:
            real_session = aiohttp.ClientSession

            class Redirect:
                def __init__(self, **kw):
                    self.client = real_session(**kw)

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_):
                    await self.client.close()

                def ws_connect(self, url, **kw):
                    return self.client.ws_connect(upstream.make_url("/realtime"), **kw)

            monkeypatch.setattr(module, "ClientSession", Redirect)
            bridge = module.Bridge(api_key="bridge-secret", hermes_url="http://127.0.0.1:1")
            bridge.http = real_session()
            if name == "public_search":

                async def large_result(*a, **kw):
                    return {
                        "ok": True,
                        "sources": [
                            {"url": "https://example.com/" + "a" * 480, "title": "Fixture", "snippet": "b" * 220}
                            for _ in range(3)
                        ],
                    }

                bridge.everyday.call = large_result
            app = web.Application()
            app.router.add_post("/v1/agent/session", bridge.broker_session)
            app.router.add_get("/v1/realtime", bridge.realtime)
            try:
                async with TestClient(TestServer(app)) as client:
                    await session(client, ctx(27, "conversation"))
                    forwarded = []
                    async with client.ws_connect("/v1/realtime", headers=HEADERS) as ws:
                        await ws.send_json(
                            {
                                "type": "session.start",
                                "assistant_mode": "everyday",
                                "local_tools": ["get_reachy_status", "set_reachy_volume"],
                                "agent_context": ctx(27, "conversation"),
                            }
                        )
                        while True:
                            event = await ws.receive_json(timeout=3)
                            if event["type"] == "fixture.done":
                                break
                            if event["type"] == "response.output_item.done" and name != "public_search":
                                forwarded.append(event["item"]["call_id"])
                                await ws.send_json(
                                    {
                                        "type": "conversation.item.create",
                                        "item": {
                                            "type": "function_call_output",
                                            "call_id": event["item"]["call_id"],
                                            "output": '{"ok":true,"verified":true}',
                                        },
                                    }
                                )
                        await ws.send_json({"type": "session.stop"})
                    assert len(forwarded) == allowed
                    assert len(results) == call_count
                    if name == "public_search":
                        assert results[0]["truncated"] and results[0]["total_sources"] == 3
                    else:
                        assert sum(result["ok"] for result in results) == allowed
            finally:
                await bridge.http.close()

    asyncio.run(main())


def test_superseded_live_model_response_cannot_create_timer(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")

    async def main(bridge, client, module):
        held, release, newer = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def script(ws, received):
            await ws.send_json(delta("user", 0, 500, "Set a timer for 60 seconds"))
            await ws.send_json(
                {
                    "type": "session.delegation.created",
                    "offset_ms": 500,
                    "delegation": {"id": "first", "target": "client"},
                }
            )
            await newer.wait()
            await ws.send_json(delta("user", 600, 900, "Never mind, tell me a joke instead"))
            await ws.send_json(
                {
                    "type": "session.delegation.created",
                    "offset_ms": 900,
                    "delegation": {"id": "second", "target": "client"},
                }
            )
            await ws.send_json(delta("assistant", 1000, 1100, "revision observed"))

        fake = FakeLive(script)

        class Provider:
            name = "openai-codex"
            model = staticmethod(lambda role: "fixture")
            request = staticmethod(lambda body: body)

            def __init__(self):
                self.count = 0

            async def complete(self, body, **kw):
                self.count += 1
                if self.count == 1:
                    held.set()
                    await release.wait()
                    message = {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "stale-timer",
                                "type": "function",
                                "function": {"name": "set_timer", "arguments": '{"seconds":60}'},
                            }
                        ],
                    }
                else:
                    message = {"content": "A fixture joke."}
                return {"choices": [{"message": message}]}

        bridge.everyday_llm = Provider()
        bridge._everyday_key = lambda: "fixture-only"
        bridge.agent_broker.actions.config.reminder_callback_url = "https://callback.invalid"
        bridge.agent_broker.actions.config.reminder_callback_token = "fixture-only"
        async with TestServer(fake.app()) as upstream:
            real_serve = live_transport.serve_live

            async def relay(request, **kw):
                return await real_serve(
                    request, upstream_url=str(upstream.make_url("/v1/live/sessions")).replace("http", "ws", 1), **kw
                )

            monkeypatch.setitem(bridge.live.__func__.__globals__, "serve_live", relay)
            events = []
            async with client.ws_connect("/v1/live", headers=HEADERS) as ws:
                await ws.send_json(
                    {
                        "type": "session.start",
                        "assistant_mode": "everyday",
                        "model": "gpt-live-1",
                        "voice": "marin",
                        "agent_context": ctx(27, "conversation"),
                        "local_tools": ["get_reachy_status"],
                    }
                )
                await asyncio.wait_for(held.wait(), 2)
                newer.set()
                await collect(ws, events, lambda p: p.get("delta") == "revision observed")
                release.set()
                await collect(ws, events, lambda p: p.get("state") == "answered")
                assert not bridge.agent_broker.actions._scheduled_tasks
                assert not any(bridge.agent_broker.actions._timers.values())
                await ws.send_json({"type": "session.close"})
                await collect(ws, events, lambda p: p["type"] == "session.closed")
        await bridge.agent_broker.actions.shutdown()

    run_host(main)
