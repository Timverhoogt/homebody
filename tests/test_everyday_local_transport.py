"""Authenticated loopback robot-local RPC and adult vision; no hardware/providers."""

import asyncio
import base64
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_bridge import load_bridge_module
from test_everyday_workspace_bridge import HEADERS, ctx, session
from test_live_transport import FakeLive, collect, delta

from companion import everyday_manifest, live_transport
from companion.everyday_manifest import (
    LOCAL_TOOL_NAMES,
    LocalToolTransport,
    local_tool_specs,
    validate_local_arguments,
    validate_local_tool_names,
)

VALID = [
    ("get_reachy_status", {}),
    ("set_reachy_power_mode", {"mode": "standby"}),
    ("capture_reachy_camera", {"purpose": "Read this label"}),
    ("start_reachy_camera", {"duration_seconds": 5}),
    ("stop_reachy_camera", {}),
    ("get_reachy_volume", {}),
    ("set_reachy_volume", {"volume": 0}),
    ("set_reachy_volume", {"muted": False}),
    ("move_reachy_head", {"direction": "center"}),
    ("express_reachy_emotion", {"emotion": "welcoming"}),
    ("dance_reachy", {"style": "short"}),
]


@pytest.mark.parametrize("name,args", VALID)
def test_manifest_valid_and_fresh_specs(name, args):
    assert validate_local_arguments(name, args) == args
    first = local_tool_specs([name])
    first[0]["parameters"]["properties"].clear()
    assert local_tool_specs([name])[0]["name"] == name
    assert {s["name"] for s in local_tool_specs()} == LOCAL_TOOL_NAMES


@pytest.mark.parametrize(
    "name,args",
    [
        ("set_reachy_volume", {"volume": True}),
        ("set_reachy_volume", {"volume": 1.0}),
        ("set_reachy_volume", {"muted": 0}),
        ("set_reachy_volume", {}),
        ("set_reachy_volume", {"volume": 20, "muted": True}),
        ("set_reachy_volume", {"volume": 101}),
        ("set_reachy_volume", {"volume": -1}),
        ("start_reachy_camera", {"duration_seconds": True}),
        ("start_reachy_camera", {"duration_seconds": 4}),
        ("start_reachy_camera", {"duration_seconds": 121}),
        ("capture_reachy_camera", {"purpose": "x" * 301}),
        ("get_reachy_status", {"extra": 1}),
        ("set_reachy_power_mode", {"mode": "shutdown"}),
        ("dance_reachy", {"style": "forever"}),
    ],
)
def test_manifest_strict_arguments(name, args):
    with pytest.raises(ValueError):
        validate_local_arguments(name, args)


@pytest.mark.parametrize(
    "names",
    [
        "get_reachy_status",
        ["shell"],
        ["get_reachy_status"] * 2,
        [{"name": "get_reachy_status", "parameters": {}}],
        None,
    ],
)
def test_names_not_client_schemas(names):
    with pytest.raises(ValueError):
        validate_local_tool_names(names)


def test_rpc_correlates_once_and_cleans_timeout_cancel_and_stale(monkeypatch):
    monkeypatch.setattr(everyday_manifest, "LOCAL_CALL_TIMEOUT_SECONDS", 0.04)

    async def main():
        sent = asyncio.Queue()
        transport = LocalToolTransport(["get_reachy_status"], sent.put)
        deadline = asyncio.get_running_loop().time() + 1
        current = [True]

        def execute():
            return asyncio.create_task(
                transport.execute("get_reachy_status", {}, deadline=deadline, current=lambda: current[0])
            )

        task = execute()
        call = await sent.get()
        result = {"type": "everyday.tool_result", "call_id": call["call_id"], "result": {"ok": True}}
        transport.receive(result)
        with pytest.raises(ValueError):
            transport.receive(result)
        assert await task == {"ok": True} and not transport.pending
        with pytest.raises(ValueError):
            transport.receive({**result, "call_id": "unknown"})
        task = execute()
        await sent.get()
        with pytest.raises(TimeoutError):
            await task
        assert not transport.pending
        task = execute()
        call = await sent.get()
        current[0] = False
        with pytest.raises(ValueError):
            transport.receive({**result, "call_id": call["call_id"]})
        transport.close()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not transport.pending and not transport.current_checks

    asyncio.run(main())


