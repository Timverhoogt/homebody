"""Real bridge handlers with synthetic vision/guess transports; no provider or hardware access."""
from __future__ import annotations

import asyncio
import copy
import json
import re
import time

import pytest
from test_ispy_target_variety import CHAIR, CUSHION, FRAMES, LAMP, SESSION, Harness, Request, candidate


class VisualHarness(Harness):
    def __init__(self, monkeypatch, *, language="en", hosted=False):
        super().__init__(monkeypatch, [CHAIR, LAMP, CUSHION])
        self.language = language
        self.hosted = hosted
        self.bridge._mark_kids_live(SESSION)
        self.bridge._kids_sessions[SESSION] = {
            "profile": ("7-9", "ispy", language) + ((True,) if hosted else ()),
            "history": [], "updated_at": time.monotonic(),
            "ispy_role": "reachy_picker", "ispy_target": dict(CHAIR),
        }

    @property
    def state(self):
        return self.bridge._kids_sessions[SESSION]

    async def chat(self, text):
        return json.loads((await self.bridge.kids_chat(Request({
            "session_id": SESSION, "profile": {
                "age_band": "7-9", "activity": "ispy", "language": self.language,
            }, "hosted": self.hosted, "input": text,
        }))).text)

    async def begin(self):
        self.vision.reply = {"match": True}
        result = await self.chat("chair")
        assert result["ispy_phase"] == "awaiting_view"
        assert result["ispy_next_action"] == "prepare_player_view"
        assert re.fullmatch("[0-9a-f]{32}", result["ispy_view_token"])
        return result["ispy_view_token"]

    def observation(self, token=None, **changes):
        payload = {
            "session_id": SESSION, "age_band": "7-9", "language": self.language,
            "view_token": token or self.state.get("ispy_view_token", "a" * 32),
            "frames_jpeg": list(FRAMES),
        }
        payload.update(changes)
        return Request(payload)

    async def observe(self, pool, token=None):
        self.vision.reply = {"candidates": pool}
        return json.loads((await self.bridge.kids_ispy_observe(self.observation(token))).text)


async def until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("synthetic operation did not reach its hold point")


@pytest.mark.parametrize("hosted", [False, True])
def test_observe_then_guess_only_retained_object_and_child_confirmation(monkeypatch, hosted):
    h = VisualHarness(monkeypatch, hosted=hosted)

    async def scenario():
        token = await h.begin()
        observed = await h.observe([CHAIR, LAMP, CUSHION])
        assert observed == {"observed": True, "candidate_count": 3, "view_token": token}
        assert "ispy_view_token" not in h.state
        assert h.state["ispy_player_scans"] == 1
        assert all(any(item["object_name"] in text for text in h.moderated) for item in [CHAIR, LAMP, CUSHION])
        assert not any("data:image" in str(value) for value in h.state.values())
        h.vision.reply = {"candidate_index": 1}
        guessed = await h.chat("It gives light")
        assert guessed["text"] == "Is it a lamp?"
        assert guessed["ispy_next_action"] == ""
        assert "ispy_view_token" not in guessed
        assert (await h.chat("no"))["ispy_phase"] == "awaiting_clue"
        h.vision.reply = {"candidate_index": 2}
        guessed = await h.chat("It is soft")
        assert guessed["text"] == "Is it a cushion?"
        prompt = h.vision.requests[-1]["messages"][0]["content"]
        assert "It gives light" in prompt and "It is soft" in prompt and '"lamp"' in prompt
        done = await h.chat("yes")
        assert done["ispy_next_action"] == ("" if hosted else "prepare_robot_round")
        assert "ispy_player_candidates" not in h.state
        if hosted:
            assert done["game_state"] == "finished"

    asyncio.run(scenario())


