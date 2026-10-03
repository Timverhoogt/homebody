from __future__ import annotations

import json

import httpx
import pytest

from homebody.config import AppConfig
from homebody.hermes_client import HermesBridgeClient, HermesBridgeError


def make_client(handler) -> HermesBridgeClient:
    config = AppConfig(
        bridge_url="http://bridge.test",
        api_key="secret",
        instance_id="robot-123",
        system_prompt="Speak briefly.",
    )
    transport = httpx.MockTransport(handler)
    return HermesBridgeClient(config, client=httpx.Client(transport=transport))


def test_health_chat_and_speech_contract() -> None:
    seen_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/chat/completions":
            seen_headers.update(request.headers)
            body = json.loads(request.content)
            assert body["messages"][-1]["content"] == "Hello"
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "Hi from Hermes"}}]},
            )
        if request.url.path == "/v1/audio/speech":
            return httpx.Response(200, content=b"ID3audio", headers={"content-type": "audio/mpeg"})
        raise AssertionError(request.url)

    client = make_client(handler)
    assert client.health()["status"] == "ok"
    assert client.chat("Hello") == "Hi from Hermes"
    speech = client.synthesize("Hi from Hermes")
    assert speech.data == b"ID3audio"
    assert speech.extension == ".mp3"
    assert seen_headers["authorization"] == "Bearer secret"
    assert seen_headers["x-hermes-session-key"] == "agent:main:reachy-mini:robot-123"
    assert seen_headers["x-hermes-session-id"].startswith("reachy-robot-123-")


def test_error_does_not_put_api_key_in_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = make_client(handler)
    with pytest.raises(HermesBridgeError) as error:
        client.chat("Hello")
    assert "secret" not in str(error.value)
    assert "401" in str(error.value)


def test_model_discovery() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "hermes-agent", "root": "hermes-agent"},
                    {"id": "reachy-gemini", "root": "gemini-3.5-flash"},
                ]
            },
        )

    client = make_client(handler)
    assert [model["id"] for model in client.models()] == [
        "hermes-agent",
        "reachy-gemini",
    ]


def test_transcription_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer secret"
        assert b'filename="utterance.wav"' in request.content
        return httpx.Response(200, json={"text": "turn on the lights"})

    client = make_client(handler)
    assert client.transcribe(b"RIFFfake") == "turn on the lights"


def test_typed_agent_broker_client_contract() -> None:
    from homebody.hermes_client import AgentBrokerContext

    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/agent/capabilities":
            return httpx.Response(
                200,
                json={"capabilities": [{"id": "get_reachy_status", "read_only": True}]},
            )
        if request.url.path == "/v1/agent/session":
            body = json.loads(request.content)
            seen.append(body)
            return httpx.Response(
                200,
                json={"ok": True, "session_generation": body["context"]["session_generation"]},
            )
        if request.url.path == "/v1/agent/execute":
            body = json.loads(request.content)
            seen.append(body)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "request_id": body["request_id"],
                    "capability_id": body["capability_id"],
                    "data": {"state": "awake"},
                    "evidence": [{"source": "reachy_runtime"}],
                    "freshness": {"observed_at": 1.0, "completed_at": 1.1, "age_seconds": 0.1},
                    "read_only": True,
                    "side_effect": False,
                },
            )
        if request.url.path == "/v1/agent/ask":
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"text": "Reachy is awake."})
        if request.url.path == "/v1/agent/activity":
            return httpx.Response(200, json={"activity": [{"event": "completed"}]})
        if request.url.path == "/v1/agent/pending-approval":
            return httpx.Response(
                200,
                json={
                    "pending_approval": {
                        "draft_id": "draft-0123456789abcdef01234567",
                        "capability_id": "append_scoped_note",
                        "arguments": {"root": "notes", "path": "x.md", "text": "exact"},
                    }
                },
            )
        if request.url.path == "/v1/agent/approve-pending":
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "draft_id": body["draft_id"],
                    "data": {"verified": True},
                    "evidence": [],
                    "freshness": {},
                    "read_only": False,
                    "side_effect": True,
                },
            )
        if request.url.path.startswith("/v1/agent/cancel/"):
            return httpx.Response(200, json={"cancelled": True})
        raise AssertionError(request.url)

    context = AgentBrokerContext(
        capability_profile="agent",
        adult_ui_unlocked=True,
        kids_mode_active=False,
        power_mode="awake",
        privacy_enabled=True,
        emergency_stop_active=False,
        robot_available=True,
        session_generation=2,
        requested_session_generation=2,
        explicit_private_intent=True,
    )
    client = make_client(handler)
    assert client.agent_capabilities()[0]["id"] == "get_reachy_status"
    client.establish_agent_session(context)
    result = client.execute_agent_capability("get_reachy_status", {}, context, request_id="agent-fixed-id")
    assert result.read_only is True
    assert result.data == {"state": "awake"}
    assert result.evidence == ({"source": "reachy_runtime"},)
    assert client.ask_agent("Status?", context, request_id="agent-ask-id") == "Reachy is awake."
    assert client.agent_activity(context, request_id="agent-activity-id") == [{"event": "completed"}]
    pending = client.pending_agent_approval(context)
    assert pending is not None
    assert pending["capability_id"] == "append_scoped_note"
    approved = client.approve_pending_agent_action(str(pending["draft_id"]), context)
    assert approved["side_effect"] is True
    assert client.cancel_agent_request("agent-ask-id") is True
    assert seen[0]["context"]["session_generation"] == 2
    assert seen[1]["context"]["session_generation"] == 2
    assert seen[2]["request_id"] == "agent-ask-id"