def run_host(test, monkeypatch=None, upstream=None):
    module = load_bridge_module()

    async def main():
        bridge = module.Bridge(api_key="bridge-secret", hermes_url="http://127.0.0.1:1")
        bridge.http = aiohttp.ClientSession()
        app = web.Application()
        app.router.add_post("/v1/agent/session", bridge.broker_session)
        app.router.add_get("/v1/everyday/session", bridge.everyday_session)
        app.router.add_post("/v1/everyday/vision", bridge.everyday_vision)
        app.router.add_get("/v1/live", bridge.live)
        try:
            async with TestClient(TestServer(app)) as client:
                await session(client, ctx(27, "conversation"))
                await test(bridge, client, module)
        finally:
            await bridge.http.close()

    asyncio.run(main())


def start(names=None, **changes):
    return {
        "type": "everyday.start",
        "text": "Check Reachy",
        "language": "en",
        "session_id": "turn-1",
        "agent_context": ctx(27, "conversation"),
        "local_tools": names or ["get_reachy_status"],
        **changes,
    }


@pytest.mark.parametrize("name,args", VALID)
def test_authenticated_pipeline_roundtrip_canonical(name, args):
    async def test(bridge, client, module):
        with pytest.raises(aiohttp.WSServerHandshakeError) as failure:
            await client.ws_connect("/v1/everyday/session")
        assert failure.value.status == 401
        seen = []

        async def reasoner(text, **options):
            assert options["local_tools"] == local_tool_specs([name])
            seen.append(await options["local_executor"](name, args))
            return {"text": "Measured result"}

        bridge.everyday_llm = object()
        bridge._everyday_key = lambda: "unused"
        bridge.everyday.answer = reasoner
        async with client.ws_connect("/v1/everyday/session", headers=HEADERS) as ws:
            await ws.send_json(start([name]))
            call = await ws.receive_json(timeout=2)
            assert call["type"] == "everyday.tool_call" and call["name"] == name and call["arguments"] == args
            await ws.send_json(
                {"type": "everyday.tool_result", "call_id": call["call_id"], "result": {"ok": True, "verified": True}}
            )
            assert await ws.receive_json(timeout=2) == {"type": "everyday.answer", "text": "Measured result"}
        assert seen == [{"ok": True, "verified": True}]

    run_host(test)


@pytest.mark.parametrize(
    "change", ["unknown_result", "duplicate_result", "generation", "same_generation", "disconnect"]
)
def test_pipeline_rejects_late_unknown_duplicate_and_teardown(change):
    async def test(bridge, client, module):
        cancelled = asyncio.Event()

        async def reasoner(text, **options):
            try:
                await options["local_executor"]("get_reachy_status", {})
                await asyncio.sleep(10)
                return {"text": "must not escape"}
            finally:
                cancelled.set()

        bridge.everyday_llm = object()
        bridge.everyday.answer = reasoner
        bridge._everyday_key = lambda: "unused"
        async with client.ws_connect("/v1/everyday/session", headers=HEADERS) as ws:
            await ws.send_json(start())
            call = await ws.receive_json(timeout=2)
            result = {"type": "everyday.tool_result", "call_id": call["call_id"], "result": {"ok": True}}
            if change == "disconnect":
                await ws.close()
            elif change in {"generation", "same_generation"}:
                await session(
                    client, ctx(28 if change == "generation" else 27, "conversation", adult_ui_unlocked=True)
                )
            else:
                if change == "unknown_result":
                    result["call_id"] = "unknown"
                await ws.send_json(result)
                if change == "duplicate_result":
                    await ws.send_json(result)
            if change != "disconnect":
                assert (await ws.receive_json(timeout=2))["type"] == "everyday.error"
            await asyncio.wait_for(cancelled.wait(), 2)

    run_host(test)


