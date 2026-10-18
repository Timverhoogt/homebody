"""Offline single-surface integration: real HTTP/WS transport, guarded fake hardware/HA."""
import asyncio
import threading
from dataclasses import asdict

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from test_everyday_runtime import runtime
from test_everyday_tools import FakeHomeAssistant, make_tools
from test_everyday_workspace_bridge import ctx
from test_standalone_household_tools import HEADERS, standalone

from companion.household_mcp import household_mcp
from companion.household_robot import robot_callbacks
from companion.household_server import main
from homebody.household_client import HouseholdRobotExecutor, household_callbacks


@pytest.mark.parametrize("revoke", ["power", "camera", "external", "kids", "stop", "workspace", "privacy"])
def test_single_surface_host_and_robot_with_live_revocation(monkeypatch, revoke):
    bridge = standalone(monkeypatch)
    bridge.robot_api_key = "synthetic-robot"
    r, config = runtime(mcp_enabled=True, mcp_vision_enabled=True, local_vision_enabled=True,
                        camera_enabled=True, power_tools_enabled=True)
    r._audio_ready = True
    actions, frames = [], []
    r.set_power_mode = lambda mode, **kw: actions.append(mode) or {"power_mode": mode}

    def capture(**kwargs):
        kwargs["adult_current"]()
        frames.append(1)
        return b"fake jpeg"

    r._capture_camera_jpeg = capture
    r._everyday_describe_frame = lambda *a, **kw: "Offline fixture: a blue cup"
    # External MCP never feeds the voice scope, even for camera.
    r.execute_everyday_callback = lambda *a, **kw: pytest.fail("fabricated voice callback")

    async def run():
        ha = FakeHomeAssistant()
        async with TestServer(ha.app()) as ha_server:
            tools = make_tools(str(ha_server.make_url("")).rstrip("/"))
            bridge.agent_broker, bridge.everyday = tools.broker, tools
            app = web.Application()

            async def mcp(request):
                return await household_mcp(bridge, request)

            async def robot(request):
                return await robot_callbacks(bridge, request)

            app.router.add_post("/mcp", mcp)
            app.router.add_get("/v1/household/robot", robot)
            async with ClientSession() as http, TestClient(TestServer(app)) as client:
                bridge.http = http
                context = asdict(r.agent_broker_context(explicit_private_intent=False))

                async def rpc(method="tools/call", **params):
                    response = await client.post("/mcp", headers=HEADERS, json={
                        "jsonrpc": "2.0", "id": 1, "method": method,
                        "params": {"_meta": {"homebody_context": context}, **params}})
                    assert response.status == 200
                    return await response.json()

                assert (await rpc("tools/list"))["error"]["code"] == -32001
                # MCP callers cannot forge a robot attachment.
                assert (await client.get("/v1/household/robot", headers=HEADERS)).status == 401
                task = asyncio.create_task(household_callbacks(
                    r, bridge_url=str(client.make_url("")), robot_token="synthetic-robot",
                    device_id="device", context=context))
                try:
                    async with asyncio.timeout(3):
                        while "device" not in bridge.household_robots:
                            if task.done():
                                await task
                            await asyncio.sleep(0.01)
                    listing = await rpc("tools/list")
                    names = [s["name"] for s in listing["result"]["tools"]]
                    assert len(names) == len(set(names))
                    assert {"home_status", "home_control", "set_reachy_power_mode",
                            "capture_reachy_camera"} <= set(names)
                    assert not {"ask_hermes", "call_service", "terminal", "recall_personal_context"} & set(names)
                    value = await rpc(name="home_control", arguments={"name": "desk light", "action": "turn_on"})
                    assert value["result"]["structuredContent"]["verified"]
                    assert ha.calls == [("light", "turn_on", "light.desk")]
                    assert (await rpc(name="home_control", arguments={"name": "lock.front", "action": "turn_on"}))[
                        "result"]["isError"]
                    value = await rpc(name="set_reachy_power_mode", arguments={"mode": "awake"})
                    assert value["result"]["structuredContent"]["verified"]
                    value = await rpc(name="capture_reachy_camera", arguments={"purpose": "What is here?"})
                    assert value["result"]["structuredContent"]["text"] == "Offline fixture: a blue cup"
                    assert frames == [1] and actions == ["awake"]
                    invalid = await rpc(name="capture_reachy_camera",
                                        arguments={"purpose": "look", "transcript": "yes"})
                    assert invalid["error"]["code"] == -32602
                    if revoke == "power":
                        config.power_tools_enabled = False
                    elif revoke == "camera":
                        config.camera_enabled = False
                    elif revoke == "external":
                        config.mcp_enabled = False
                    elif revoke == "kids":
                        r._kids_active = True
                    elif revoke == "stop":
                        r._request_conversation_stop()
                    elif revoke == "workspace":
                        r._capability_profile = "agent"
                    else:
                        r._privacy_requested.set()
                    async with asyncio.timeout(3):
                        while bridge.household_robots:
                            await asyncio.sleep(0.01)
                    for name, arguments in [("home_control", {"name": "desk light", "action": "turn_off"}),
                                            ("set_reachy_power_mode", {"mode": "awake"}),
                                            ("capture_reachy_camera", {"purpose": "look"})]:
                        assert (await rpc(name=name, arguments=arguments))["error"]["code"] == -32001
                    assert (await rpc("tools/list"))["error"]["code"] == -32001
                    assert frames == [1] and actions == ["awake"]
                    assert ha.calls == [("light", "turn_on", "light.desk")]
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await bridge.agent_broker.actions.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["stop", "kids", "mcp_vision", "camera"])
def test_callback_camera_checks_original_fence_after_capture(boundary):
    r, config = runtime(mcp_enabled=True, mcp_vision_enabled=True, local_vision_enabled=True, camera_enabled=True)
    executor = HouseholdRobotExecutor(r)
    entered, release = threading.Event(), threading.Event()

    def capture(**kw):
        kw["adult_current"]()
        entered.set()
        assert release.wait(3)
        return b"fixture"

    r._capture_camera_jpeg = capture
    r._everyday_describe_frame = lambda *a, **kw: pytest.fail("No image analysis after revocation")
    values = []
    worker = threading.Thread(target=lambda: values.append(executor.call(
        "capture_reachy_camera", {"purpose": "look"}, "one")))
    worker.start()
    assert entered.wait(3)
    if boundary == "stop":
        r._request_conversation_stop()
    elif boundary == "kids":
        r._kids_active = True
    elif boundary == "mcp_vision":
        config.mcp_vision_enabled = False
    else:
        config.camera_enabled = False
    release.set()
    worker.join(3)
    assert not worker.is_alive() and values[0]["isError"]


