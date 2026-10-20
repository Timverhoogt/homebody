"""I Spy target variety: a bounded validated candidate pool with a code-side random choice.

Synthetic fixtures only: fake vision HTTP, fake moderation and an injected chooser. No frames,
network, provider or device access.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

import pytest
from test_bridge import load_bridge_module

SESSION = "kids-" + "f" * 32
FRAMES = [base64.b64encode(b"jpeg-%d" % index).decode("ascii") for index in range(5)]


def candidate(name: str, **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "object_name": name,
        "colour": "blue",
        "category": "household object",
        "location": "in the room",
        "frame_index": 1,
        "bbox": [0.1, 0.1, 0.3, 0.3],
        "confidence": 0.9,
        "stable": True,
        "visible_frame_count": 3,
        "hints_en": [f"Look for the {name}"],
        "hints_nl": [f"Zoek de {name}"],
    }
    value.update(changes)
    return value


CHAIR = candidate("chair", colour="blue", frame_index=0)
LAMP = candidate("lamp", colour="yellow", frame_index=2)
CUSHION = candidate("cushion", colour="green", frame_index=3)


class Rotate:
    """Deterministic injected chooser: rotate each shuffled list by a fixed offset."""

    def __init__(self, offset: int = 0) -> None:
        self.offset = offset
        self.seen: list[list[str]] = []

    def shuffle(self, items: list[Any]) -> None:
        self.seen.append([str(item["object_name"]) for item in items])
        if items:
            shift = self.offset % len(items)
            items[:] = items[shift:] + items[:shift]


class Response:
    def __init__(self, body: dict[str, object], gate: asyncio.Event | None) -> None:
        self.status = 200
        self.body = body
        self.gate = gate

    async def __aenter__(self) -> Response:
        if self.gate is not None:
            await self.gate.wait()
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def json(self, **_kwargs: object) -> dict[str, object]:
        return self.body


class FakeVision:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.requests: list[dict[str, object]] = []
        self.gate: asyncio.Event | None = None

    def post(self, _url: str, *, json: dict[str, object], **_kwargs: object) -> Response:
        self.requests.append(json)
        content = self.reply if isinstance(self.reply, str) else _dumps(self.reply)
        return Response({"choices": [{"message": {"content": content}}]}, self.gate)


def _dumps(value: object) -> str:
    return json.dumps(value)


class Request:
    headers = {"Authorization": "Bearer bridge-secret"}

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def json(self) -> dict[str, object]:
        return self.payload


def select_request(session_id: str = SESSION) -> Request:
    return Request({"session_id": session_id, "age_band": "7-9", "language": "en", "frames_jpeg": list(FRAMES)})


class Harness:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        candidates: object,
        *,
        offset: int = 0,
        flagged: tuple[str, ...] = (),
    ) -> None:
        self.module = load_bridge_module()
        self.bridge = self.module.Bridge(api_key="bridge-secret", hermes_url="http://127.0.0.1:8642", profile=None)
        monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
        monkeypatch.delenv("REACHY_ISPY_VISION_URL", raising=False)
        reply = candidates if isinstance(candidates, (dict, str)) else {"candidates": candidates}
        self.vision = FakeVision(reply)
        self.bridge.http = self.vision
        self.rng = Rotate(offset)
        self.bridge._ispy_rng = self.rng
        self.flagged = flagged
        self.moderated: list[str] = []
        self.moderation_gate: asyncio.Event | None = None
        self.moderation_unavailable = False

        async def moderation(text: str, _key: str) -> bool:
            self.moderated.append(text)
            if self.moderation_gate is not None:
                await self.moderation_gate.wait()
            if self.moderation_unavailable:
                raise self.module.web.HTTPServiceUnavailable(text="Kids Mode safety screening is unavailable")
            return any(word in text for word in self.flagged)

        monkeypatch.setattr(self.bridge, "_moderation_flagged", moderation)

    async def select(self, session_id: str = SESSION) -> dict[str, object]:
        response = await self.bridge.kids_ispy_select(select_request(session_id))
        assert response.headers["Cache-Control"] == "no-store"
        return json.loads(response.text)

    def run(self, session_id: str = SESSION) -> dict[str, object]:
        return asyncio.run(self.select(session_id))

    async def end(self, session_id: str = SESSION) -> None:
        await self.bridge.kids_session(Request({"session_id": session_id, "state": "ended"}))

    async def cancel(self, session_id: str = SESSION) -> None:
        await self.bridge.kids_ispy_cancel(Request({"session_id": session_id}))


# --- Selection among validated candidates ------------------------------------------------------


def test_select_chooses_each_distinct_safe_candidate_through_injected_chooser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chosen: list[str] = []
    for offset in range(3):
        harness = Harness(monkeypatch, [CHAIR, LAMP, CUSHION], offset=offset)
        payload = harness.run()
        assert set(payload) == {"target"}
        target = payload["target"]
        assert isinstance(target, dict)
        chosen.append(str(target["object_name"]))
        # Exactly the one returned target is retained server-side for the round.
        assert harness.bridge._kids_sessions[SESSION]["ispy_target"] == target
        assert harness.rng.seen == [["chair", "lamp", "cushion"]]
    assert chosen == ["chair", "lamp", "cushion"]


def test_vision_request_uses_strict_bounded_candidate_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, [CHAIR])
    harness.run()
    response_format = harness.vision.requests[0]["response_format"]
    assert isinstance(response_format, dict)
    spec = response_format["json_schema"]
    assert spec["strict"] is True
    schema = spec["schema"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["candidates"]
    assert set(schema["properties"]) == {"candidates"}
    pool = schema["properties"]["candidates"]
    assert pool["type"] == "array"
    assert pool["minItems"] == 0
    assert pool["maxItems"] == harness.module._ISPY_MAX_CANDIDATES <= 4
    assert pool["items"]["required"] == harness.module._ISPY_TARGET_SCHEMA["required"]
    assert "English" in pool["items"]["properties"]["object_name"]["description"]
    assert pool["items"]["additionalProperties"] is False
    assert pool["items"]["properties"]["confidence"]["minimum"] == 0.0


def test_repeated_and_alias_candidates_cannot_weight_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    shelf = candidate("bookshelf", colour="brown", frame_index=1, bbox=[0.1, 0.1, 0.4, 0.5])
    renamed = [
        shelf,
        candidate("Book shelf", colour="brown", frame_index=3, bbox=[0.5, 0.2, 0.3, 0.3]),
        candidate("the bookshelves", colour="brown", frame_index=4),
        LAMP,
    ]
    same_spot = [
        shelf,
        candidate("bookcase", colour="brown", frame_index=1, bbox=[0.12, 0.11, 0.38, 0.48]),
        LAMP,
    ]
    for pool in (renamed, same_spot):
        harness = Harness(monkeypatch, pool)
        harness.run()
        assert harness.rng.seen == [["bookshelf", "lamp"]]


@pytest.mark.parametrize("changes", [
    {"object_name": "person"}, {"object_name": "phones"},
    {"location": "near a screen"}, {"hints_en": ["Near the credit card"]},
    {"confidence": 0.05}, {"stable": False}, {"visible_frame_count": 1},
    {"bbox": [0.1, 0.1, 0.01, 0.01]}, {"bbox": [0, 0, 1, 1]}, {"extra": "ignored"},
])
def test_model_object_judgment_is_not_overridden(monkeypatch, changes):
    proposal = {**candidate("mug"), **changes}
    harness = Harness(monkeypatch, [proposal])
    chosen = harness.run()["target"]
    assert chosen["object_name"] == proposal["object_name"]
    assert harness.moderated == []
    from homebody.ispy import validate_ispy_target
    assert validate_ispy_target(chosen, frame_count=5).object_name == proposal["object_name"]


@pytest.mark.parametrize(
    "reply",
    [
        {"target": CHAIR},
        {"candidates": []},
        {"candidates": CHAIR},
        # Both slightly oversized and runaway lists violate the native1..4 contract.
        {"candidates": [CHAIR] * 5},
        {"candidates": [CHAIR] * 17},
        {"candidates": [CHAIR], "target": CHAIR},
        "not json",
        [CHAIR],
    ],
)
def test_whole_response_outside_the_bounded_schema_fails_closed(
    monkeypatch: pytest.MonkeyPatch, reply: object
) -> None:
    harness = Harness(monkeypatch, {"candidates": []})
    harness.vision.reply = reply
    with pytest.raises(harness.module.web.HTTPBadGateway):
        harness.run()
    assert SESSION not in harness.bridge._kids_sessions
    assert harness.moderated == []


def test_single_safe_candidate_remains_the_honest_only_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, [candidate("invalid", bbox=[0, 0, -1, 1]), CHAIR])
    for _round in range(3):
        assert harness.run()["target"]["object_name"] == "chair"


# --- Moderation --------------------------------------------------------------------------------


@pytest.mark.parametrize("unavailable", [False, True])
def test_object_selection_does_not_use_secondary_moderation(monkeypatch, unavailable):
    harness = Harness(monkeypatch, [CHAIR, LAMP], flagged=("chair", "lamp"))
    harness.moderation_unavailable = unavailable
    assert harness.run()["target"]["object_name"] == "chair"
    assert harness.moderated == []


# --- Fixed single target for the round and session-scoped variety ------------------------------


def test_target_stays_fixed_for_the_round(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, [CHAIR, LAMP, CUSHION], offset=1)
    target = harness.run()["target"]

    async def wrong(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(harness.bridge, "_judge_ispy_guess", wrong)
    chat = Request({"input": "a sofa", "session_id": SESSION, "profile": {
        "age_band": "7-9", "activity": "ispy", "language": "en"}})
    clue = json.loads(asyncio.run(harness.bridge.kids_ispy_clue(Request({"session_id": SESSION}))).text)
    assert clue["text"].endswith("yellow.")
    asyncio.run(harness.bridge.kids_chat(chat))
    asyncio.run(harness.bridge.kids_chat(chat))
    assert harness.bridge._kids_sessions[SESSION]["ispy_target"] == target
    again = json.loads(asyncio.run(harness.bridge.kids_ispy_clue(Request({"session_id": SESSION}))).text)
    assert again["text"] == clue["text"]


def test_recent_targets_are_avoided_within_session_and_forgotten_on_end(monkeypatch: pytest.MonkeyPatch) -> None:
    shelf = candidate("bookshelf", colour="brown", frame_index=1)
    harness = Harness(monkeypatch, [shelf, LAMP, CUSHION])
    picks = [str(harness.run()["target"]["object_name"]) for _round in range(4)]
    assert picks[:3] == ["bookshelf", "lamp", "cushion"]
    recent = harness.bridge._kids_sessions[SESSION]["ispy_recent"]
    assert len(recent) <= harness.module._ISPY_RECENT_LIMIT
    # Recency is a preference among validated objects, never a reason to invent one.
    assert picks[3] in {"bookshelf", "lamp", "cushion"}

    asyncio.run(harness.cancel())
    assert SESSION not in harness.bridge._kids_sessions
    assert harness.run()["target"]["object_name"] == "bookshelf"
    asyncio.run(harness.end())
    assert SESSION not in harness.bridge._kids_sessions


def test_recent_memory_expires_with_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, [candidate("bookshelf"), LAMP])
    assert harness.run()["target"]["object_name"] == "bookshelf"
    harness.bridge._kids_sessions[SESSION]["updated_at"] = (
        time.monotonic() - harness.module._KIDS_HISTORY_TTL_SECONDS - 1
    )
    assert harness.run()["target"]["object_name"] == "bookshelf"


# --- Stale results after Stop, cancel, expiry or replacement ------------------------------------


async def _held(harness: Harness, stage: str, interrupt: Any) -> BaseException | None:
    gate = asyncio.Event()
    if stage == "vision":
        harness.vision.gate = gate
    else:
        harness.moderation_gate = gate
    task = asyncio.create_task(harness.select())
    for _ in range(50):
        await asyncio.sleep(0)
        if (harness.vision.requests if stage == "vision" else harness.moderated):
            break
    await interrupt()
    gate.set()
    try:
        await task
    except BaseException as exc:  # noqa: BLE001 - the stale result must surface as an error
        return exc
    return None


@pytest.mark.parametrize("stage", ["vision"])
@pytest.mark.parametrize("interrupt", ["end", "cancel", "expire"])
def test_held_select_result_is_discarded_after_stop_cancel_or_expiry(
    monkeypatch: pytest.MonkeyPatch, stage: str, interrupt: str
) -> None:
    harness = Harness(monkeypatch, [CHAIR, LAMP])

    async def act() -> None:
        if interrupt == "end":
            await harness.end()
        elif interrupt == "cancel":
            await harness.cancel()
        else:
            harness.bridge._kids_live[SESSION] = time.monotonic() - harness.module._KIDS_LIVE_MAX_SECONDS - 1

    error = asyncio.run(_held(harness, stage, act))
    assert isinstance(error, harness.module.web.HTTPConflict)
    assert SESSION not in harness.bridge._kids_sessions
    assert not harness.bridge._ispy_selections


def test_replaced_select_discards_the_older_held_result(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, [CHAIR, LAMP])

    async def scenario() -> tuple[BaseException | None, dict[str, object]]:
        newer: dict[str, object] = {}

        async def replace() -> None:
            harness.vision.gate = None
            harness.rng.offset = 1
            newer.update(await harness.select())

        return await _held(harness, "vision", replace), newer

    error, newer = asyncio.run(scenario())
    assert isinstance(error, harness.module.web.HTTPConflict)
    assert newer["target"]["object_name"] == "lamp"
    assert harness.bridge._kids_sessions[SESSION]["ispy_target"]["object_name"] == "lamp"
    assert not harness.bridge._ispy_selections


def test_select_after_stop_sends_no_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, [CHAIR])
    asyncio.run(harness.end())
    with pytest.raises(harness.module.web.HTTPConflict):
        harness.run()
    assert harness.vision.requests == []
    assert SESSION not in harness.bridge._kids_sessions
