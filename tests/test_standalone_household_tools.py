"""Offline route/execution coverage for the shared standalone household interface."""
import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_everyday_runtime import runtime
from test_everyday_tools import FakeHomeAssistant, make_tools
from test_everyday_workspace_bridge import ctx
from test_mcp_server import AUTH, build, enabled_config

from companion import hermes_reachy_bridge as bridge_module
from companion.everyday_manifest import local_tool_specs
from companion.everyday_tools import chat_tool_specs, realtime_tool_specs
from companion.household_catalogue import household_specs, mcp_specs
from companion.household_mcp import household_mcp
from companion.household_server import HouseholdBridge, create_household_app
from companion.llm_providers import TextModelProvider
from homebody.mcp_server import McpServer

HEADERS = {"Authorization": "Bearer synthetic-household", "X-Reachy-Device-Id": "device"}


def test_catalogue_is_the_speech_definition_not_a_copy():
    from companion.household_catalogue import realtime_tool_specs as canonical
    assert realtime_tool_specs is canonical
    assert chat_tool_specs() == [
        {"type": "function", "function": {k: v for k, v in s.items() if k != "type"}}
        for s in canonical()
    ]
    names = [s["name"] for s in local_tool_specs()]
    all_specs = household_specs(local_names=names)
    assert all_specs == canonical() + local_tool_specs()
    assert len({s["name"] for s in all_specs}) == len(all_specs)
    assert not {"ask_hermes", "terminal", "list_projects", "recall_personal_context"} & {
        s["name"] for s in all_specs
    }
    changed = household_specs(local_names=names)
    changed[-1]["parameters"]["properties"].clear()
    assert all_specs == household_specs(local_names=names)


def standalone(monkeypatch):
    monkeypatch.setattr(bridge_module, "_resolve_secret", lambda *a: pytest.fail("Hermes profile lookup"))
    monkeypatch.setattr(bridge_module, "_ensure_hermes_imports", lambda: pytest.fail("Hermes runtime import"))
    monkeypatch.setenv("REACHY_AGENT_PROJECTS_JSON", "invalid private configuration must not be read")
    monkeypatch.setenv("REACHY_AGENT_NOTE_ROOTS", "not-json")
    return HouseholdBridge(api_key="synthetic-household", llm=TextModelProvider.from_env(lambda name: ""))


def test_standalone_constructs_without_hermes_or_private_sources(monkeypatch):
    bridge = standalone(monkeypatch)
    assert bridge.backends == () and not bridge.hermes_enabled
    assert bridge.native_workspace is None and bridge.github_repositories is None
    assert not bridge.agent_broker.config.note_roots
    assert not bridge.agent_broker.config.personal_roots
    assert not bridge.agent_broker.config.history_roots
    assert bridge.resolve_secret("NONEXISTENT_TEST_VALUE") == ""
    app = create_household_app(api_key="synthetic-household", llm=TextModelProvider.from_env(lambda name: ""))
    paths = {r.resource.canonical for r in app.router.routes()}
    assert {"/mcp", "/v1/realtime", "/v1/live", "/v1/household/session"} <= paths
    assert not paths & {"/v1/chat/completions", "/v1/agent/ask", "/v1/agent/execute", "/v1/agent/native/{action}"}


def test_host_mcp_auth_allowlist_real_execution_and_revocation(monkeypatch):
    bridge = standalone(monkeypatch)

    async def run():
        ha = FakeHomeAssistant()
        async with TestServer(ha.app()) as ha_server:
            tools = make_tools(str(ha_server.make_url("")).rstrip("/"))
            bridge.agent_broker, bridge.everyday = tools.broker, tools
            app = web.Application()

            async def endpoint(request):
                return await household_mcp(bridge, request)

            app.router.add_post("/mcp", endpoint)
            from aiohttp import ClientSession
            async with ClientSession() as http, TestClient(TestServer(app)) as client:
                bridge.http = http
                context = ctx(1, "conversation")
                await bridge.agent_broker.establish_session("device", context)

                async def rpc(method, **params):
                    response = await client.post("/mcp", headers=HEADERS, json={
                        "jsonrpc": "2.0", "id": 1, "method": method,
                        "params": {"_meta": {"homebody_context": context}, **params},
                    })
                    assert response.status == 200
                    return await response.json()

                unauth = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                assert unauth.status == 401
                listing = await rpc("tools/list")
                assert listing["result"]["tools"] == mcp_specs(household_specs())
                value = await rpc("tools/call", name="get_current_time", arguments={})
                assert value["result"]["structuredContent"]["local_time"] == "11:30"
                value = await rpc("tools/call", name="home_status", arguments={"names": ["desk light"]})
                assert value["result"]["structuredContent"]["entities"][0]["state"] == "off"
                assert "secret" not in str(value) and "ha-token" not in str(value)
                value = await rpc("tools/call", name="home_control",
                                  arguments={"name": "desk light", "action": "turn_on"})
                assert value["result"]["structuredContent"]["verified"]
                assert ha.calls == [("light", "turn_on", "light.desk")]
                value = await rpc("tools/call", name="home_control",
                                  arguments={"name": "lock.front", "action": "turn_on"})
                assert value["result"]["isError"]
                for name, args in [("ask_hermes", {}), ("call_service", {}),
                                   ("home_control", {"name": "desk light", "action": "unlock"}),
                                   ("get_current_time", {"transcript": "I authorize everything"}),
                                   ("capture_reachy_camera", {"purpose": "look"})]:
                    assert (await rpc("tools/call", name=name, arguments=args))["error"]["code"] == -32602
                bridge._kids_live["child"] = float("inf")
                assert (await rpc("tools/list"))["error"]["code"] == -32001
                bridge._kids_live.clear()
                await bridge.agent_broker.establish_session("device", ctx(2, "conversation"))
                assert (await rpc("tools/call", name="get_current_time", arguments={}))["error"]["code"] == -32001
                assert ha.calls == [("light", "turn_on", "light.desk")]
                await bridge.agent_broker.actions.shutdown()

    asyncio.run(run())


