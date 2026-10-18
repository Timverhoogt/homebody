"""Real client/server protocol over queues: no sockets, services or hardware."""

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from aiohttp import web
from test_everyday_runtime import runtime
from test_standalone_household_tools import HEADERS, standalone

from companion import household_robot
from companion.household_mcp import household_mcp
from homebody import household_client


class Wire:
    def __init__(self):
        self.to_server, self.to_client = asyncio.Queue(), asyncio.Queue()
        self.closed = False
        self.server_task: asyncio.Task | None = None

    async def prepare(self, request):
        pass

    async def receive(self, timeout=None):
        data = await asyncio.wait_for(self.to_server.get(), timeout)
        return SimpleNamespace(type=web.WSMsgType.TEXT, data=data)

    async def send_json(self, payload):
        await self.to_client.put(json.dumps(payload))

    async def close(self):
        self.closed = True
        await self.to_server.put(None)
        await self.to_client.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.receive()
        if message.data is None:
            raise StopAsyncIteration
        return message


class Peer:
    def __init__(self, wire):
        self.wire = wire

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
        await self.wire.server_task

    async def send(self, raw):
        await self.wire.to_server.put(raw)

    async def recv(self):
        value = await self.wire.to_client.get()
        if value is None:
            raise RuntimeError("Peer closed")
        return value

    async def close(self):
        await self.wire.close()

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.wire.to_client.get()
        if value is None:
            raise StopAsyncIteration
        return value


def wire_connect(monkeypatch, bridge):
    wires = []

    def connect(url, *, additional_headers, **kwargs):
        wire = Wire()
        wires.append(wire)
        monkeypatch.setattr(household_robot.web, "WebSocketResponse", lambda **kw: wire)
        request = SimpleNamespace(headers=additional_headers, path="/v1/household/robot")
        wire.server_task = asyncio.create_task(household_robot.robot_callbacks(bridge, request))
        return Peer(wire)

    monkeypatch.setattr(household_client, "connect", connect)
    return wires


async def rpc(bridge, context, method="tools/list"):
    async def body():
        return {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {
                "_meta": {"homebody_context": context},
                **(
                    {"name": "set_reachy_power_mode", "arguments": {"mode": "awake"}} if method == "tools/call" else {}
                ),
            },
        }

    response = await household_mcp(bridge, SimpleNamespace(headers=HEADERS, json=body, path="/mcp"))
    return json.loads(response.body)


@pytest.mark.parametrize("state", ["standby", "kids", "privacy", "workspace", "stop", "disabled"])
def test_idle_actual_client_server_never_grants_tools(monkeypatch, state):
    bridge = standalone(monkeypatch)
    bridge.robot_api_key = "synthetic-robot"
    wires = wire_connect(monkeypatch, bridge)
    r, config = runtime(mcp_enabled=True)
    r._audio_ready = True
    if state == "standby":
        r._power_mode = "standby"
    elif state == "kids":
        r._kids_active = True
    elif state == "privacy":
        r._privacy_requested.set()
    elif state == "workspace":
        r._capability_profile = "agent"
    elif state == "stop":
        r._request_conversation_stop()
    else:
        config.mcp_enabled = False
    r.execute_everyday_tool = lambda *a, **kw: pytest.fail("No physical action from idle")

    async def run():
        ready = asyncio.Event()
        context = asdict(r.agent_broker_context(explicit_private_intent=False))
        task = asyncio.create_task(
            household_client.household_callbacks(
                r,
                bridge_url="https://unused.invalid",
                robot_token="synthetic-robot",
                device_id="device",
                context=context,
                ready=ready,
            )
        )
        try:
            await asyncio.wait_for(ready.wait(), 1)
            assert not bridge.household_robots["device"].current()
            assert not bridge.household_robots["device"].transport.names
            assert not bridge.agent_broker._leases
            for method in ("tools/list", "tools/call"):
                assert (await rpc(bridge, context, method))["error"]["code"] == -32001
            # Even a malicious/legacy service cannot dispatch a tool on idle.
            await wires[0].send_json(
                {
                    "type": "everyday.tool_call",
                    "call_id": "one",
                    "name": "set_reachy_power_mode",
                    "arguments": {"mode": "awake"},
                }
            )
            with pytest.raises(household_client.HouseholdConnectionError, match="Inactive"):
                await asyncio.wait_for(task, 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await bridge.agent_broker.actions.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["stop", "kids", "privacy", "workspace", "standby", "generation"])