def test_pipeline_total_deadline_cancels_model(monkeypatch):
    async def test(bridge, client, module):
        monkeypatch.setattr(module, "EVERYDAY_TURN_TIMEOUT_SECONDS", 0.05)
        cancelled = asyncio.Event()

        async def reasoner(text, **options):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        bridge.everyday_llm = object()
        bridge.everyday.answer = reasoner
        bridge._everyday_key = lambda: "unused"
        async with client.ws_connect("/v1/everyday/session", headers=HEADERS) as ws:
            await ws.send_json(start())
            assert (await ws.receive_json(timeout=2))["type"] == "everyday.error"
            await asyncio.wait_for(cancelled.wait(), 2)

    run_host(test)


def vision_body(**changes):
    return {
        "image": base64.b64encode(b"\xff\xd8synthetic-jpeg\xff\xd9").decode(),
        "purpose": "Read this label",
        "agent_context": ctx(27, "conversation"),
        **changes,
    }


def test_vision_narrow_provider_and_original_generation(monkeypatch):
    async def test(bridge, client, module):
        calls = []
        entered, release = asyncio.Event(), asyncio.Event()

        async def complete(body, **kwargs):
            calls.append(body)
            entered.set()
            await release.wait()
            return {"choices": [{"message": {"content": "A blue box"}}]}

        bridge.everyday_llm = SimpleNamespace(
            model=lambda purpose: "fake-vision", request=lambda b: b, complete=complete
        )
        bridge._everyday_key = lambda: "unused"
        denied = await client.post("/v1/everyday/vision", json=vision_body())
        assert denied.status == 401
        pending = asyncio.create_task(client.post("/v1/everyday/vision", headers=HEADERS, json=vision_body()))
        await asyncio.wait_for(entered.wait(), 2)
        await session(client, ctx(28, "conversation"))
        release.set()
        response = await pending
        assert response.status == 403
        fresh = await client.post(
            "/v1/everyday/vision", headers=HEADERS, json=vision_body(agent_context=ctx(28, "conversation"))
        )
        assert fresh.status == 200 and await fresh.json() == {"text": "A blue box"}
        assert fresh.headers["Cache-Control"] == "no-store"
        assert len(calls) == 2
        assert all("tools" not in body and "tool_choice" not in body for body in calls)
        assert calls[0]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    run_host(test)


@pytest.mark.parametrize(
    "changes",
    [
        {"purpose": "x" * 301},
        {"image": "bad"},
        {"image": ""},
        {"image": base64.b64encode(b"\xff\xd8" + b"x" * 1_000_000 + b"\xff\xd9").decode()},
        {"agent_context": ctx(27, "agent")},
        {"extra": "unsupported"},
    ],
)
def test_vision_refuses_invalid_before_provider(changes):
    async def test(bridge, client, module):
        async def forbidden(*a, **kw):
            pytest.fail("invalid request reached provider")

        bridge.everyday_llm = SimpleNamespace(complete=forbidden)
        bridge._everyday_key = lambda: "unused"
        response = await client.post("/v1/everyday/vision", headers=HEADERS, json=vision_body(**changes))
        assert response.status in {400, 403}

    run_host(test)


def test_live_authenticated_local_rpc_does_not_reach_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")

    async def test(bridge, client, module):
        async def script(ws, received):
            await ws.send_json(delta("user", 0, 500, "Check your speaker volume"))
            await ws.send_json(
                {
                    "type": "session.delegation.created",
                    "offset_ms": 500,
                    "delegation": {"id": "del-1", "target": "client"},
                }
            )

        fake = FakeLive(script)
        async with TestServer(fake.app()) as upstream:
            real_serve = live_transport.serve_live

            async def relay(request, **kwargs):
                return await real_serve(
                    request,
                    upstream_url=str(upstream.make_url("/v1/live/sessions")).replace("http", "ws", 1),
                    **kwargs,
                )

            monkeypatch.setitem(bridge.live.__func__.__globals__, "serve_live", relay)
            seen = []

            async def reasoner(text, **options):
                result = await options["local_executor"]("get_reachy_volume", {})
                seen.append(result)
                return {"text": "The measured volume is 42"}

            bridge.everyday.answer = reasoner
            bridge.everyday_llm = object()
            bridge._everyday_key = lambda: "unused"
            events = []
            async with client.ws_connect("/v1/live", headers=HEADERS) as ws:
                await ws.send_json(
                    {
                        "type": "session.start",
                        "assistant_mode": "everyday",
                        "model": "gpt-live-1",
                        "voice": "marin",
                        "agent_context": ctx(27, "conversation"),
                        "local_tools": ["get_reachy_volume"],
                    }
                )
                await collect(ws, events, lambda p: p["type"] == "everyday.tool_call")
                call = events[-1]
                await ws.send_json(
                    {"type": "everyday.tool_result", "call_id": call["call_id"], "result": {"ok": True, "volume": 42}}
                )
                await collect(ws, events, lambda p: p.get("state") == "answered")
                await ws.send_json({"type": "session.close"})
                await collect(ws, events, lambda p: p["type"] == "session.closed")
            assert seen == [{"ok": True, "volume": 42}]
            assert not any(p["type"].startswith("everyday.") for p in fake.received)
            comments = [p for p in fake.received if p["type"] == "session.commentary.append"]
            assert comments[0]["content"] == "The measured volume is 42"

    run_host(test)