@pytest.mark.parametrize("pool", [[], [CHAIR]])
def test_no_fit_refreshes_once_retains_clues_then_never_scans_again(monkeypatch, pool):
    h = VisualHarness(monkeypatch)

    async def scenario():
        token = await h.begin()
        await h.observe(pool)
        h.vision.reply = {"candidate_index": None}
        requests = len(h.vision.requests)
        retry = await h.chat("It is round")
        assert retry["ispy_next_action"] == "prepare_player_view"
        assert retry["ispy_view_token"] != token
        assert h.state["ispy_player_clues"] == ["It is round"]
        if not pool:
            assert len(h.vision.requests) == requests  # no freeform/text-only fallback
        with pytest.raises(h.module.web.HTTPConflict):
            await h.bridge.kids_ispy_observe(h.observation(token))
        await h.observe([])
        requests = len(h.vision.requests)
        for text in ("It is red", "It is small", "It is near me"):
            result = await h.chat(text)
            assert result["ispy_next_action"] == ""
            assert result["ispy_phase"] == "awaiting_clue"
            assert result["text"] == h.module._kids_copy("en", "ispy_player_no_fit")
        assert h.state["ispy_player_scans"] == 2
        assert len(h.vision.requests) == requests
        assert h.state["ispy_player_clues"][0] == "It is round"
        await h.chat("I give up")
        assert "ispy_player_candidates" not in h.state

    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [True, False, -1, 99, "lamp", 0.0, {}, "repeat", "freeform"])
def test_out_of_pool_malformed_or_repeated_guesses_never_spoken(monkeypatch, bad):
    h = VisualHarness(monkeypatch)

    async def scenario():
        await h.begin()
        await h.observe([CHAIR, LAMP])
        h.state["ispy_player_guesses"] = ["lamp"]
        h.vision.reply = ({"guess": "invented table"} if bad == "freeform" else {
            "candidate_index": 1 if bad == "repeat" else bad,
        })
        result = await h.chat("It is bright")
        assert result["ispy_next_action"] == "prepare_player_view"
        assert h.state["ispy_player_guesses"] == ["lamp"]
        assert "invented" not in result["text"]
        await h.observe([LAMP])
        h.vision.reply = {"candidate_index": 0}
        result = await h.chat("It shines")
        assert result["ispy_next_action"] == ""
        assert result["ispy_phase"] == "awaiting_clue"
        assert h.state["ispy_player_guesses"] == ["lamp"]

    asyncio.run(scenario())


@pytest.mark.parametrize("language,name", [
    ("en", "chair"), ("nl", "stoel"), ("es", "silla"), ("de", "Stuhl"),
    ("fr", "chaise"), ("it", "sedia"), ("pt", "cadeira"),
])
def test_observation_localizes_names_and_fixed_copy(monkeypatch, language, name):
    h = VisualHarness(monkeypatch, language=language)

    async def scenario():
        await h.begin()
        item = candidate(name)
        if language not in {"en", "nl"}:
            item[f"hints_{language}"] = [name]
        await h.observe([item])
        schema = h.vision.requests[-1]["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["candidates"]["minItems"] == 0
        name_schema = schema["properties"]["candidates"]["items"]["properties"]["object_name"]
        assert h.module._KIDS_LANGUAGE_NAMES[language] in name_schema["description"]
        assert h.module._ispy_response_schema(language)["properties"]["candidates"]["minItems"] == 1
        h.vision.reply = {"candidate_index": 0}
        result = await h.chat("blue")
        assert result["text"] == h.module._kids_copy(language, "ispy_guess", guess=name)
        for key in ("ispy_player_look", "ispy_player_refresh", "ispy_player_no_fit"):
            assert h.module._KIDS_COPY[language][key]
            if language != "en":
                assert h.module._KIDS_COPY[language][key] != h.module._KIDS_COPY["en"][key]

    asyncio.run(scenario())


def test_filters_unsafe_pool_and_moderates_every_retained_candidate(monkeypatch):
    h = VisualHarness(monkeypatch)

    async def scenario():
        await h.begin()
        h.flagged = ("lamp",)
        h.moderated.clear()
        result = await h.observe([candidate("knife"), CHAIR, LAMP, CUSHION])
        assert result["candidate_count"] == 2
        assert len(h.moderated) == 3
        assert [item["object_name"] for item in h.state["ispy_player_candidates"]] == ["chair", "cushion"]
        h.vision.reply = {"candidate_index": None}
        await h.chat("round")
        result = await h.observe([candidate("person"), candidate("phone")])
        assert result["candidate_count"] == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["flagged", "unavailable"])
