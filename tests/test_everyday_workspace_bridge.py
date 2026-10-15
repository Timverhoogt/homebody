"""Bridge routing for Everyday/Workspace against loopback fakes (OpenAI WS, Hermes runs API)."""

import asyncio
import json
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_bridge import load_bridge_module

from companion.native_adapters import HermesNativeAdapter
from companion.native_workspace import NativeWorkspace, TargetConfig, WorkspaceError
from companion.reachy_projects import Project, ProjectCatalog

HEADERS = {"Authorization": "Bearer bridge-secret", "X-Reachy-Device-Id": "reachy-1"}


def ctx(generation, profile="agent", **changes):
    value = {
        "capability_profile": profile, "adult_ui_unlocked": profile == "agent", "kids_mode_active": False,
        "power_mode": "awake", "privacy_enabled": True, "emergency_stop_active": False, "robot_available": True,
        "session_generation": generation, "requested_session_generation": generation,
        "explicit_private_intent": False, "reachy_status": {},
    }
    value.update(changes)
    return value


def native(tmp_path, *, discussion=True):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ROADMAP.md").write_text("Next: import readiness")
    projects = ProjectCatalog((Project("photo", "Photo", root, Path("ROADMAP.md")),))
    targets = TargetConfig.parse({"photo": {
        "backend": "hermes", "url": "http://127.0.0.1:8642", "token_env": "TEST_NATIVE_TOKEN",
        "project_id": "photo", "session_id": "native-existing", "discussion_enabled": discussion,
    }}, projects)
    return projects, targets


class Adapter:
    def __init__(self):
        self.calls = []
        self.enforced = False  # the handshake proves contract v1 only when set

    async def history(self, target):
        return [{"role": "assistant", "text": "private native history"}]

    async def discussion_handshake(self, target):
        if not self.enforced:
            raise WorkspaceError("Native voice discussion is not available")
        return "default"

    async def discuss(self, target, text, request_id, *, profile):
        self.calls.append(("discuss", target.session_id, text, profile))
        return {"run_id": "run-d1", "status": "completed", "output": "From your Hermes session.",
                "tool_calls_refused": 0}

    async def stop(self, target, run_id):
        self.calls.append(("stop", run_id))
        return "stopping"

    async def close(self):
        pass


def test_discussion_flag_is_hermes_only_and_keeps_legacy_fingerprint(tmp_path):
    projects, targets = native(tmp_path, discussion=False)
    assert "discussion_enabled" not in targets["photo"].config_dict()
    with pytest.raises(WorkspaceError, match="Hermes sessions only"):
        TargetConfig.parse({"oc": {"backend": "openclaw", "url": "ws://127.0.0.1:1", "token_env": "TOKEN_X",
                                   "project_id": "photo", "session_id": "s1", "discussion_enabled": True}}, projects)


CAPABILITY = {"version": 1, "request_field": "hermes_discussion", "mode": "discussion", "tools": "none",
              "enforcement": "dispatch", "history": "session", "profile": "default"}
