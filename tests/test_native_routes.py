"""Owner native flow through authenticated bridge routes, not direct manager calls alone."""

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_native_workspace import NativeFixture, configured
from test_project_roadmap import context

from companion.hermes_reachy_bridge import Bridge
from companion.native_workspace import NativeWorkspace
from companion.reachy_agent_broker import BrokerConfig, ReachyAgentBroker


def test_native_bridge_voice_proposes_then_ui_approves_and_wake_preserves_work(tmp_path):
    async def scenario():
        projects, targets = configured(tmp_path)
        upstream = NativeFixture()
        manager = NativeWorkspace(projects, targets, {"hermes": upstream}, tmp_path / "state.json")
        bridge = Bridge(api_key="owner-fixture", hermes_url="http://unused.test")
        bridge.http = object()
        bridge.agent_broker = ReachyAgentBroker(BrokerConfig(projects=projects))
        bridge.native_workspace = manager
        app = web.Application(middlewares=[bridge.kids_latch_middleware])
        app.router.add_post("/v1/agent/session", bridge.broker_session)
        app.router.add_post("/v1/agent/native/{action}", bridge.native_workspace_action)
        app.router.add_post("/v1/agent/ask", bridge.broker_ask)
        headers = {"Authorization": "Bearer owner-fixture", "X-Reachy-Device-Id": "robot"}
        state = context()
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/v1/agent/native/bind", json={"context": state, "target_id": "photo-hermes"})
            assert response.status == 401
            response = await client.post(
                "/v1/agent/native/bind", json={"context": state, "target_id": "photo-hermes"}, headers=headers
            )
            assert response.status == 403  # no authoritative runtime lease yet
            assert (await client.post("/v1/agent/session", json={"context": state}, headers=headers)).status == 200
            assert (
                await client.post(
                    "/v1/agent/native/bind", json={"context": state, "target_id": "photo-hermes"}, headers=headers
                )
            ).status == 200
            response = await client.post(
                "/v1/agent/ask",
                json={"context": state, "request_id": "voice-proposal-123", "request": "Implement the discussed item"},
                headers=headers,
            )
            assert response.status == 200
            assert "No work has started" in (await response.json())["text"]
            assert not any(c[0] == "submit" for c in upstream.calls)
            snapshot = await manager.snapshot("robot")
            approval = snapshot["pending"]["approval_id"]
            response = await client.post(
                "/v1/agent/native/approve", json={"context": state, "approval_id": approval}, headers=headers
            )
            assert response.status == 200
            assert (await response.json())["run_id"] == "run-exact"
            generation = state["session_generation"] + 1
            state.update(session_generation=generation, requested_session_generation=generation)
            assert (
                await client.post(
                    "/v1/agent/session", json={"context": state, "preserve_native": True}, headers=headers
                )
            ).status == 200
            assert not any(c[0] == "stop" for c in upstream.calls)
            state.update(
                session_generation=generation + 1, requested_session_generation=generation + 1, privacy_enabled=False
            )
            assert (
                await client.post(
                    "/v1/agent/session", json={"context": state, "preserve_native": True}, headers=headers
                )
            ).status == 200
            assert ("stop", "native-existing", "run-exact") in upstream.calls
            assert (await manager.snapshot("robot"))["messages"] == []
            assert (await client.post("/v1/agent/native/read", json={"context": state}, headers=headers)).status == 403
        await manager.close()

    asyncio.run(scenario())
