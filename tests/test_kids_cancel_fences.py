"""Held fixed-speech/clue approvals across I Spy cancel/End/round change, and strict moderation parsing.

Synthetic fixtures only: fake moderation/model transports; no provider, robot or media access.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from test_ispy_target_variety import CHAIR, LAMP, SESSION, Harness, Request
from test_kids_story_ispy_speech import ModelHttp, approve, chat, fixed_bridge, story_bridge


def _held(bridge) -> asyncio.Event:
    gate = asyncio.Event()

    async def held(_text: str, _key: str) -> bool:
        await gate.wait()
        return False

    bridge._moderation_flagged = held
    return gate


async def _run_held(bridge, call, interrupt) -> BaseException | None:
    gate = _held(bridge)
    task = asyncio.create_task(call())
    for _ in range(5):
        await asyncio.sleep(0)
    await interrupt()
    gate.set()
    try:
        await task
    except BaseException as exc:  # noqa: BLE001 - the stale result must surface as an error
        return exc
    return None


def ispy_round(bridge, target: dict | None = None) -> dict:
    target = dict(target or LAMP, colour="yellow")
    bridge._kids_sessions[SESSION] = {
        "profile": ("7-9", "ispy", "en"), "history": [], "updated_at": time.monotonic(),
        "ispy_role": "reachy_picker", "ispy_target": target,
    }
    bridge._mark_kids_live(SESSION)
    return target


ISPY = {"age_band": "7-9", "activity": "ispy"}


# --- Fixed-speech approval held across I Spy cancel ---------------------------------------------


def test_held_greeting_approval_across_ispy_cancel_is_refused_but_session_stays_usable(monkeypatch) -> None:
    module, bridge, _screened = fixed_bridge(monkeypatch)

    async def scenario() -> BaseException | None:
        return await _run_held(
            bridge, lambda: bridge.kids_speech_approve(approve()),
            lambda: bridge.kids_ispy_cancel(Request({"session_id": SESSION})),
        )

    assert isinstance(asyncio.run(scenario()), module.web.HTTPConflict)
    assert not bridge._kids_speech_approvals
    # Cancel ends the round, not the session: a fresh deliberate request still works.
    bridge._moderation_flagged = _safe
    payload = json.loads(asyncio.run(bridge.kids_speech_approve(approve())).text)
    assert payload["text"] and len(bridge._kids_speech_approvals) == 2


async def _safe(_text: str, _key: str) -> bool:
    return False


@pytest.mark.parametrize("change", ["cancel", "new_round", "end"])
def test_held_ispy_clue_approval_refuses_a_changed_round(monkeypatch, change: str) -> None:
    module, bridge, _screened = fixed_bridge(monkeypatch)
    ispy_round(bridge)

    async def interrupt() -> None:
        if change == "cancel":
            await bridge.kids_ispy_cancel(Request({"session_id": SESSION}))
        elif change == "end":
            await bridge.kids_session(Request({"session_id": SESSION, "state": "ended"}))
        else:  # a new selection commits a new bridge-owned target object for the round
            bridge._kids_sessions[SESSION].update({"ispy_target": dict(CHAIR, colour="blue")})

    async def scenario() -> BaseException | None:
        return await _run_held(
            bridge, lambda: bridge.kids_speech_approve(approve([{"kind": "ispy_clue"}], "en", **ISPY)), interrupt,
        )

    assert isinstance(asyncio.run(scenario()), module.web.HTTPConflict)
    assert not bridge._kids_speech_approvals


# --- Legacy /v1/kids/ispy/clue held across End/cancel/replacement -------------------------------


@pytest.mark.parametrize("change", ["cancel", "new_round", "end", "replaced_session"])
def test_held_legacy_clue_is_refused_after_the_round_changed(monkeypatch, change: str) -> None:
    harness = Harness(monkeypatch, [CHAIR])
    harness.run()
    bridge = harness.bridge

    async def interrupt() -> None:
        if change == "cancel":
            await bridge.kids_ispy_cancel(Request({"session_id": SESSION}))
        elif change == "end":
            await bridge.kids_session(Request({"session_id": SESSION, "state": "ended"}))
        elif change == "new_round":
            bridge._kids_sessions[SESSION].update({"ispy_target": dict(LAMP)})
        else:
            bridge._kids_sessions[SESSION] = dict(bridge._kids_sessions[SESSION])

    async def scenario() -> BaseException | None:
        return await _run_held(bridge, lambda: bridge.kids_ispy_clue(Request({"session_id": SESSION})), interrupt)

    assert isinstance(asyncio.run(scenario()), harness.module.web.HTTPConflict)
    assert not bridge._kids_speech_approvals


def test_legacy_clue_still_works_for_the_current_round(monkeypatch) -> None:
    harness = Harness(monkeypatch, [CHAIR])
    harness.run()
    payload = json.loads(asyncio.run(harness.bridge.kids_ispy_clue(Request({"session_id": SESSION}))).text)
    assert payload["text"].endswith("blue.") and len(harness.bridge._kids_speech_approvals) == 2


# --- Strict moderation parsing -------------------------------------------------------------------


class _BadJson(ValueError):
    pass


class ModerationHttp(ModelHttp):
    """Model transport plus a configurable moderation reply for one stage."""

    def __init__(self, reply: object, *, stage: int) -> None:
        super().__init__(content="A model-derived answer.")
        self.reply = reply
        self.stage = stage
        self.moderations = 0

    def post(self, url: str, **kwargs: object) -> object:
        if "moderations" not in url:
            return super().post(url, **kwargs)
        self.moderations += 1
        reply = self.reply if self.moderations == self.stage else {"results": [{"flagged": False}]}

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def json(self, **_kwargs: object) -> object:
                if isinstance(reply, _BadJson):
                    raise reply
                return reply

        return Response()


MALFORMED = [
    {"results": [{"flagged": None}]},
    {"results": [{"flagged": "false"}]},
    {"results": [{"flagged": 0}]},
    {"results": [{}]},
    {"results": []},
    {"results": None},
    {"results": [None]},
    None,
    "not an object",
    _BadJson("Expecting value"),
]


@pytest.mark.parametrize("reply", MALFORMED)
def test_malformed_moderation_is_unavailable_never_safe(monkeypatch, reply: object) -> None:
    module, bridge = story_bridge(monkeypatch, ModelHttp())
    bridge.http = ModerationHttp(reply, stage=1)
    monkeypatch.delattr(bridge, "_moderation_flagged")  # exercise the real parser
    with pytest.raises(module.web.HTTPServiceUnavailable):
        asyncio.run(bridge._moderation_flagged("hello", "synthetic"))


@pytest.mark.parametrize("stage", [1, 2])
@pytest.mark.parametrize("reply", MALFORMED)
def test_malformed_moderation_in_a_turn_gives_retry_and_no_model_approval(monkeypatch, reply, stage) -> None:
    module, bridge = story_bridge(monkeypatch, ModelHttp())
    bridge.http = ModerationHttp(reply, stage=stage)
    monkeypatch.delattr(bridge, "_moderation_flagged")
    payload = json.loads(asyncio.run(bridge.kids_chat(chat())).text)
    assert payload["retry"] is True and payload["text"] == module._KIDS_MODEL_RETRY_REPLY["nl"]
    assert len(bridge.http.model_kwargs) == (0 if stage == 1 else 1)
    assert bridge._kids_sessions[SESSION]["history"] == []
    retry_digest = module.hashlib.sha256(payload["text"].encode()).hexdigest()
    assert {value["text_digest"] for value in bridge._kids_speech_approvals.values()} == {retry_digest}


def test_well_formed_moderation_still_screens(monkeypatch) -> None:
    module, bridge = story_bridge(monkeypatch, ModelHttp())
    bridge.http = ModerationHttp({"results": [{"flagged": True}]}, stage=1)
    monkeypatch.delattr(bridge, "_moderation_flagged")
    assert asyncio.run(bridge._moderation_flagged("x", "synthetic")) is True
    bridge.http = ModerationHttp({"results": [{"flagged": False}]}, stage=1)
    assert asyncio.run(bridge._moderation_flagged("x", "synthetic")) is False
