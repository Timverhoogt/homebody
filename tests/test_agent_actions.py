from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from companion.reachy_agent_actions import (
    ActionConfig,
    ActionValidationError,
    AgentActionService,
    validate_action_arguments,
)


class UnusedHttp:
    def get(self, *_args, **_kwargs):
        raise AssertionError("unexpected GET")

    def post(self, *_args, **_kwargs):
        raise AssertionError("unexpected POST")


def run(coro):
    return asyncio.run(coro)


def test_timer_and_reminder_are_session_scoped_and_reversible() -> None:
    service = AgentActionService(
        ActionConfig(
            reminder_callback_url="http://reachy.test",
            reminder_callback_token="bridge-secret",
        )
    )

    async def scenario() -> None:
        timer, _, _, changed = await service.execute(
            "set_timer",
            {"seconds": 30, "label": "tea"},
            UnusedHttp(),
            device_id="reachy-a",
            generation=2,
        )
        assert changed is True
        assert isinstance(timer, dict)
        assert timer["timer_id"].startswith("timer-")
        undone, _, _, changed = await service.execute(
            "undo_last_reversible_action",
            {},
            UnusedHttp(),
            device_id="reachy-a",
            generation=2,
        )
        assert changed is True
        assert isinstance(undone, dict)
        assert undone["undone"] == "cancel_timer"
        with pytest.raises(ActionValidationError, match="no reversible action"):
            await service.execute(
                "undo_last_reversible_action",
                {},
                UnusedHttp(),
                device_id="reachy-a",
                generation=3,
            )

    run(scenario())