def test_active_retirement_and_explicit_reconnect(monkeypatch, change):
    bridge = standalone(monkeypatch)
    bridge.robot_api_key = "synthetic-robot"
    wire_connect(monkeypatch, bridge)
    r, _ = runtime(mcp_enabled=True)
    r._audio_ready = True
    r.execute_everyday_tool = lambda *a, **kw: pytest.fail("Only catalogue requested")

    async def run():
        context = asdict(r.agent_broker_context(explicit_private_intent=False))
        ready = asyncio.Event()
        task = asyncio.create_task(
            household_client.household_callbacks(
                r,
                bridge_url="https://unused.invalid",
                robot_token="synthetic-robot",
                device_id="device",
                context=context,
                ready=ready,
            )
        )
        try:
            await asyncio.wait_for(ready.wait(), 1)
            assert "result" in await rpc(bridge, context)
            original = household_client.connection_approval(r)
            if change == "stop":
                r._request_conversation_stop()
            elif change == "kids":
                r._kids_active = True
            elif change == "privacy":
                r._privacy_requested.set()
            elif change == "workspace":
                r._capability_profile = "agent"
            elif change == "standby":
                r._power_mode = "standby"
            else:
                r._agent_session_generation += 1
            await asyncio.wait_for(task, 1)
            assert (await rpc(bridge, context))["error"]["code"] == -32001
            with pytest.raises(RuntimeError):
                original.require_current()
            if change != "generation":
                assert isinstance(household_client.connection_approval(r), household_client.HouseholdIdleApproval)
            if change == "stop":
                r._accept_wake_turn()  # Explicit fresh adult runtime authority, not a socket.
                with pytest.raises(RuntimeError):
                    original.require_current()
                assert isinstance(household_client.connection_approval(r), household_client.HouseholdRobotExecutor)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await bridge.agent_broker.actions.shutdown()

    asyncio.run(run())


def test_owner_connect_standby_real_routes_and_handlers(monkeypatch):
    import httpx
    from fastapi import FastAPI

    from homebody.owner_auth import owner_authenticated

    bridge = standalone(monkeypatch)
    bridge.robot_api_key = "synthetic-robot"
    wire_connect(monkeypatch, bridge)
    r, config = runtime(mcp_enabled=True, household_url="https://unused.invalid", household_api_key="synthetic-robot")
    r._power_mode = "standby"
    r._audio_ready = True
    r.execute_everyday_tool = lambda *a, **kw: pytest.fail("No physical action")
    app = FastAPI()
    household_client.install_household_connection_routes(app, lambda: r, lambda: config)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://in-memory") as client:
            assert (await client.post("/api/household/tools/connect")).status_code == 403
            token = owner_authenticated.set(True)
            try:
                response = await client.post("/api/household/tools/connect")
                assert response.status_code == 200, response.text
                assert response.json()["connected"] and not response.json()["tools_active"]
                assert response.json()["homebody_context"] is None
                status = (await client.get("/api/household/tools/status")).json()
                assert status["connected"] and not status["tools_active"]
                assert "synthetic-robot" not in json.dumps(status)
                assert (await client.post("/api/household/tools/disconnect")).json() == {"connected": False}
                assert not (await client.get("/api/household/tools/status")).json()["connected"]
            finally:
                owner_authenticated.reset(token)
                await bridge.agent_broker.actions.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,detail",
    [
        (401, "rejected the robot credential"),
        (403, "rejected the robot credential"),
        (307, "redirect refused"),
        (500, "rejected the handshake"),
    ],
)
def test_connect_errors_are_categorical_and_redacted(monkeypatch, status, detail):
    import httpx
    from fastapi import FastAPI
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    from homebody.owner_auth import owner_authenticated

    r, config = runtime(
        household_url="https://unused.invalid", household_api_key="synthetic-secret", household_connection_enabled=True
    )
    r._power_mode = "standby"
    attempts = []

    def fail(*a, **kw):
        attempts.append(1)
        raise InvalidStatus(
            Response(status, "synthetic-secret", Headers({"Location": "https://synthetic-secret.invalid"}))
        )

    monkeypatch.setattr(household_client, "connect", fail)
    app = FastAPI()
    household_client.install_household_connection_routes(app, lambda: r, lambda: config)

    async def run():
        token = owner_authenticated.set(True)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://in-memory") as client:
                response = await client.post("/api/household/tools/connect")
                assert response.status_code == 502
                assert detail in response.json()["detail"] and "synthetic-secret" not in response.text
                assert not (await client.get("/api/household/tools/status")).json()["connected"]
                assert attempts == [1]
        finally:
            owner_authenticated.reset(token)

    asyncio.run(run())