POLICY = {"version": 1, "mode": "discussion", "tools": "none", "session_id": "native-existing", "profile": "default"}
DIGEST = __import__("hashlib").sha256(json.dumps(POLICY, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
ECHO = {**POLICY, "enforcement": "dispatch", "policy_digest": DIGEST, "tool_calls_refused": 0}


def fake_hermes(*, capability=CAPABILITY, admitted_echo=ECHO, status_echo=ECHO, session="native-existing",
                post_delay=0.0):
    """Loopback Hermes stand-in for the wire contract; returns (app, seen)."""
    seen = {"posts": [], "gets": 0, "stops": 0}

    async def capabilities(request):
        features = {"run_submission": True}
        if capability is not None:
            features["native_discussion"] = capability
        return web.json_response({"features": features})

    async def runs(request):
        seen["posts"].append(await request.json())
        await asyncio.sleep(post_delay)
        body = {"run_id": "run-d1", "status": "started"}
        if admitted_echo is not None:
            body["discussion"] = admitted_echo
        return web.json_response(body, status=202)

    async def status(request):
        seen["gets"] += 1
        body = {"run_id": "run-d1", "status": "completed", "session_id": session, "output": "Item two is next."}
        if status_echo is not None:
            body["discussion"] = status_echo
        return web.json_response(body)

    async def stop(request):
        seen["stops"] += 1
        return web.json_response({"run_id": "run-d1", "status": "stopping"})

    app = web.Application()
    app.router.add_get("/v1/capabilities", capabilities)
    app.router.add_post("/v1/runs", runs)
    app.router.add_get("/v1/runs/{run_id}", status)
    app.router.add_post("/v1/runs/{run_id}/stop", stop)
    return app, seen


def with_fake_hermes(app, scenario):
    async def main():
        server = TestServer(app)
        await server.start_server()
        target = TargetConfig("photo", "hermes", str(server.make_url("")).rstrip("/"), "TEST_NATIVE_TOKEN",
                              "photo", "native-existing", discussion_enabled=True)
        try:
            async with aiohttp.ClientSession() as http:
                await scenario(HermesNativeAdapter(http, lambda name: "native-token"), target)
        finally:
            await server.close()

    asyncio.run(main())


def test_hermes_discussion_sends_exact_policy_and_verifies_every_echo():
    app, seen = fake_hermes()

    async def scenario(adapter, target):
        profile = await adapter.discussion_handshake(target)
        result = await adapter.discuss(target, "What is next?", "req-1", profile=profile, poll=0.01)
        assert result == {"run_id": "run-d1", "status": "completed", "output": "Item two is next.",
                          "tool_calls_refused": 0}

    with_fake_hermes(app, scenario)
    # Only the user message and the exact policy: no instructions, history, model or tool overrides.
    assert seen["posts"] == [{"session_id": "native-existing", "input": "What is next?", "hermes_discussion": POLICY}]


@pytest.mark.parametrize("capability", [
    None,  # an installed runtime without the contract
    {**CAPABILITY, "version": 2},
    {**CAPABILITY, "version": True},
    {**CAPABILITY, "tools": "read_only"},
    {**CAPABILITY, "enforcement": "prompt"},
    {**CAPABILITY, "extra": 1},
    {k: v for k, v in CAPABILITY.items() if k != "profile"},
    {**CAPABILITY, "profile": "../work"},
])
def test_unsupported_or_ambiguous_handshake_fails_closed_before_any_run(capability):
    app, seen = fake_hermes(capability=capability)

    async def scenario(adapter, target):
        with pytest.raises(WorkspaceError, match="did not confirm"):
            await adapter.discussion_handshake(target)

    with_fake_hermes(app, scenario)
    assert seen["posts"] == [] and seen["gets"] == 0


@pytest.mark.parametrize(("admitted", "status", "session", "error"), [
    (None, ECHO, "native-existing", "did not confirm"),  # admitted as an ordinary (tool-capable) run
    ({**ECHO, "tools": "all"}, ECHO, "native-existing", "did not confirm"),
    ({**ECHO, "policy_digest": "0" * 64}, ECHO, "native-existing", "did not confirm"),
    ({**ECHO, "profile": "work"}, ECHO, "native-existing", "did not confirm"),
    (ECHO, None, "native-existing", "did not confirm"),  # status lost the policy
    (ECHO, ECHO, "other-session", "different session"),  # answered from the wrong session
])
def test_run_without_the_exact_echo_is_stopped_and_refused(admitted, status, session, error):
    app, seen = fake_hermes(admitted_echo=admitted, status_echo=status, session=session)

    async def scenario(adapter, target):
        with pytest.raises(WorkspaceError, match=error):
            await adapter.discuss(target, "What is next?", "req-1", profile="default", poll=0.01)

    with_fake_hermes(app, scenario)
    assert seen["stops"] == 1  # the admitted run is stopped, its output never returned


def test_cancel_while_admission_is_in_flight_stops_the_admitted_run():
    app, seen = fake_hermes(post_delay=0.3)

    async def scenario(adapter, target):
        turn = asyncio.create_task(adapter.discuss(target, "What is next?", "req-1", profile="default", poll=0.01))
        while not seen["posts"]:
            await asyncio.sleep(0.01)
        turn.cancel()  # Stop before the 202 arrives
        with pytest.raises(asyncio.CancelledError):
            await turn
        for _ in range(100):
            if seen["stops"]:
                break
            await asyncio.sleep(0.01)

    with_fake_hermes(app, scenario)
    assert seen["stops"] == 1 and seen["gets"] == 0  # identified, stopped, never polled for an answer


def run_bridge(tmp_path, test, *, discussion=True):
    bridge_module = load_bridge_module()

    async def main():
        bridge = bridge_module.Bridge(api_key="bridge-secret", hermes_url="http://127.0.0.1:1")
        projects, targets = native(tmp_path, discussion=discussion)
        adapter = Adapter()
        bridge.native_workspace = NativeWorkspace(projects, targets, {"hermes": adapter}, tmp_path / "j.json")
        app = web.Application()
        app.router.add_post("/v1/agent/session", bridge.broker_session)
        app.router.add_post("/v1/agent/native/{action}", bridge.native_workspace_action)
        app.router.add_post("/v1/agent/ask", bridge.broker_ask)
        app.router.add_post("/v1/everyday/turn", bridge.everyday_turn)
        app.router.add_get("/v1/everyday/capabilities", bridge.everyday_capabilities)
        bridge.http = aiohttp.ClientSession()
        async with TestClient(TestServer(app)) as client:
            try:
                await test(bridge, client, adapter)
            finally:
                await bridge.http.close()

    asyncio.run(main())


async def session(client, context, **flags):
    response = await client.post("/v1/agent/session", headers=HEADERS, json={"context": context, **flags})
    assert response.status == 200, await response.text()


def test_switch_to_everyday_parks_work_and_teardown_still_revokes(tmp_path):
    async def test(bridge, client, adapter):
        manager = bridge.native_workspace
        await session(client, ctx(1))
        await manager.bind("reachy-1", "photo")
        manager.bindings["reachy-1"]["run"] = {"status": "running", "run_id": "run-exact"}
        await session(client, ctx(2, "conversation"), park_native=True)  # explicit owner switch
        assert manager.parked("reachy-1") and adapter.calls == []
        badge = await (await client.post("/v1/agent/native/badge", headers=HEADERS, json={})).json()
        assert badge["running"] is True and "native-existing" not in json.dumps(badge)
        hidden = await manager.snapshot("reachy-1")
        assert hidden["messages"] == [] and hidden["binding"] is None and hidden["parked"] is True
        with pytest.raises(WorkspaceError, match="Switch to Workspace"):
            await manager.discuss("reachy-1", "what next")  # Everyday gets no native authority
        await session(client, ctx(3, "conversation"), preserve_native=True)  # next Everyday wake
        assert manager.parked("reachy-1") and not manager.bindings["reachy-1"].get("detached")
        await session(client, ctx(4))  # back to Workspace
        assert not manager.parked("reachy-1")
        await session(client, ctx(5, "conversation"), park_native=True)
        await session(client, ctx(6, "conversation", kids_mode_active=True), park_native=True)
        assert manager.bindings["reachy-1"]["detached"] is True
        assert ("stop", "run-exact") in adapter.calls  # Kids keeps revoking running work

    run_bridge(tmp_path, test)


def test_plain_profile_change_without_park_still_detaches(tmp_path):
    async def test(bridge, client, adapter):
        await session(client, ctx(1))
        await bridge.native_workspace.bind("reachy-1", "photo")
        await session(client, ctx(2, "conversation"))
        assert bridge.native_workspace.bindings["reachy-1"]["detached"] is True

    run_bridge(tmp_path, test)


def test_verified_discussion_answers_from_the_native_session(tmp_path):
    async def test(bridge, client, adapter):
        adapter.enforced = True
        await session(client, ctx(1))
        await bridge.native_workspace.bind("reachy-1", "photo")
        response = await client.post("/v1/agent/ask", headers=HEADERS, json={
            "request_id": "agent-0123456789", "request": "Tell me about it", "context": ctx(1)})
        body = await response.json()
        assert body["text"] == "From your Hermes session."
        assert body["source"]["kind"] == "native_hermes" and body["source"]["run_id"] == "run-d1"
        assert body["source"]["enforcement"] == "dispatch"
        assert adapter.calls == [("discuss", "native-existing", "Tell me about it", "default")]
        snapshot = await bridge.native_workspace.snapshot("reachy-1")
        assert snapshot["binding"]["voice_discussion"] is True

    run_bridge(tmp_path, test)


def test_enabled_discussion_gives_truthful_not_ready_without_native_run_or_broker(tmp_path):
    async def test(bridge, client, adapter):
        broker_turns = []

        async def broker_turn(*args, **kwargs):
            broker_turns.append(args)
            return "Restricted roadmap summary."

        bridge._agent_answer_turn = broker_turn
        await session(client, ctx(1))
        await bridge.native_workspace.bind("reachy-1", "photo")
        assert bridge.native_workspace.targets["photo"].discussion_enabled is True
        response = await client.post("/v1/agent/ask", headers=HEADERS, json={
            "request_id": "agent-0123456789", "request": "Tell me about it", "context": ctx(1)})
        body = await response.json()
        assert body["source"] == {"kind": "native_not_ready", "state": "discussion_unenforced"}
        assert "Nothing was sent" in body["text"] and "no tools" in body["text"]
        assert "From your Hermes session." not in body["text"]
        assert "native_hermes" not in json.dumps(body) and "run-d1" not in json.dumps(body)
        assert adapter.calls == []  # no native discussion run
        assert broker_turns == []  # and no ordinary-chat fallback to the restricted broker
        snapshot = await bridge.native_workspace.snapshot("reachy-1")
        assert snapshot["binding"]["voice_discussion"] is False

    run_bridge(tmp_path, test)


def test_without_host_discussion_the_broker_answer_is_labelled_not_native(tmp_path):
    async def test(bridge, client, adapter):
        async def broker_turn(*args, **kwargs):
            return "Restricted roadmap summary."

        bridge._agent_answer_turn = broker_turn
        await session(client, ctx(1))
        await bridge.native_workspace.bind("reachy-1", "photo")
        response = await client.post("/v1/agent/ask", headers=HEADERS, json={
            "request_id": "agent-0123456789", "request": "Tell me about the project roadmap", "context": ctx(1)})
        body = await response.json()
        assert body["text"] == "Restricted roadmap summary."
        assert body["source"]["kind"] == "restricted_broker" and "not your Hermes" in body["source"]["label"]
        # Ordinary chat is not silently rerouted to the restricted reader: an actionable setup outcome.
        response = await client.post("/v1/agent/ask", headers=HEADERS, json={
            "request_id": "agent-0123456790", "request": "Tell me about it", "context": ctx(1)})
        body = await response.json()
        assert body["source"] == {"kind": "native_not_ready", "state": "discussion_disabled"}
        assert "Nothing was sent" in body["text"]
        assert adapter.calls == []

    run_bridge(tmp_path, test, discussion=False)


def test_everyday_routes_are_narrow_and_disclose_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("REACHY_EVERYDAY_WEATHER_PROVIDER", "open-meteo")
    monkeypatch.setenv("REACHY_EVERYDAY_WEATHER_LATLON", "52.37,4.89")

    async def test(bridge, client, adapter):
        capabilities = await (await client.get("/v1/everyday/capabilities", headers=HEADERS)).json()
        assert capabilities["available"] is False and "key" in capabilities["error"]
        assert capabilities["weather"]["location_leaves_home"] is True
        assert "52.37" not in json.dumps(capabilities) and "sk-" not in json.dumps(capabilities)
        unavailable = await client.post("/v1/everyday/turn", headers=HEADERS, json={"text": "hi"})
        assert unavailable.status == 503
        bad = await client.post("/v1/everyday/turn", headers=HEADERS, json={"text": "hi", "model": "hermes-agent"})
        assert bad.status == 403
        seen = []

        async def answer(text, **kwargs):
            seen.append((text, kwargs["device_id"]))
            return {"text": "It is 11:30.", "tools": [], "source": {"kind": "everyday"}}

        bridge.everyday.answer = answer
        ok = await client.post("/v1/everyday/turn", headers=HEADERS, json={"text": "time?", "session_id": "s"})
        assert (await ok.json())["text"] == "It is 11:30." and seen == [("time?", "reachy-1")]
        bridge._kids_live["kids"] = 9e18
        bridge.kids_session_live = lambda: True
        kids = await client.post("/v1/everyday/turn", headers=HEADERS, json={"text": "time?"})
        assert kids.status == 403 and len(seen) == 1

    run_bridge(tmp_path, test)


def test_realtime_everyday_session_has_no_hermes_tools_and_runs_host_tools(monkeypatch):
    bridge_module = load_bridge_module()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    received = []
    output_seen = asyncio.Event

    async def main():
        got_output = output_seen()

        async def openai(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            received.append(await ws.receive_json())
            await ws.send_json({"type": "response.output_item.done", "item": {
                "type": "function_call", "status": "completed", "name": "get_current_time",
                "call_id": "call-1", "arguments": "{}"}})
            async for message in ws:
                payload = json.loads(message.data)
                received.append(payload)
                if payload.get("item", {}).get("type") == "function_call_output":
                    got_output.set()
            return ws

        upstream_app = web.Application()
        upstream_app.router.add_get("/v1/realtime", openai)
        upstream = TestServer(upstream_app)
        await upstream.start_server()
        fake_url = str(upstream.make_url("/v1/realtime")).replace("http", "ws", 1)

        class Redirect(aiohttp.ClientSession):
            def ws_connect(self, url, **kwargs):
                assert url.startswith("wss://api.openai.com/v1/realtime")
                return super().ws_connect(fake_url, **kwargs)

        monkeypatch.setattr(bridge_module, "ClientSession", Redirect)
        bridge = bridge_module.Bridge(api_key="bridge-secret", hermes_url="http://127.0.0.1:1")
        bridge.http = aiohttp.ClientSession()
        app = web.Application()
        app.router.add_get("/v1/realtime", bridge.realtime)
        try:
            async with TestClient(TestServer(app)) as client:
                ws = await client.ws_connect("/v1/realtime", headers=HEADERS)
                await ws.send_json({"type": "session.start", "assistant_mode": "everyday",
                                    "semantic_eagerness": "high", "agent_tools_enabled": True,
                                    "power_tools_enabled": True, "agent_context": ctx(1, "conversation")})
                await asyncio.wait_for(got_output.wait(), 5)
                await ws.send_json({"type": "session.stop"})
                await ws.close()
        finally:
            await bridge.http.close()
            await upstream.close()

    asyncio.run(main())
    update = received[0]["session"]
    names = {tool["name"] for tool in update["tools"]}
    assert {"get_current_time", "get_weather", "home_control", "set_reachy_power_mode"} <= names
    assert not names & {"ask_hermes", "ask_openclaw"}
    assert update["audio"]["input"]["turn_detection"]["eagerness"] == "high"
    assert "Everyday" in update["instructions"]
    output = next(p for p in received if p.get("item", {}).get("type") == "function_call_output")
    assert output["item"]["call_id"] == "call-1" and json.loads(output["item"]["output"])["ok"] is True


def test_legacy_tool_surface_unchanged_and_everyday_parser_requires_completed_known_calls():
    bridge_module = load_bridge_module()
    tools = bridge_module._build_realtime_tools(False, False, True, False)
    assert [tool["name"] for tool in tools] == ["ask_hermes"]  # legacy robots: unchanged surface
    assert bridge_module._completed_everyday_call("response.output_item.done", {"item": {
        "type": "function_call", "status": "in_progress", "name": "home_control", "call_id": "c"}}) is None
    assert bridge_module._completed_everyday_call("response.output_item.done", {"item": {
        "type": "function_call", "status": "completed", "name": "ask_hermes", "call_id": "c"}}) is None