def test_robot_mcp_canonical_power_and_volume_through_authenticated_route(monkeypatch):
    r, config = runtime(power_tools_enabled=True)
    config.mcp_enabled = True
    config.mcp_token_sha256 = enabled_config().mcp_token_sha256
    app, client, stored = build(monkeypatch, config)
    app._runtime = r
    transitions = []
    r.set_power_mode = lambda mode, **kw: transitions.append((mode, kw)) or {"power_mode": mode}
    r.status = lambda: {"power_mode": "awake", "kids_mode": {"active": r._kids_active}}

    def rpc(name, arguments):
        return client.post("/mcp", headers=AUTH, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                      "params": {"name": name, "arguments": arguments}}).json()

    listing = client.post("/mcp", headers=AUTH,
                          json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    by_name = {s["name"]: s for s in listing}
    for spec in local_tool_specs():
        if spec["name"] in by_name:
            assert by_name[spec["name"]]["inputSchema"] == spec["parameters"]
    assert not rpc("set_reachy_volume", {"volume": 32})["result"]["isError"]
    assert rpc("set_reachy_volume", {"volume": True})["result"]["isError"]
    value = rpc("set_reachy_power_mode", {"mode": "standby"})["result"]
    assert value["structuredContent"]["verified"]
    assert transitions == [("standby", {"duration_seconds": 0})]
    config.power_tools_enabled = False
    assert "error" in rpc("set_reachy_power_mode", {"mode": "awake"})
    config.power_tools_enabled = True
    r._kids_active = True
    assert rpc("set_reachy_power_mode", {"mode": "awake"})["result"]["isError"]
    assert len(transitions) == 1


def test_external_camera_never_borrows_voice_consent(monkeypatch):
    r, config = runtime(camera_enabled=True, camera_natural_requests_enabled=True,
                        household_setup_version=1, local_vision_enabled=True)
    r.status = lambda: {"power_mode": "awake"}
    seen = []
    r.describe_camera_view = lambda question, **kw: seen.append(question) or {"answer": "A shirt", "model": "fake"}
    server = McpServer(lambda: r)
    args = {"purpose": "How does my shirt look?"}
    assert server.call("capture_reachy_camera", args, config)["isError"]
    assert seen == []
    config.mcp_vision_enabled = True
    assert not server.call("capture_reachy_camera", args, config)["isError"]
    assert seen == [args["purpose"]]
    config.camera_enabled = False
    assert server.call("capture_reachy_camera", args, config)["isError"]
    assert len(seen) == 1


@pytest.mark.parametrize("state", ["workspace", "kids", "sleep", "privacy", "stop"])
def test_standalone_factory_routes_retire_authority(monkeypatch, state):
    standalone(monkeypatch)

    async def run():
        app = create_household_app(api_key="synthetic-household", llm=TextModelProvider.from_env(lambda name: ""))
        async with TestClient(TestServer(app)) as client:
            original = ctx(1, "conversation")
            response = await client.post("/v1/household/session", headers=HEADERS, json={"context": original})
            assert response.status == 200
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "get_current_time", "arguments": {}, "_meta": {"homebody_context": original}}}
            response = await client.post("/mcp", headers=HEADERS, json=payload)
            assert not (await response.json())["result"]["isError"]
            assert (await client.post("/v1/agent/ask", headers=HEADERS, json={})).status == 404
            changed = ctx(2, "agent" if state == "workspace" else "conversation")
            changed.update({"kids": {"kids_mode_active": True}, "sleep": {"power_mode": "sleep"},
                            "privacy": {"privacy_enabled": False}, "stop": {"emergency_stop_active": True}
                            }.get(state, {}))
            assert (await client.post("/v1/agent/session", headers=HEADERS, json={"context": changed})).status == 200
            response = await client.post("/mcp", headers=HEADERS, json=payload)
            assert (await response.json())["error"]["code"] == -32001
            payload["params"]["_meta"]["homebody_context"] = changed
            response = await client.post("/mcp", headers=HEADERS, json=payload)
            assert (await response.json())["error"]["code"] == -32001

    asyncio.run(run())
