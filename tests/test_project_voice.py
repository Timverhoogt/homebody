from __future__ import annotations

import asyncio
import copy
import json
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_project_roadmap import context

from companion.hermes_reachy_bridge import Bridge
from companion.reachy_agent_broker import BrokerConfig, BrokerContext, BrokerValidationError, ReachyAgentBroker
from companion.reachy_projects import ProjectCatalog


def call(name, arguments=None):
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": f"call-{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments or {})},
            }
        ],
    }


def answer(text, capabilities=(), status="answered"):
    return {
        "role": "assistant",
        "content": json.dumps({"text": text, "status": status, "used_capabilities": list(capabilities)}),
    }


class FixtureHttp:
    """Explicit deterministic provider responses; never invokes a real model."""

    def __init__(self, messages):
        self.messages = iter(messages)
        self.payloads = []

    def post(self, _url, *, json, **_kwargs):
        self.payloads.append(copy.deepcopy(json))
        message = next(self.messages)

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self, **_kwargs):
                return {"choices": [{"message": message}]}

        return Response()


def setup(tmp_path, monkeypatch, responses):
    (tmp_path / "roadmap.md").write_text("# Photo roadmap\nDone: importer\nNext: colour pipeline\n")
    bridge = Bridge(api_key="fixture-key", hermes_url="http://127.0.0.1:8642", profile=None)
    bridge.agent_broker = ReachyAgentBroker(
        BrokerConfig(
            projects=ProjectCatalog.from_json(
                json.dumps(
                    {
                        "photo": {"title": "Photo agent", "root": str(tmp_path), "roadmap": "roadmap.md"},
                    }
                )
            )
        )
    )
    monkeypatch.setattr(bridge, "_llm_key", lambda: "fixture-provider-key")
    monkeypatch.setattr(bridge, "http", FixtureHttp(responses))
    return bridge


async def ask(bridge, text, device="reachy-a", state=None):
    state = state or context()
    await bridge.agent_broker.establish_session(device, state)
    await bridge.agent_broker.register_request(device, state, "project-voice-test")
    try:
        return await bridge._agent_answer(text, context=state, device_id=device)
    finally:
        await bridge.agent_broker.unregister_request(device, state["session_generation"], "project-voice-test")


def test_discover_read_and_followup_re_read_real_source(tmp_path, monkeypatch):
    bridge = setup(
        tmp_path,
        monkeypatch,
        [
            call("list_projects"),
            call("read_project_roadmap", {"project_id": "photo"}),
            answer(
                "The roadmap records importer complete; colour pipeline next (lines 2–3).",
                ["list_projects", "read_project_roadmap"],
            ),
            call("read_project_roadmap", {"project_id": "photo"}),
            answer("The roadmap now lists regression tests next (line 3).", ["read_project_roadmap"]),
        ],
    )

    async def scenario():
        first = await ask(bridge, "Tell me about the photo project roadmap")
        (tmp_path / "roadmap.md").write_text("# Photo roadmap\nDone: colour pipeline\nNext: regression tests\n")
        second = await ask(bridge, "What's next?")
        return first, second

    first, second = asyncio.run(scenario())
    assert "colour pipeline" in first and "regression tests" in second
    provider = bridge.http
    assert isinstance(provider, FixtureHttp)
    payloads = provider.payloads
    assert len(payloads) == 5
    assert any(item.get("content") == first for item in payloads[3]["messages"])
    assert "Selected project ID from this live session: photo" in payloads[3]["messages"][0]["content"]
    results = [json.loads(item["content"]) for item in payloads[4]["messages"] if item.get("role") == "tool"]
    assert results[-1]["data"]["text"].endswith("3|Next: regression tests")
    assert results[-1]["data"]["execution_available"] is False
    assert not any(item["data"].get("text", "").endswith("Next: colour pipeline") for item in results)


def test_project_history_is_device_scoped_and_not_used_for_unrelated_questions(tmp_path, monkeypatch):
    bridge = setup(
        tmp_path,
        monkeypatch,
        [
            call("read_project_roadmap", {"project_id": "photo"}),
            answer("Photo roadmap next: colour pipeline", ["read_project_roadmap"]),
            answer("No project is selected here.", status="insufficient"),
            answer("I could not verify the weather.", status="insufficient"),
        ],
    )

    async def scenario():
        await ask(bridge, "Read the photo project roadmap")
        await ask(bridge, "What's next?", device="reachy-b")
        await ask(bridge, "Weather today?")

    asyncio.run(scenario())
    provider = bridge.http
    assert isinstance(provider, FixtureHttp)
    for payload in provider.payloads[2:]:
        assert not any(item.get("content") == "Photo roadmap next: colour pipeline" for item in payload["messages"])
        assert "Selected project ID" not in payload["messages"][0]["content"]