@pytest.mark.parametrize("refresh", [False, True])
def test_failed_output_moderation_cannot_issue_or_consume_view_action(monkeypatch, failure, refresh):
    h = VisualHarness(monkeypatch)

    async def scenario():
        if refresh:
            await h.begin()
            await h.observe([])
        else:
            h.vision.reply = {"match": True}
        before = copy.copy(h.state)
        calls = 0

        async def moderation(_text, _key):
            nonlocal calls
            calls += 1
            if calls == 2:
                if failure == "unavailable":
                    raise h.module.web.HTTPServiceUnavailable()
                return True
            return False

        h.bridge._moderation_flagged = moderation
        result = await h.chat("round")
        assert result["ispy_next_action"] == ""
        assert "ispy_view_token" not in result
        assert "ispy_view_token" not in h.state
        for key in before:
            if key not in {"_turn_token", "history", "updated_at"}:
                assert h.state[key] == before[key]
        with pytest.raises(h.module.web.HTTPConflict):
            await h.bridge.kids_ispy_observe(h.observation("a" * 32))

    asyncio.run(scenario())


@pytest.mark.parametrize("case", [
    "unsolicited", "inactive", "wrong_phase", "wrong_token", "duplicate", "expired", "profile", "fixed_profile",
    "age", "language", "missing_token", "uppercase_token", "extra", "frames", "oversize", "auth",
])
def test_camera_admission_is_bounded_authenticated_and_never_creates_state(monkeypatch, case):
    h = VisualHarness(monkeypatch)

    async def scenario():
        token = await h.begin()
        request = h.observation(token)
        expected = h.module.web.HTTPConflict
        if case == "unsolicited":
            h.bridge._kids_sessions.clear()
            h.bridge._kids_live.clear()
        elif case == "inactive":
            h.bridge._kids_live.clear()
        elif case == "wrong_phase":
            h.state["ispy_player_phase"] = "awaiting_confirmation"
        elif case == "wrong_token":
            request.payload["view_token"] = "a" * 32
        elif case == "duplicate":
            await h.observe([])
        elif case == "expired":
            h.state["updated_at"] = time.monotonic() - h.module._KIDS_HISTORY_TTL_SECONDS - 1
        elif case == "profile":
            h.state["profile"] = ("7-9", "story", "en")
        elif case == "fixed_profile":
            h.bridge._kids_fixed_profiles[SESSION] = ("7-9", "ispy", "nl")
        elif case in {"age", "language"}:
            request.payload["age_band" if case == "age" else "language"] = "4-6" if case == "age" else "nl"
        elif case == "auth":
            request.headers = {}
            expected = h.module.web.HTTPUnauthorized
        else:
            expected = h.module.web.HTTPBadRequest
            if case == "missing_token":
                del request.payload["view_token"]
            elif case == "uppercase_token":
                request.payload["view_token"] = "A" * 32
            elif case == "extra":
                request.payload["hosted"] = True
            elif case == "frames":
                request.payload["frames_jpeg"] = FRAMES[:4]
            elif case == "oversize":
                request.payload["frames_jpeg"] = ["a" * 2_000_001] * 5
        before = dict(h.bridge._kids_live)
        count = len(h.vision.requests)
        with pytest.raises(expected):
            await h.bridge.kids_ispy_observe(request)
        assert len(h.vision.requests) == count
        assert h.bridge._kids_live == before
        if case == "unsolicited":
            assert not h.bridge._kids_sessions

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["vision", "moderation", "guess", "output"])
@pytest.mark.parametrize("invalidate", ["end", "cancel", "replace", "supersede", "expire"])
def test_held_results_cannot_revive_stale_player_views(monkeypatch, stage, invalidate):
    h = VisualHarness(monkeypatch)

    async def scenario():
        await h.begin()
        if stage in {"guess", "output"}:
            await h.observe([CHAIR])
        h.bridge._kids_speech_approvals.clear()
        gate = asyncio.Event()
        entered = asyncio.Event()
        if stage in {"vision", "guess"}:
            h.vision.gate = gate
            h.vision.requests.clear()
            h.vision.reply = {"candidates": [CHAIR]} if stage == "vision" else {"candidate_index": 0}
        else:
            calls = 0

            async def moderation(_text, _key):
                nonlocal calls
                calls += 1
                if stage == "moderation" or calls == 2:
                    entered.set()
                    await gate.wait()
                return False

            h.bridge._moderation_flagged = moderation
            h.vision.reply = {"candidates": [CHAIR]} if stage == "moderation" else {"candidate_index": 0}
        request = h.observation()
        task = asyncio.create_task(
            h.bridge.kids_ispy_observe(request) if stage in {"vision", "moderation"} else h.chat("It is blue")
        )
        await until(lambda: bool(h.vision.requests) if stage in {"vision", "guess"} else entered.is_set())
        if invalidate == "end":
            await h.end()
        elif invalidate == "cancel":
            await h.cancel()
        elif invalidate == "expire":
            h.bridge._kids_live[SESSION] = time.monotonic() - h.module._KIDS_LIVE_MAX_SECONDS - 1
        elif invalidate == "replace":
            h.bridge._kids_sessions[SESSION] = {
                "profile": ("7-9", "ispy", "en"), "history": [], "updated_at": time.monotonic(),
            }
        else:
            if stage in {"vision", "moderation"}:
                h.state["ispy_view_token"] = "f" * 32
            else:
                h.state["_turn_token"] = object()
        gate.set()
        with pytest.raises(h.module.web.HTTPConflict):
            await asyncio.wait_for(task, 1)
        assert not h.bridge._kids_speech_approvals
        if invalidate in {"end", "cancel", "expire"}:
            assert SESSION not in h.bridge._kids_sessions
        elif invalidate == "replace":
            assert "ispy_player_candidates" not in h.state
        else:
            assert h.state.get("ispy_player_phase") != "awaiting_confirmation"

    asyncio.run(scenario())


