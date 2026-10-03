from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from test_agent_broker import context
from test_bridge import load_bridge_module

from companion.reachy_agent_actions import ActionConfig, ActionValidationError, AgentActionService
from companion.reachy_agent_broker import BrokerConfig, ReachyAgentBroker

HEADERS = {"Authorization": "Bearer secret", "X-Reachy-Device-Id": "reachy-a"}
NOTE = {"root": "notes", "path": "owner.md", "text": "Book dentist"}


def run_with_bridge(scenario, monkeypatch: pytest.MonkeyPatch, *, broker: ReachyAgentBroker | None = None):
    module = load_bridge_module()
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    async def main():
        app = module.create_app(api_key="secret", hermes_url="http://127.0.0.1:9")
        bridge = app.middlewares[0].__self__
        if broker is not None:
            bridge.agent_broker = broker
        async with TestClient(TestServer(app)) as client:
            return await scenario(module, bridge, client)

    return asyncio.run(main())


def approve_body(arguments: dict[str, object]) -> dict[str, object]:
    return {
        "request_id": "agent-request-1234",
        "capability_id": "append_scoped_note",
        "arguments": arguments,
        "context": context(),
    }


def test_one_call_approve_only_executes_the_exact_staged_draft(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    broker = ReachyAgentBroker(BrokerConfig(note_roots={"notes": root}))

    async def scenario(_module, _bridge, client) -> None:
        await broker.establish_session("reachy-a", context())
        # Nothing staged: arbitrary arguments are refused.
        refused = await client.post("/v1/agent/approve", headers=HEADERS, json=approve_body(NOTE))
        assert refused.status == 403

        await broker.execute(
            {
                "request_id": "agent-request-1234",
                "capability_id": "draft_note",
                "arguments": NOTE,
                "context": context(),
            },
            object(),
            device_id="reachy-a",
        )
        altered = await client.post(
            "/v1/agent/approve", headers=HEADERS, json=approve_body({**NOTE, "text": "Transfer money"})
        )
        assert altered.status == 403

        approved = await client.post("/v1/agent/approve", headers=HEADERS, json=approve_body(NOTE))
        assert approved.status == 200, await approved.text()
        replay = await client.post("/v1/agent/approve", headers=HEADERS, json=approve_body(NOTE))
        assert replay.status == 403

    run_with_bridge(scenario, monkeypatch, broker=broker)
    assert (root / "owner.md").read_text(encoding="utf-8") == "Book dentist\n"


def test_transcription_rejects_extra_file_parts_and_long_options(monkeypatch) -> None:
    async def scenario(_module, _bridge, client) -> None:
        form = FormData()
        form.add_field("file", b"RIFF" * 10, filename="a.wav", content_type="audio/wav")
        form.add_field("file", b"RIFF" * 10, filename="b.wav", content_type="audio/wav")
        response = await client.post("/v1/audio/transcriptions", headers=HEADERS, data=form)
        assert response.status == 400
        assert "one audio file" in await response.text()

        form = FormData()
        form.add_field("model", "m" * 1000)
        form.add_field("file", b"RIFF" * 10, filename="a.wav", content_type="audio/wav")
        response = await client.post("/v1/audio/transcriptions", headers=HEADERS, data=form)
        assert response.status == 400

    leftovers_before = set(Path("/tmp").glob("homebody-stt-*"))
    run_with_bridge(scenario, monkeypatch)
    assert set(Path("/tmp").glob("homebody-stt-*")) == leftovers_before


def test_json_routes_refuse_oversized_bodies(monkeypatch) -> None:
    async def scenario(_module, _bridge, client) -> None:
        response = await client.post(
            "/v1/kids/chat",
            headers={**HEADERS, "Content-Type": "application/json"},
            data=b"{" + b" " * (2 * 1024 * 1024) + b"}",
        )
        assert response.status == 413

    run_with_bridge(scenario, monkeypatch)


class RecordingHttp:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def get(self, url: str, **_kwargs: object):
        self.requests.append(url)
        raise AssertionError("Home Assistant must not be contacted for a rejected entity")

    post = get


@pytest.mark.parametrize("entity_id", ["../config?x=", "light.not_allowlisted", "media_player.kitchen"])
def test_home_actions_validate_and_allowlist_before_contacting_home_assistant(entity_id: str) -> None:
    service = AgentActionService(
        ActionConfig(
            hass_url="http://ha.local:8123",
            hass_token="token",
            home_actions={"light.desk": frozenset({"turn_on"})},
        )
    )
    http = RecordingHttp()

    async def scenario() -> None:
        with pytest.raises(ActionValidationError):
            await service._home_action(
                "control_home_entity", {"entity_id": entity_id, "action": "turn_on"}, http, ("reachy-a", 4)
            )

    asyncio.run(scenario())
    assert http.requests == []


def test_scheduled_timers_are_capped_and_cancelled_on_shutdown(monkeypatch) -> None:
    service = AgentActionService(
        ActionConfig(reminder_callback_url="http://reachy.test", reminder_callback_token="bridge-secret")
    )

    async def scenario() -> None:
        for _ in range(32):
            await service.execute(
                "set_timer", {"seconds": 600, "label": "tea"}, object(), device_id="reachy-a", generation=1
            )
        with pytest.raises(ActionValidationError, match="too many"):
            await service.execute(
                "set_timer", {"seconds": 600, "label": "tea"}, object(), device_id="reachy-a", generation=1
            )
        tasks = list(service._scheduled_tasks.values())
        await service.shutdown()
        assert service._scheduled_tasks == {}
        assert all(task.cancelled() for task in tasks)

    asyncio.run(scenario())