@pytest.mark.parametrize(
    "changed",
    [
        {"request_id": "agent-other-id"},
        {"capability_id": "read_scoped_note"},
        {"evidence": ["not-an-object"]},
        {"freshness": {"observed_at": 1.0}},
    ],
)
def test_agent_broker_client_rejects_unverified_result_metadata(changed: dict[str, object]) -> None:
    from homebody.hermes_client import AgentBrokerContext

    def handler(_request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = {
            "ok": True,
            "request_id": "agent-fixed-id",
            "capability_id": "get_reachy_status",
            "data": {},
            "evidence": [],
            "freshness": {"observed_at": 1.0, "completed_at": 1.1, "age_seconds": 0.1},
            "read_only": True,
            "side_effect": False,
        }
        payload.update(changed)
        return httpx.Response(200, json=payload)

    context = AgentBrokerContext(
        capability_profile="agent",
        adult_ui_unlocked=True,
        kids_mode_active=False,
        power_mode="awake",
        privacy_enabled=True,
        emergency_stop_active=False,
        robot_available=True,
        session_generation=2,
        requested_session_generation=2,
        explicit_private_intent=False,
    )
    client = make_client(handler)
    with pytest.raises(HermesBridgeError, match="invalid Agent Mode result"):
        client.execute_agent_capability("get_reachy_status", {}, context, request_id="agent-fixed-id")


def test_agent_05_client_preview_current_and_action_contracts() -> None:
    from homebody.hermes_client import AgentBrokerContext

    seen: list[tuple[str, dict[str, object]]] = []
    run = {
        "run_id": "run-" + "b" * 24,
        "goal": "Check two sources",
        "status": "preview",
        "generation": 2,
        "resumable": True,
        "budgets": {"max_steps": 5},
        "steps": [{"step_id": "step-1", "status": "queued"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        return httpx.Response(200, json={"run": run})

    context = AgentBrokerContext(
        capability_profile="agent",
        adult_ui_unlocked=True,
        kids_mode_active=False,
        power_mode="standby",
        privacy_enabled=True,
        emergency_stop_active=False,
        robot_available=True,
        session_generation=2,
        requested_session_generation=2,
        explicit_private_intent=True,
    )
    client = make_client(handler)
    assert client.preview_agent_run("Check two sources", context)["run_id"] == run["run_id"]
    assert client.current_agent_run(context)["status"] == "preview"  # type: ignore[index]
    assert client.agent_run_action(
        "approve", str(run["run_id"]), context, step_id="step-1"
    )["status"] == "preview"

    assert [item[0] for item in seen] == [
        "/v1/agent/run/preview",
        "/v1/agent/run/current",
        "/v1/agent/run/approve",
    ]
    assert seen[0][1]["goal"] == "Check two sources"
    assert seen[2][1]["step_id"] == "step-1"