def test_inflight_observation_is_one_shot_and_action_not_readmitted(monkeypatch):
    h = VisualHarness(monkeypatch)

    async def scenario():
        await h.begin()
        h.vision.reply = {"candidates": []}
        h.vision.requests.clear()
        h.vision.gate = asyncio.Event()
        request = h.observation()
        task = asyncio.create_task(h.bridge.kids_ispy_observe(request))
        await until(lambda: bool(h.vision.requests))
        with pytest.raises(h.module.web.HTTPConflict):
            await h.bridge.kids_ispy_observe(request)
        result = await h.chat("look now")
        assert result["ispy_next_action"] == ""
        h.vision.gate.set()
        await task
        assert h.state["ispy_player_phase"] == "awaiting_clue"
        assert h.state["ispy_player_scans"] == 1

    asyncio.run(scenario())


def test_three_wrong_guesses_end_player_round_without_fourth_guess(monkeypatch):
    h = VisualHarness(monkeypatch)

    async def scenario():
        await h.begin()
        await h.observe([CHAIR, LAMP, CUSHION])
        for index in range(3):
            h.vision.reply = {"candidate_index": index}
            assert (await h.chat("It is in the room"))["ispy_phase"] == "awaiting_confirmation"
            reply = await h.chat("no")
        assert reply["ispy_phase"] == "awaiting_reveal"
        count = len(h.vision.requests)
        result = await h.chat("It was a ball")
        assert result["ispy_role"] == "reachy_pending"
        assert len(h.vision.requests) == count
        assert "ispy_player_candidates" not in h.state

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", [{"candidates": [CHAIR] * 5}, {"target": CHAIR}, {"candidates": "chair"}])
def test_observation_rejects_bad_envelopes_without_relaxing_selection_contract(monkeypatch, raw):
    h = VisualHarness(monkeypatch)

    async def scenario():
        token = await h.begin()
        h.vision.reply = raw
        with pytest.raises(h.module.web.HTTPBadGateway):
            await h.bridge.kids_ispy_observe(h.observation(token))
        assert h.state["ispy_player_candidates"] == []
        assert h.state["ispy_player_scans"] == 1
        assert h.state["ispy_player_phase"] == "awaiting_clue"
        with pytest.raises(h.module.web.HTTPConflict):
            await h.bridge.kids_ispy_observe(h.observation(token))
        with pytest.raises(ValueError):
            h.module._ispy_candidate_pool({"candidates": []}, frame_count=5)

    asyncio.run(scenario())


