"""Workspace GPT-Live delegation end to end through ``Bridge.live``.

Loopback fake Live provider, real broker leases, a fixture native adapter and scripted reasoning
responses. No OpenAI, robot, camera, credentials or real model.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import json
import logging
from pathlib import Path

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from test_live_transport import FakeLive, collect, delta

import companion.hermes_reachy_bridge as bridge_module
from companion import live_transport
from companion.hermes_reachy_bridge import Bridge
from companion.native_workspace import NativeWorkspace, TargetConfig
from companion.reachy_agent_broker import BrokerConfig, BrokerValidationError, ReachyAgentBroker
from companion.reachy_projects import Project, ProjectCatalog

DEVICE = "reachy-1"
HEADERS = {"Authorization": "Bearer bridge-secret", "X-Reachy-Device-Id": DEVICE}
PRIVATE = "Kwartaalplan van Marieke"  # a private-looking body that must never reach logs


def ctx(generation, **changes):
    value = {
        "capability_profile": "agent", "adult_ui_unlocked": True, "kids_mode_active": False,
        "power_mode": "awake", "privacy_enabled": True, "emergency_stop_active": False, "robot_available": True,
        "session_generation": generation, "requested_session_generation": generation,
        "explicit_private_intent": False, "reachy_status": {},
    }
    value.update(changes)
    return value


class Adapter:
    """Native Hermes session fixture: exact run identity, optional blocking discussion."""

    def __init__(self):
        self.calls = []
        self.block = False
        self.release = None

    async def history(self, target):
        return []

    async def discuss(self, target, text, request_id, instructions):
        if self.block:
            self.release = asyncio.Event()  # created on the running session loop
        self.calls.append(("discuss", target.session_id, text))
        if self.release is not None:
            await self.release.wait()
        return {"run_id": "run-d1", "status": "completed", "output": "From your Hermes session."}

    async def stop(self, target, run_id):
        self.calls.append(("stop", run_id))
        return "stopping"

    async def close(self):
        pass


class Reasoning:
    """Scripted restricted-broker provider responses; records every request."""

    def __init__(self, messages=()):
        self.messages = list(messages)
        self.payloads = []

    def post(self, _url, *, json, **_kwargs):
        self.payloads.append(copy.deepcopy(json))
        message = self.messages.pop(0)

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self, **_kwargs):
                return {"choices": [{"message": message}]}

        return Response()


def answer(text, capabilities=(), status="answered"):
    return {"role": "assistant",
            "content": json.dumps({"text": text, "status": status, "used_capabilities": list(capabilities)})}


def make_bridge(tmp_path, monkeypatch, *, discussion=True, reasoning=()):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-live")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ROADMAP.md").write_text("Next: import readiness\n")
    projects = ProjectCatalog((Project("photo", "Photo", root, Path("ROADMAP.md")),))
    targets = TargetConfig.parse({"photo": {
        "backend": "hermes", "url": "http://127.0.0.1:8642", "token_env": "TEST_NATIVE_TOKEN",
        "project_id": "photo", "session_id": "native-existing", "discussion_enabled": discussion,
    }}, projects)
    bridge = Bridge(api_key="bridge-secret", hermes_url="http://127.0.0.1:1")
    bridge.agent_broker = ReachyAgentBroker(BrokerConfig(projects=projects))
    adapter = Adapter()
    bridge.native_workspace = NativeWorkspace(projects, targets, {"hermes": adapter}, tmp_path / "j.json")
    provider = Reasoning(reasoning)
    bridge.http = provider
    monkeypatch.setattr(bridge, "_llm_key", lambda: "fixture-provider-key")
    return bridge, adapter, provider


def run_live(bridge, monkeypatch, request_text, *, generation=1, during=None, wait_for="answered"):
    """One Workspace Live session with one delegation. ``during(bridge)`` runs once it has started."""

    async def script(ws, _received):
        await ws.send_json(delta("user", 0, 600, request_text))
        await ws.send_json({"type": "session.delegation.created", "offset_ms": 600,
                            "delegation": {"id": "del-1", "target": "client"}})

    async def main():
        fake = FakeLive(script)
        upstream = TestServer(fake.app())
        await upstream.start_server()
        url = str(upstream.make_url("/v1/live/sessions")).replace("http", "ws", 1)
        monkeypatch.setattr(bridge_module, "serve_live", functools.partial(
            live_transport.serve_live, upstream_url=url, started_timeout=1.0, transcript_grace=0.2))
        app = web.Application()
        app.router.add_get("/v1/live", bridge.live)
        server = TestServer(app)
        await server.start_server()
        events = []
        try:
            await bridge.agent_broker.establish_session(DEVICE, ctx(generation))
            async with ClientSession() as http:
                async with http.ws_connect(server.make_url("/v1/live"), headers=HEADERS) as ws:
                    await ws.send_json({"type": "session.start", "model": "gpt-live-1", "voice": "marin",
                                        "timeout_seconds": 60, "assistant_mode": "workspace",
                                        "agent_context": ctx(generation)})
                    await collect(ws, events, lambda p: p.get("state") == "started")
                    if during is not None:
                        await during(bridge)
                    try:
                        await collect(ws, events, lambda p: p.get("state") == wait_for, timeout=3.0)
                    except TimeoutError:
                        pass
                    if not ws.closed:
                        await ws.send_json({"type": "session.close"})
                        try:
                            await collect(ws, events, lambda p: p["type"] == "session.closed", timeout=3.0)
                        except TimeoutError:
                            pass
        finally:
            await server.close()
            await upstream.close()
            if bridge.native_workspace is not None:
                await bridge.native_workspace.close()
        commentary = [e for e in fake.received if e["type"] == "session.commentary.append"]
        return events, commentary

    return asyncio.run(main())


def states(events):
    return [e["state"] for e in events if e.get("type") == "bridge.delegation"]


# --- Native conversation is the goal ----------------------------------------------------------


def test_enabled_discussion_cannot_launch_an_unapproved_tool_capable_run(tmp_path, monkeypatch):
    # Hermes /v1/runs runs with the full platform toolset and admits no per-run restriction; the
    # discussion instructions are a prompt, not authorization. Host opt-in must not launch it.
    bridge, adapter, provider = make_bridge(tmp_path, monkeypatch)

    async def bind():
        await bridge.native_workspace.bind(DEVICE, "photo")

    asyncio.run(bind())
    assert bridge.native_readiness(DEVICE) == "discussion_unenforced"
    events, commentary = run_live(bridge, monkeypatch, "Let's discuss the importer design", wait_for="not_ready")
    assert states(events) == ["started", "not_ready"]
    assert adapter.calls == []  # no native run of any kind
    assert provider.payloads == []  # and never the restricted broker as ordinary-chat fallback
    spoken = [c["content"] for c in commentary]
    assert len(spoken) == 1 and "Nothing was sent" in spoken[0] and "no tools" in spoken[0]
    assert "From your Hermes session." not in spoken


def test_native_discussion_is_refused_before_any_native_request(tmp_path, monkeypatch):
    from companion.native_adapters import HermesNativeAdapter
    from companion.native_workspace import DISCUSSION_UNENFORCED, WorkspaceError

    bridge, adapter, _provider = make_bridge(tmp_path, monkeypatch)

    class RecordingHttp:
        def __init__(self):
            self.requests = []

        def request(self, method, url, **_kwargs):
            self.requests.append((method, url))
            raise AssertionError("no native request may be made for discussion")

        get = post = request

    async def scenario():
        manager = bridge.native_workspace
        await manager.bind(DEVICE, "photo")
        target = manager.targets["photo"]
        assert target.discussion_enabled and manager.discussion_available(target) is False
        with pytest.raises(WorkspaceError, match="nothing was sent"):
            await manager.discuss(DEVICE, "Let's discuss the importer")
        snapshot = await manager.snapshot(DEVICE)
        assert snapshot["binding"]["voice_discussion"] is False
        http = RecordingHttp()
        native = HermesNativeAdapter(http, lambda _name: "fixture-token")
        with pytest.raises(WorkspaceError) as refused:
            await native.discuss(target, "Let's discuss", "req-1", "Discuss only")
        assert str(refused.value) == DISCUSSION_UNENFORCED
        assert http.requests == []
        await manager.close()

    asyncio.run(scenario())
    assert adapter.calls == []


@pytest.mark.parametrize("setup", ["unbound", "detached", "parked", "discussion_disabled"])
def test_not_ready_native_session_gets_actionable_setup_not_a_broker_failure(tmp_path, monkeypatch, caplog, setup):
    # Baseline: ordinary chat fell through to the restricted broker, whose unevidenced answer
    # raised BrokerValidationError and Live still reported "answered" with a generic sentence.
    bridge, adapter, provider = make_bridge(
        tmp_path, monkeypatch, discussion=setup != "discussion_disabled",
        reasoning=[answer("Sure, happy to talk about that.")],
    )

    async def arrange():
        manager = bridge.native_workspace
        if setup != "unbound":
            await manager.bind(DEVICE, "photo")
        if setup == "detached":
            await manager.stop(DEVICE, detach=True)
        if setup == "parked":
            await manager.park(DEVICE)

    asyncio.run(arrange())
    caplog.set_level(logging.INFO)
    events, commentary = run_live(bridge, monkeypatch, f"Let's talk about {PRIVATE}", wait_for="not_ready")
    assert states(events) == ["started", "not_ready"]
    assert provider.payloads == [] and adapter.calls == []
    spoken = commentary[0]["content"]
    assert "Nothing was sent" in spoken and "answered" not in states(events)
    expected = {"unbound": "Open project", "detached": "Open project", "parked": "Switch to Workspace",
                "discussion_disabled": "not enabled on the agent host"}[setup]
    assert expected in spoken
    assert PRIVATE not in caplog.text and "BrokerValidationError" not in caplog.text


@pytest.mark.parametrize("configuration", ["empty_targets", "no_native_manager"])
def test_unconfigured_native_sessions_give_setup_not_a_broker_failure(tmp_path, monkeypatch, caplog, configuration):
    # Reviewed defect: with no native targets, ordinary chat still fell through to the broker.
    bridge, adapter, provider = make_bridge(tmp_path, monkeypatch, reasoning=[answer("Sure, let's chat.")])
    asyncio.run(bridge.native_workspace.close())
    if configuration == "empty_targets":
        projects = bridge.agent_broker.config.projects
        bridge.native_workspace = NativeWorkspace(projects, TargetConfig.parse({}, projects), {"hermes": adapter},
                                                  tmp_path / "empty.json")
    else:
        bridge.native_workspace = None  # REACHY_NATIVE_TARGETS_JSON unset on the host
    caplog.set_level(logging.INFO)
    events, commentary = run_live(bridge, monkeypatch, f"Let's talk about {PRIVATE}", wait_for="not_ready")
    assert states(events) == ["started", "not_ready"]
    assert provider.payloads == [] and adapter.calls == []
    assert "No Hermes project session is set up" in commentary[0]["content"]
    assert "Nothing was sent" in commentary[0]["content"]
    assert PRIVATE not in caplog.text and "BrokerValidationError" not in caplog.text


def test_explicit_roadmap_request_still_uses_labelled_restricted_reader(tmp_path, monkeypatch):
    bridge, adapter, provider = make_bridge(tmp_path, monkeypatch, reasoning=[
        {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "read_project_roadmap", "arguments": json.dumps({"project_id": "photo"})}}]},
        answer("The roadmap lists import readiness next.", ["read_project_roadmap"]),
    ])
    events, commentary = run_live(bridge, monkeypatch, "What is next on the photo project roadmap?")
    assert states(events) == ["started", "answered"]
    assert "import readiness" in commentary[0]["content"]
    assert len(provider.payloads) == 2 and adapter.calls == []


# --- Exact restricted-broker rejection branches (each reproduced separately) -------------------


@pytest.mark.parametrize(("label", "message", "category"), [
    ("unevidenced_answer", answer("Sure, the project is going well."), "answer_unverified"),
    ("fenced_json", {"role": "assistant", "content": "```json\n" + json.dumps(
        {"text": "x", "status": "insufficient", "used_capabilities": []}) + "\n```"}, "answer_not_structured"),
    ("two_reads_at_once", {"role": "assistant", "tool_calls": [
        {"id": f"c{i}", "type": "function", "function": {"name": "list_projects", "arguments": "{}"}}
        for i in range(2)]}, "project_scope_refused"),
])
def test_restricted_broker_rejection_is_categorical_failure_not_answered(
        tmp_path, monkeypatch, caplog, label, message, category):
    bridge, _adapter, provider = make_bridge(tmp_path, monkeypatch, reasoning=[message])
    caplog.set_level(logging.INFO)
    events, commentary = run_live(bridge, monkeypatch, f"Tell me about the {PRIVATE} project roadmap",
                                  wait_for="failed")
    assert len(provider.payloads) == 1, label  # exactly one provider HTTP 200, as in the incident
    assert states(events) == ["started", "failed"]
    assert "Nothing is confirmed" in commentary[0]["content"]
    assert f"category={category}" in caplog.text
    assert PRIVATE not in caplog.text


def test_broker_rejection_branches_raise_validation_directly(tmp_path, monkeypatch):
    """Ground truth for the categories above, via the unchanged restricted validator."""
    bridge, _adapter, _provider = make_bridge(tmp_path, monkeypatch, reasoning=[answer("Hello there.")])

    async def scenario():
        await bridge.agent_broker.establish_session(DEVICE, ctx(1))
        await bridge.agent_broker.register_request(DEVICE, ctx(1), "branch-check-1")
        with pytest.raises(BrokerValidationError, match="provenance"):
            await bridge._agent_answer("Read the photo project roadmap", context=ctx(1), device_id=DEVICE)
        await bridge.native_workspace.close()

    asyncio.run(scenario())


# --- Provider unavailable, staleness and late cancellation -------------------------------------


def test_provider_unavailable_is_failed_with_truthful_text(tmp_path, monkeypatch, caplog):
    bridge, _adapter, _provider = make_bridge(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "_llm_key", lambda: "")
    caplog.set_level(logging.INFO)
    events, commentary = run_live(bridge, monkeypatch, "Read the photo project roadmap", wait_for="failed")
    assert states(events) == ["started", "failed"]
    assert "unavailable" in commentary[0]["content"] and "Nothing was done" in commentary[0]["content"]
    assert "category=provider_unavailable" in caplog.text


def test_clear_during_restricted_turn_discards_late_answer_and_ends_session(tmp_path, monkeypatch):
    # Native discussion no longer launches a run, so the late-answer fence is exercised on the
    # remaining delegated path: a labelled roadmap read whose provider reply lands after Clear.
    bridge, adapter, provider = make_bridge(tmp_path, monkeypatch)
    release = asyncio.Event()
    # The same valid two-step reply that answers in test_explicit_roadmap_request_still_uses_...
    replies = [
        {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "read_project_roadmap", "arguments": json.dumps({"project_id": "photo"})}}]},
        answer("The roadmap lists import readiness next.", ["read_project_roadmap"]),
    ]

    def blocked_post(url, *, json, **kwargs):
        provider.payloads.append(copy.deepcopy(json))
        message = replies.pop(0)

        class Response:
            status = 200

            async def __aenter__(self):
                await release.wait()
                return self

            async def __aexit__(self, *_args):
                return None

            async def json(self, **_kwargs):
                return {"choices": [{"message": message}]}

        return Response()

    provider.post = blocked_post

    async def during(bridge):
        for _ in range(100):
            if provider.payloads:
                break
            await asyncio.sleep(0.02)
        # A new runtime generation (owner Clear, profile change, privacy) while the turn is running.
        await bridge.agent_broker.establish_session(DEVICE, ctx(2))
        release.set()

    events, commentary = run_live(bridge, monkeypatch, "What is next on the photo project roadmap?",
                                  during=during)
    assert provider.payloads and commentary == []  # nothing spoken after Clear
    assert "answered" not in states(events) and "failed" not in states(events)
    assert adapter.calls == []