@pytest.mark.parametrize("variant", ["stop", "generation", "same_generation", "revision", "unknown"])
def test_live_local_pending_cancelled_and_never_published_after_fence(monkeypatch, variant):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")

    async def test(bridge, client, module):
        newer = asyncio.Event()
        cancelled = asyncio.Event()
        observed = []

        async def script(ws, received):
            await ws.send_json(delta("user", 0, 500, "Check your speaker volume"))
            await ws.send_json(
                {
                    "type": "session.delegation.created",
                    "offset_ms": 500,
                    "delegation": {"id": "del-1", "target": "client"},
                }
            )
            if variant == "revision":
                await newer.wait()
                await ws.send_json(delta("user", 600, 900, "Check your status instead"))
                await ws.send_json(
                    {
                        "type": "session.delegation.created",
                        "offset_ms": 900,
                        "delegation": {"id": "del-2", "target": "client"},
                    }
                )
                # The relay has processed del-2 before forwarding this ordered marker.
                await ws.send_json(delta("assistant", 1000, 1100, "revision observed"))

        fake = FakeLive(script)
        async with TestServer(fake.app()) as upstream:
            real_serve = live_transport.serve_live

            async def relay(request, **kwargs):
                return await real_serve(
                    request,
                    upstream_url=str(upstream.make_url("/v1/live/sessions")).replace("http", "ws", 1),
                    **kwargs,
                )

            monkeypatch.setitem(bridge.live.__func__.__globals__, "serve_live", relay)

            async def reasoner(text, **options):
                try:
                    result = await options["local_executor"]("get_reachy_volume", {})
                    observed.append(result)
                    return {"text": "must never be spoken"}
                finally:
                    cancelled.set()

            bridge.everyday.answer = reasoner
            bridge.everyday_llm = object()
            bridge._everyday_key = lambda: "unused"
            events = []
            async with client.ws_connect("/v1/live", headers=HEADERS) as ws:
                await ws.send_json(
                    {
                        "type": "session.start",
                        "assistant_mode": "everyday",
                        "model": "gpt-live-1",
                        "voice": "marin",
                        "agent_context": ctx(27, "conversation"),
                        "local_tools": ["get_reachy_volume"],
                    }
                )
                await collect(ws, events, lambda p: p["type"] == "everyday.tool_call")
                call = events[-1]
                if variant == "stop":
                    await ws.send_json({"type": "session.close"})
                elif variant in {"generation", "same_generation"}:
                    await session(
                        client, ctx(28 if variant == "generation" else 27, "conversation", adult_ui_unlocked=True)
                    )
                else:
                    if variant == "revision":
                        newer.set()
                        await collect(ws, events, lambda p: p.get("delta") == "revision observed")
                    await ws.send_json(
                        {
                            "type": "everyday.tool_result",
                            "call_id": "unknown" if variant == "unknown" else call["call_id"],
                            "result": {"ok": True, "volume": 42},
                        }
                    )
                await collect(ws, events, lambda p: p["type"] == "session.closed")
                await asyncio.wait_for(cancelled.wait(), 2)
            assert observed == []
            assert not any(p["type"] == "session.commentary.append" for p in fake.received)
            assert not any(p["type"].startswith("everyday.") for p in fake.received)

    run_host(test)