def test_view_token_is_not_admissible_until_looking_output_is_moderated(monkeypatch):
    h = VisualHarness(monkeypatch)

    async def scenario():
        h.vision.reply = {"match": True}
        gate, entered = asyncio.Event(), asyncio.Event()
        calls = 0

        async def moderation(_text, _key):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                await gate.wait()
            return False

        h.bridge._moderation_flagged = moderation
        task = asyncio.create_task(h.chat("chair"))
        await until(entered.is_set)
        assert "ispy_view_token" not in h.state
        assert h.state["ispy_role"] == "reachy_picker"
        with pytest.raises(h.module.web.HTTPConflict):
            await h.bridge.kids_ispy_observe(h.observation("a" * 32))
        gate.set()
        result = await task
        assert result["ispy_next_action"] == "prepare_player_view"
        assert result["ispy_view_token"] == h.state["ispy_view_token"]

    asyncio.run(scenario())


@pytest.mark.parametrize("mutate", ["profile", "view", "pool", "phase"])
def test_guess_held_across_in_place_round_change_is_rejected(monkeypatch, mutate):
    h = VisualHarness(monkeypatch)

    async def scenario():
        await h.begin()
        await h.observe([CHAIR])
        h.bridge._kids_speech_approvals.clear()
        h.vision.reply = {"candidate_index": 0}
        h.vision.requests.clear()
        h.vision.gate = asyncio.Event()
        task = asyncio.create_task(h.chat("blue"))
        await until(lambda: bool(h.vision.requests))
        if mutate == "profile":
            h.state["profile"] = ("7-9", "ispy", "nl")
        elif mutate == "view":
            h.state["ispy_view_token"] = "f" * 32
        elif mutate == "pool":
            h.state["ispy_player_candidates"] = [LAMP]
        else:
            h.state["ispy_player_phase"] = "awaiting_view"
        h.vision.gate.set()
        with pytest.raises(h.module.web.HTTPConflict):
            await task
        assert not h.bridge._kids_speech_approvals

    asyncio.run(scenario())


@pytest.mark.parametrize("pool,index", [([], None), ([CHAIR], 0)])
def test_native_subscription_transport_preserves_observe_and_index_schemas(monkeypatch, pool, index):
    from test_subscription_structured_output import codex_client, provider

    from companion import subscription_reasoning as sub

    h = VisualHarness(monkeypatch)
    _client, transport = codex_client(monkeypatch, [
        json.dumps({"match": True}), json.dumps({"candidates": pool}), json.dumps({"candidate_index": index}),
    ])
    h.bridge.llm = provider()
    h.bridge.http = sub.SubscriptionHttp(h.vision, h.bridge.llm)

    async def scenario():
        await h.begin()
        result = await h.observe(pool)
        assert result["candidate_count"] == len(pool)
        reply = await h.chat("blue")
        wire = transport.responses.calls
        schema = wire[1]["text"]["format"]["schema"]["properties"]["candidates"]
        assert schema["minItems"] == 0 and schema["maxItems"] == 4
        if pool:
            assert reply["text"] == "Is it a chair?"
            assert wire[2]["text"]["format"]["schema"]["properties"]["candidate_index"]["type"] == ["integer", "null"]
        else:
            assert len(wire) == 2
            assert reply["ispy_next_action"] == "prepare_player_view"
        assert not h.vision.requests

    asyncio.run(scenario())


def test_observation_failure_does_not_loop_scans_or_keep_old_pool(monkeypatch):
    h = VisualHarness(monkeypatch)

    async def scenario():
        await h.begin()
        await h.observe([CHAIR])
        h.vision.reply = {"candidate_index": None}
        refresh = await h.chat("round")
        h.moderation_unavailable = True
        h.vision.reply = {"candidates": [LAMP]}
        with pytest.raises(h.module.web.HTTPServiceUnavailable):
            await h.bridge.kids_ispy_observe(h.observation(refresh["ispy_view_token"]))
        assert h.state["ispy_player_candidates"] == []
        assert h.state["ispy_player_scans"] == 2
        h.moderation_unavailable = False
        assert (await h.chat("yellow"))["ispy_next_action"] == ""

    asyncio.run(scenario())
