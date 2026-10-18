"""Real HTTP model loop and native Realtime wire contract; loopback only."""

import asyncio
import json

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_bridge import load_bridge_module
from test_everyday_local_transport import run_host, start
from test_everyday_workspace_bridge import HEADERS, ctx, session

from companion.everyday_manifest import local_tool_specs
from companion.llm_providers import TextModelProvider


def test_pipeline_real_everyday_model_loop_over_http():
    async def test(bridge, client, module):
        requests = []

        async def model(request):
            body = await request.json()
            requests.append(body)
            message = (
                {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "model-call-1",
                            "type": "function",
                            "function": {"name": "set_reachy_volume", "arguments": '{"muted":true}'},
                        }
                    ],
                }
                if len(requests) == 1
                else {"content": "The speaker reports muted."}
            )
            return web.json_response({"choices": [{"message": message}]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", model)
        async with TestServer(app) as model_server:
            bridge.everyday_llm = TextModelProvider.from_env(
                lambda k: {
                    "REACHY_LLM_PROVIDER": "custom",
                    "REACHY_LLM_URL": str(model_server.make_url("/v1")),
                    "REACHY_LLM_MODEL": "fixture-model",
                }.get(k, "")
            )
            bridge.everyday_llm.models["everyday"] = "fixture-model"
            bridge._everyday_key = lambda: "fixture-only"
            async with client.ws_connect("/v1/everyday/session", headers=HEADERS) as ws:
                await ws.send_json(start(["set_reachy_volume"]))
                call = await ws.receive_json(timeout=2)
                assert call["name"] == "set_reachy_volume" and call["arguments"] == {"muted": True}
                await ws.send_json(
                    {
                        "type": "everyday.tool_result",
                        "call_id": call["call_id"],
                        "result": {"ok": True, "verified": True, "muted": True},
                    }
                )
                assert await ws.receive_json(timeout=2) == {
                    "type": "everyday.answer",
                    "text": "The speaker reports muted.",
                }
            assert len(requests) == 2
            names = {t["function"]["name"] for t in requests[0]["tools"]}
            assert "set_reachy_volume" in names and not names & {"ask_hermes", "ask_openclaw"}
            assert json.loads(requests[1]["messages"][-1]["content"])["muted"] is True

    run_host(test)


@pytest.mark.parametrize(
    "variant",
    ["valid", "invalid_args", "unadvertised", "schema_override", "stale", "unknown_result", "duplicate_result"],
)
@pytest.mark.parametrize("standalone", [False, True])
def test_realtime_advertisement_canonical_and_admission(monkeypatch, variant, standalone):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    module = load_bridge_module()

    async def main():
        upstream_requests = []
        send_call = asyncio.Event()

        async def provider(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            upstream_requests.append(await ws.receive_json())
            await ws.send_json({"type": "session.updated"})
            await send_call.wait()
            if variant in {"valid", "invalid_args", "unadvertised", "unknown_result", "duplicate_result"}:
                await ws.send_json(
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "status": "completed",
                            "call_id": "local-1",
                            "name": "get_reachy_status" if variant == "unadvertised" else "set_reachy_volume",
                            "arguments": '{"volume":true}' if variant == "invalid_args" else '{"volume":35}',
                        },
                    }
                )
            async for message in ws:
                upstream_requests.append(json.loads(message.data))
                if upstream_requests[-1].get("item", {}).get("type") == "function_call_output":
                    await ws.send_json({"type": "fixture.result_received"})
            return ws

        app = web.Application()
        app.router.add_get("/v1/realtime", provider)
        async with TestServer(app) as upstream:
            real_session = aiohttp.ClientSession

            class Redirect:
                def __init__(self, **kw):
                    self.session = real_session(**kw)

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    await self.session.close()

                def ws_connect(self, url, **kwargs):
                    return self.session.ws_connect(upstream.make_url("/v1/realtime"), **kwargs)

            monkeypatch.setattr(module, "ClientSession", Redirect)
            bridge = module.Bridge(api_key="bridge-secret", hermes_url="http://127.0.0.1:1",
                                   backends=() if standalone else ("hermes",))
            if standalone:
                bridge._agent_identity = lambda *a: pytest.fail("Everyday must not resolve an agent")
                bridge._agent_answer = lambda *a, **k: pytest.fail("Everyday must not invoke an agent")
            bridge.http = real_session()
            host = web.Application()
            host.router.add_post("/v1/agent/session", bridge.broker_session)
            host.router.add_get("/v1/realtime", bridge.realtime)
            try:
                async with TestClient(TestServer(host)) as client:
                    await session(client, ctx(27, "conversation"))
                    async with client.ws_connect("/v1/realtime", headers=HEADERS) as ws:
                        await ws.send_json(
                            {
                                "type": "session.start",
                                "assistant_mode": "everyday",
                                "local_tools": ["set_reachy_volume"],
                                "agent_context": ctx(27, "conversation"),
                            }
                        )
                        assert (await ws.receive_json(timeout=2))["type"] == "session.updated"
                        spec = next(
                            t for t in upstream_requests[0]["session"]["tools"] if t["name"] == "set_reachy_volume"
                        )
                        assert spec == local_tool_specs(["set_reachy_volume"])[0]
                        assert not {"ask_hermes", "ask_openclaw"} & {
                            t["name"] for t in upstream_requests[0]["session"]["tools"]
                        }
                        if variant == "schema_override":
                            await ws.send_json({"type": "session.update", "session": {"tools": [{"name": "shell"}]}})
                        if variant == "stale":
                            await session(client, ctx(28, "conversation"))
                        send_call.set()
                        response = await ws.receive(timeout=2)
                        if variant in {"valid", "unknown_result", "duplicate_result"}:
                            assert json.loads(response.data)["item"]["name"] == "set_reachy_volume"
                            result = {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": ("unknown" if variant == "unknown_result" else "local-1"),
                                    "output": '{"ok":true,"volume":35}',
                                },
                            }
                            await ws.send_json(result)
                            reply = await ws.receive_json(timeout=2)
                            if variant == "unknown_result":
                                assert reply["type"] == "bridge.error"
                            else:
                                assert reply["type"] == "fixture.result_received"
                                if variant == "duplicate_result":
                                    await ws.send_json(result)
                                    assert (await ws.receive_json(timeout=2))["type"] == "bridge.error"
                                else:
                                    await ws.send_json({"type": "session.stop"})
                        elif variant in {"invalid_args", "unadvertised"}:
                            assert json.loads(response.data)["type"] == "bridge.error"
                        else:
                            assert response.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
                    assert not any(
                        p.get("type") == "session.update" and "shell" in json.dumps(p) for p in upstream_requests
                    )
            finally:
                await bridge.http.close()

    asyncio.run(main())