def test_draft_note_requires_exact_one_shot_phone_approval(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    service = AgentActionService(ActionConfig(note_roots={"notes": root}))
    arguments = {"root": "notes", "path": "owner.md", "text": "Book dentist"}

    async def scenario() -> None:
        draft, _, _, changed = await service.execute(
            "draft_note",
            arguments,
            UnusedHttp(),
            device_id="reachy-a",
            generation=4,
        )
        assert changed is False
        assert isinstance(draft, dict)
        assert draft["requires_exact_phone_approval"] is True
        pending = await service.pending("reachy-a", 4)
        assert pending is not None
        assert pending["arguments"] == arguments
        result, _, _, changed = await service.approve_pending(
            "reachy-a", 4, str(pending["draft_id"]), UnusedHttp()
        )
        assert changed is True
        assert isinstance(result, dict)
        assert result["verified"] is True
        assert await service.pending("reachy-a", 4) is None
        with pytest.raises(ActionValidationError, match="missing, stale, or mismatched"):
            await service.approve_pending(
                "reachy-a", 4, str(pending["draft_id"]), UnusedHttp()
            )

    run(scenario())
    assert (root / "owner.md").read_text(encoding="utf-8") == "Book dentist\n"


def test_concurrent_pending_approvals_execute_the_draft_once(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    service = AgentActionService(ActionConfig(note_roots={"notes": root}))
    arguments = {"root": "notes", "path": "owner.md", "text": "Book dentist"}

    async def scenario() -> list[object]:
        await service.execute("draft_note", arguments, UnusedHttp(), device_id="reachy-a", generation=4)
        pending = await service.pending("reachy-a", 4)
        assert pending is not None
        draft_id = str(pending["draft_id"])
        return await asyncio.gather(
            *(service.approve_pending("reachy-a", 4, draft_id, UnusedHttp()) for _ in range(3)),
            return_exceptions=True,
        )

    outcomes = run(scenario())
    succeeded = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    rejected = [outcome for outcome in outcomes if isinstance(outcome, ActionValidationError)]
    assert len(succeeded) == 1
    assert len(rejected) == 2
    assert (root / "owner.md").read_text(encoding="utf-8") == "Book dentist\n"

def test_media_is_staged_without_touching_home_assistant() -> None:
    service = AgentActionService(
        ActionConfig(media_entities=frozenset({"media_player.living_room"}))
    )
    arguments = {
        "entity_id": "media_player.living_room",
        "media_uri": "https://example.test/song.mp3",
        "media_type": "music",
    }

    async def scenario() -> None:
        pending, evidence, _, changed = await service.execute(
            "play_media",
            arguments,
            UnusedHttp(),
            device_id="reachy-a",
            generation=8,
        )
        assert changed is False
        assert isinstance(pending, dict)
        assert pending["arguments"] == arguments
        assert evidence[0]["source"] == "agent_approval_queue"

    run(scenario())


def test_allowlisted_home_action_verifies_state_and_undoes() -> None:
    class Response:
        status = 200

        def __init__(self, payload: object = None) -> None:
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def json(self, **_kwargs):
            return self.payload

        async def read(self) -> bytes:
            return b"[]"

    class Http:
        state = "off"
        services: list[str] = []

        def get(self, url: str, **kwargs):
            assert url.endswith("/api/states/light.desk")
            assert kwargs["allow_redirects"] is False
            return Response({"state": self.state, "attributes": {}})

        def post(self, url: str, **kwargs):
            service_name = url.rsplit("/", 1)[-1]
            assert kwargs["json"] == {"entity_id": "light.desk"}
            self.services.append(service_name)
            self.state = "on" if service_name == "turn_on" else "off"
            return Response([])

    http = Http()
    service = AgentActionService(
        ActionConfig(
            hass_url="http://home-assistant.test",
            hass_token="host-secret",
            home_actions={"light.desk": frozenset({"turn_on", "turn_off"})},
        )
    )

    async def scenario() -> None:
        result, _, _, changed = await service.execute(
            "control_home_entity",
            {"entity_id": "light.desk", "action": "turn_on"},
            http,
            device_id="reachy-a",
            generation=3,
        )
        assert changed is True
        assert isinstance(result, dict) and result["verified"] is True
        assert http.state == "on"
        await service.execute(
            "undo_last_reversible_action",
            {},
            http,
            device_id="reachy-a",
            generation=3,
        )
        assert http.state == "off"
        assert http.services == ["turn_on", "turn_off"]

    run(scenario())


def test_timer_delivery_uses_authenticated_no_redirect_callback() -> None:
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def read(self) -> bytes:
            return b"{}"

    class Http:
        request: tuple[str, dict[str, object]] | None = None

        def post(self, url: str, **kwargs):
            self.request = (url, kwargs)
            return Response()

    http = Http()
    service = AgentActionService(
        ActionConfig(
            reminder_callback_url="https://reachy.test",
            reminder_callback_token="bridge-secret",
        )
    )
    run(service._deliver_later("timer-0123456789abcdef", 0, "Timer finished.", http))
    assert http.request is not None
    url, kwargs = http.request
    assert url == "https://reachy.test/api/agent/reminder-delivery"
    assert kwargs["headers"] == {"Authorization": "Bearer bridge-secret"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["json"] == {
        "item_id": "timer-0123456789abcdef",
        "text": "Timer finished.",
    }


def test_approval_is_bound_to_exact_arguments_device_and_generation(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    service = AgentActionService(ActionConfig(note_roots={"notes": root}))
    arguments = {"root": "notes", "path": "owner.md", "text": "Exact text"}

    async def scenario() -> None:
        approval = await service.issue_approval(
            "reachy-a", 5, "append_scoped_note", arguments
        )
        token = str(approval["approval_token"])
        with pytest.raises(ActionValidationError, match="mismatched"):
            await service.execute(
                "append_scoped_note",
                {**arguments, "text": "Changed text"},
                UnusedHttp(),
                device_id="reachy-a",
                generation=5,
                approval_token=token,
            )
        # A mismatched attempt consumes the token so it cannot be probed/reused.
        with pytest.raises(ActionValidationError, match="mismatched"):
            await service.execute(
                "append_scoped_note",
                arguments,
                UnusedHttp(),
                device_id="reachy-a",
                generation=5,
                approval_token=token,
            )

    run(scenario())
    assert not (root / "owner.md").exists()


def test_approved_note_append_rejects_symlinks_and_hardlinks(tmp_path: Path) -> None:
    root = tmp_path / "notes"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (root / "symlink.md").symlink_to(outside)
    (root / "hardlink.md").hardlink_to(outside)
    service = AgentActionService(ActionConfig(note_roots={"notes": root}))

    async def scenario(path: str) -> None:
        arguments = {"root": "notes", "path": path, "text": "must not append"}
        approval = await service.issue_approval(
            "reachy-a", 9, "append_scoped_note", arguments
        )
        with pytest.raises(ActionValidationError):
            await service.execute(
                "append_scoped_note",
                arguments,
                UnusedHttp(),
                device_id="reachy-a",
                generation=9,
                approval_token=str(approval["approval_token"]),
            )

    run(scenario("symlink.md"))
    run(scenario("hardlink.md"))
    assert outside.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.parametrize(
    ("capability_id", "arguments"),
    [
        ("set_media_volume", {"entity_id": "media_player.room", "volume": 0.81}),
        ("set_timer", {"seconds": True}),
        ("control_home_entity", {"entity_id": "lock.front", "action": "unlock"}),
        (
            "send_approved_message",
            {"channel": "mobile_app", "recipient": "a,b", "text": "hello"},
        ),
    ],
)
def test_action_schemas_reject_unbounded_or_coerced_values(
    capability_id: str, arguments: dict[str, object]
) -> None:
    with pytest.raises(ActionValidationError):
        validate_action_arguments(capability_id, arguments)