def test_history_clears_on_generation_privacy_and_idle_expiry(tmp_path, monkeypatch):
    bridge = setup(tmp_path, monkeypatch, [])
    broker = bridge.agent_broker

    async def scenario():
        state = context()
        await broker.establish_session("reachy-a", state)
        async with broker.project_conversation("reachy-a", BrokerContext.parse(state)) as lease:
            lease.project_dialogue.append({"role": "user", "content": "private project"})
            lease.selected_project_id = "photo"
            broker.refresh_project_conversation(lease)
            assert lease.project_expiry is not None
        await broker.establish_session("reachy-a", context(privacy_enabled=False))
        assert not lease.project_dialogue and not lease.selected_project_id
        assert lease.project_expiry is None
        new = context(session_generation=8, requested_session_generation=8)
        await broker.establish_session("reachy-a", new)
        async with broker.project_conversation("reachy-a", BrokerContext.parse(new)) as current:
            assert not current.project_dialogue
            current.project_dialogue.append({"role": "user", "content": "new project"})
            current.selected_project_id = "photo"
            current.project_updated_at = time.monotonic() - 601
        async with broker.project_conversation("reachy-a", BrokerContext.parse(new)) as current:
            assert not current.project_dialogue and not current.selected_project_id
            broker.refresh_project_conversation(current)
            current.project_dialogue.append({"role": "user", "content": "timer cleared"})
            callback = current.project_expiry._callback
            callback(*current.project_expiry._args)
            assert not current.project_dialogue and current.project_expiry is None

    asyncio.run(scenario())


def test_followup_cannot_reuse_cached_answer_as_evidence(tmp_path, monkeypatch):
    bridge = setup(
        tmp_path,
        monkeypatch,
        [
            call("read_project_roadmap", {"project_id": "photo"}),
            answer("Colour pipeline next", ["read_project_roadmap"]),
            call("get_reachy_status"),
            answer("Colour pipeline next", ["get_reachy_status"]),
        ],
    )

    async def scenario():
        await ask(bridge, "Read the photo roadmap")
        with pytest.raises(BrokerValidationError, match="read-only scope"):
            await ask(bridge, "What's next?")
        lease = bridge.agent_broker._leases["reachy-a"]
        assert len(lease.project_dialogue) == 2

    asyncio.run(scenario())


def test_discovery_exception_cannot_chain_other_actions(tmp_path, monkeypatch):
    bridge = setup(tmp_path, monkeypatch, [call("list_projects"), call("set_timer", {"seconds": 60})])
    with pytest.raises(BrokerValidationError, match="read-only scope"):
        asyncio.run(ask(bridge, "Tell me about the photo project roadmap"))


@pytest.mark.parametrize(
    "claim",
    [
        "I started coding",
        "I'll start work",
        "I am implementing the project",
        "I'll start working on this",
        "Ready to test?",
    ],
)
def test_no_project_execution_claims(tmp_path, monkeypatch, claim):
    bridge = setup(
        tmp_path,
        monkeypatch,
        [
            call("read_project_roadmap", {"project_id": "photo"}),
            answer(claim, ["read_project_roadmap"]),
        ],
    )
    with pytest.raises(BrokerValidationError, match="provenance"):
        asyncio.run(ask(bridge, "Read the photo project roadmap"))
    assert not bridge.agent_broker._leases["reachy-a"].project_dialogue


def test_unconfigured_project_access_is_honest_without_provider_call(tmp_path, monkeypatch):
    bridge = setup(tmp_path, monkeypatch, [])
    bridge.agent_broker = ReachyAgentBroker(BrokerConfig())

    def unexpected_provider():
        raise AssertionError("unconfigured project request must not call a provider")

    monkeypatch.setattr(bridge, "_llm_key", unexpected_provider)
    reply = asyncio.run(ask(bridge, "Read the raw photo project roadmap"))
    assert "not configured" in reply and "No project work has started" in reply