def test_external_camera_needs_external_permission_and_local_vision():
    r, config = runtime(mcp_enabled=True, camera_enabled=True, camera_natural_requests_enabled=True,
                        household_setup_version=1, local_vision_enabled=True)
    executor = HouseholdRobotExecutor(r)
    r._capture_camera_jpeg = lambda **kw: pytest.fail("Speech approval is not external consent")
    assert "capture_reachy_camera" not in executor.names
    assert executor.call("capture_reachy_camera", {"purpose": "look"}, "one")["isError"]
    config.mcp_vision_enabled = True
    config.local_vision_enabled = False
    assert "capture_reachy_camera" not in HouseholdRobotExecutor(r).names


@pytest.mark.parametrize("boundary", ["disconnect", "generation", "kids"])
def test_pending_composed_call_cannot_outlive_robot_authority(monkeypatch, boundary):
    bridge = standalone(monkeypatch)
    bridge.robot_api_key = "synthetic-robot"

    async def run():
        app = web.Application()

        async def mcp(request):
            return await household_mcp(bridge, request)

        async def robot(request):
            return await robot_callbacks(bridge, request)

        app.router.add_post("/mcp", mcp)
        app.router.add_get("/v1/household/robot", robot)
        async with ClientSession() as http, TestClient(TestServer(app)) as client:
            bridge.http = http
            context = ctx(1, "conversation")
            robot_headers = {**HEADERS, "Authorization": "Bearer synthetic-robot"}
            socket = await client.ws_connect("/v1/household/robot", headers=robot_headers)
            await socket.send_json({"type": "household.attach", "context": context,
                                    "local_tools": ["set_reachy_power_mode"]})
            assert (await socket.receive_json())["type"] == "household.ready"
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "_meta": {"homebody_context": context}, "name": "set_reachy_power_mode",
                "arguments": {"mode": "awake"}}}
            other = await client.post("/mcp", headers={**HEADERS, "X-Reachy-Device-Id": "other"}, json=payload)
            assert (await other.json())["error"]["code"] == -32001
            pending = asyncio.create_task(client.post("/mcp", headers=HEADERS, json=payload))
            call = await socket.receive_json()
            assert call["type"] == "everyday.tool_call"
            if boundary == "disconnect":
                await socket.close()
            elif boundary == "generation":
                await bridge.agent_broker.establish_session("device", ctx(2, "conversation"))
            else:
                bridge._kids_live["child"] = float("inf")
            response = await asyncio.wait_for(pending, 2)
            assert (await response.json())["error"]["code"] == -32001
            await socket.close()
            await bridge.agent_broker.actions.shutdown()

    asyncio.run(run())


def test_launch_entrypoint_is_explicit_and_loopback(monkeypatch):
    from companion import household_server
    monkeypatch.delenv("HOMEBODY_HOUSEHOLD_TOKEN", raising=False)
    monkeypatch.delenv("HOMEBODY_HOUSEHOLD_ROBOT_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        main([])
    monkeypatch.setenv("HOMEBODY_HOUSEHOLD_TOKEN", "fake-caller")
    monkeypatch.setenv("HOMEBODY_HOUSEHOLD_ROBOT_TOKEN", "fake-robot")
    seen = []
    marker = object()
    monkeypatch.setattr(household_server, "create_household_app", lambda **kw: seen.append(kw) or marker)
    monkeypatch.setattr(household_server.web, "run_app", lambda app, **kw: seen.append((app, kw)))
    main([])
    assert seen == [{"api_key": "fake-caller", "robot_api_key": "fake-robot"},
                    (marker, {"host": "127.0.0.1", "port": 8644, "access_log": None})]