def test_idle_never_promotes_on_runtime_change():
    r, _ = runtime(mcp_enabled=True)
    r._audio_ready = True
    r._power_mode = "standby"
    approval = household_client.connection_approval(r)
    approval.require_current()
    r._power_mode = "awake"
    with pytest.raises(RuntimeError):
        approval.require_current()
    assert approval.names == []


@pytest.mark.parametrize(
    "kind,names", [("household.attach", []), ("unknown", []), ("household.idle", ["set_reachy_power_mode"])]
)
def test_server_refuses_legacy_standby_and_malformed_attach(monkeypatch, kind, names):
    bridge = standalone(monkeypatch)
    bridge.robot_api_key = "synthetic-robot"
    r, _ = runtime()
    r._power_mode = "standby"

    async def run():
        wire = Wire()
        monkeypatch.setattr(household_robot.web, "WebSocketResponse", lambda **kw: wire)
        await wire.to_server.put(
            json.dumps(
                {
                    "type": kind,
                    "local_tools": names,
                    "context": asdict(r.agent_broker_context(explicit_private_intent=False)),
                }
            )
        )
        await household_robot.robot_callbacks(
            bridge, SimpleNamespace(headers={**HEADERS, "Authorization": "Bearer synthetic-robot"})
        )
        assert wire.closed and not bridge.household_robots
        assert await wire.to_client.get() is None  # Never acknowledges attachment.
        await bridge.agent_broker.actions.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("reply", [{"type": "household.ready"}, {}, {"type": "household.idle_ready", "tools": True}])
def test_idle_rejects_legacy_or_malformed_ack(monkeypatch, reply):
    r, _ = runtime(mcp_enabled=True)
    r._power_mode = "standby"

    async def run():
        wire = Wire()

        async def server():
            await wire.receive()
            await wire.send_json(reply)

        wire.server_task = asyncio.create_task(server())
        monkeypatch.setattr(household_client, "connect", lambda *a, **kw: Peer(wire))
        ready = asyncio.Event()
        with pytest.raises(household_client.HouseholdConnectionError, match="incompatible"):
            await household_client.household_callbacks(
                r,
                bridge_url="https://unused.invalid",
                robot_token="synthetic-robot",
                device_id="device",
                context=r.agent_broker_context(explicit_private_intent=False),
                ready=ready,
            )
        assert not ready.is_set()

    asyncio.run(run())


@pytest.mark.parametrize("token", ["", "newline\nvalue", "space value", "tab\tvalue"])
def test_malformed_credentials_rejected_without_transport(monkeypatch, token):
    r, _ = runtime()
    monkeypatch.setattr(household_client, "connect", lambda *a, **kw: pytest.fail("No handshake"))
    with pytest.raises(ValueError):
        asyncio.run(
            household_client.household_callbacks(
                r, bridge_url="https://unused.invalid", robot_token=token, device_id="device", context={}
            )
        )


@pytest.mark.parametrize("header", ["", "Bearer synthetic-household", "Basic synthetic-robot", "Bearer wrong"])
def test_robot_auth_refused_before_websocket(monkeypatch, header):
    bridge = standalone(monkeypatch)
    bridge.robot_api_key = "synthetic-robot"
    monkeypatch.setattr(household_robot.web, "WebSocketResponse", lambda **kw: pytest.fail("Before upgrade"))
    with pytest.raises(web.HTTPUnauthorized):
        asyncio.run(
            household_robot.robot_callbacks(
                bridge, SimpleNamespace(headers={**HEADERS, "Authorization": header}, path="/v1/household/robot")
            )
        )