def test_project_voice_http_requires_auth_and_current_lease(tmp_path, monkeypatch):
    bridge = setup(
        tmp_path,
        monkeypatch,
        [
            call("read_project_roadmap", {"project_id": "photo"}),
            answer("Colour pipeline next", ["read_project_roadmap"]),
        ],
    )

    async def scenario():
        app = web.Application()
        app.router.add_post("/v1/agent/session", bridge.broker_session)
        app.router.add_post("/v1/agent/ask", bridge.broker_ask)
        headers = {"Authorization": "Bearer fixture-key", "X-Reachy-Device-Id": "reachy-a"}
        payload = {"request_id": "project-http-test", "request": "Read the photo roadmap", "context": context()}
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/v1/agent/ask", json=payload)
            assert response.status == 401
            response = await client.post("/v1/agent/ask", json=payload, headers=headers)
            assert response.status == 403
            response = await client.post("/v1/agent/session", json={"context": context()}, headers=headers)
            assert response.status == 200
            response = await client.post("/v1/agent/ask", json=payload, headers=headers)
            assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
            assert (await response.json())["text"] == "Colour pipeline next"
            payload["context"] = context(kids_mode_active=True)
            response = await client.post("/v1/agent/ask", json=payload, headers=headers)
            assert response.status == 403

    asyncio.run(scenario())


def test_inflight_project_turn_cannot_survive_safety_transition(tmp_path, monkeypatch):
    bridge = setup(tmp_path, monkeypatch, [])

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        class SlowHttp:
            def post(self, *_args, **_kwargs):
                class Response:
                    status = 200

                    async def __aenter__(self):
                        entered.set()
                        await release.wait()
                        return self

                    async def __aexit__(self, *_args):
                        return None

                    async def json(self, **_kwargs):
                        return {"choices": [{"message": answer("Old answer", status="insufficient")}]}

                return Response()

        monkeypatch.setattr(bridge, "http", SlowHttp())
        task = asyncio.create_task(ask(bridge, "Read the photo project roadmap"))
        await asyncio.wait_for(entered.wait(), 1)
        old_lease = bridge.agent_broker._leases["reachy-a"]
        await bridge.agent_broker.establish_session(
            "reachy-a", context(session_generation=8, requested_session_generation=8, privacy_enabled=False)
        )
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert not old_lease.project_dialogue and not old_lease.selected_project_id
        assert not bridge.agent_broker._leases["reachy-a"].project_dialogue

    asyncio.run(scenario())


def test_project_dialogue_is_bounded_and_redacted(tmp_path, monkeypatch):
    responses = []
    for i in range(5):
        responses.extend(
            [
                call("read_project_roadmap", {"project_id": "photo"}),
                answer(f"Roadmap reply {i}", ["read_project_roadmap"]),
            ]
        )
    bridge = setup(tmp_path, monkeypatch, responses)

    async def scenario():
        for _ in range(5):
            await ask(bridge, "Read the photo roadmap password=private-value")
        lease = bridge.agent_broker._leases["reachy-a"]
        assert len(lease.project_dialogue) == 6
        assert "private-value" not in json.dumps(list(lease.project_dialogue))
        assert not any(item["content"] == "Roadmap reply 0" for item in lease.project_dialogue)

    asyncio.run(scenario())


def test_unregistered_project_is_not_repaired_or_guessed(tmp_path, monkeypatch):
    bridge = setup(tmp_path, monkeypatch, [call("read_project_roadmap", {"project_id": "Photo"})])
    with pytest.raises(BrokerValidationError):
        asyncio.run(ask(bridge, "Read the photo project roadmap"))
    assert not bridge.agent_broker._leases["reachy-a"].project_dialogue


def test_roadmap_read_failure_does_not_reuse_previous_evidence(tmp_path, monkeypatch):
    bridge = setup(
        tmp_path,
        monkeypatch,
        [
            call("read_project_roadmap", {"project_id": "photo"}),
            answer("Colour pipeline next", ["read_project_roadmap"]),
            call("read_project_roadmap", {"project_id": "photo"}),
        ],
    )

    async def scenario():
        await ask(bridge, "Read the photo roadmap")
        (tmp_path / "roadmap.md").unlink()
        with pytest.raises(BrokerValidationError):
            await ask(bridge, "What's next?")
        assert len(bridge.agent_broker._leases["reachy-a"].project_dialogue) == 2

    asyncio.run(scenario())
