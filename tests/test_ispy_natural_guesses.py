"""Natural-guess contracts; semantic accuracy is separately checked with the real provider."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from test_ispy_target_variety import SESSION, Harness, Request, candidate


class JudgeHTTP:
    def __init__(self, decision=True):
        self.decision = decision
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append(copy.deepcopy(kwargs["json"]))
        return self

    async def __aenter__(self):
        self.status = 200
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        return {"choices": [{"message": {"content": json.dumps({"match": self.decision})}}]}


@pytest.mark.parametrize("guess", ["car", "Is it the car?", "auto", "Is het dat autootje?"])
def test_real_judge_transports_original_guess_and_observation_as_untrusted_data(monkeypatch, guess):
    target = candidate("yellow car", category="toy vehicle", colour="yellow", hints_en=["It has wings and wheels."])
    h = Harness(monkeypatch, [target])
    http = JudgeHTTP()
    h.bridge.http = http
    before = copy.deepcopy(target)
    assert asyncio.run(h.bridge._judge_ispy_guess(guess, target, language="nl", openai_key="test")) is True
    messages = http.requests[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "optional" in messages[0]["content"] and "colour" in messages[0]["content"]
    assert "different object" in messages[0]["content"]
    data = json.loads(messages[1]["content"])
    assert messages[1]["role"] == "user" and data["guess"] == guess
    assert data["target"]["object_name"] == "yellow car"
    assert data["target"]["hints_en"] == ["It has wings and wheels."]
    assert "bbox" not in data["target"] and "frame_index" not in data["target"]
    assert target == before


@pytest.mark.parametrize("matched", [True, False])
def test_real_chat_route_uses_judge_without_changing_target_or_bypassing_moderation(monkeypatch, matched):
    h = Harness(monkeypatch, [candidate("yellow car", colour="yellow", category="toy vehicle")])
    selected = h.run()["target"]
    http = JudgeHTTP(matched)
    h.bridge.http = http
    payload = json.loads(
        asyncio.run(
            h.bridge.kids_chat(
                Request(
                    {
                        "input": "Is it the car?",
                        "session_id": SESSION,
                        "profile": {"age_band": "7-9", "activity": "ispy", "language": "en"},
                    }
                )
            )
        ).text
    )
    assert len(http.requests) == 1 and http.requests[0]["response_format"]["json_schema"]["name"] == "ispy_match"
    assert h.moderated[0] == "Is it the car?" and len(h.moderated) >= 2
    if matched:
        assert payload["ispy_role"] == "player_picker"
        assert "ispy_target" not in h.bridge._kids_sessions[SESSION]
    else:
        assert payload["ispy_role"] == "reachy_picker"
        assert h.bridge._kids_sessions[SESSION]["ispy_target"] == selected


def test_judge_rejects_non_boolean_decision(monkeypatch):
    h = Harness(monkeypatch, [])
    h.bridge.http = JudgeHTTP("yes")
    with pytest.raises(h.module.web.HTTPBadGateway):
        asyncio.run(h.bridge._judge_ispy_guess("car", candidate("yellow car"), language="en", openai_key="test"))


@pytest.mark.parametrize("language", ["en", "nl"])
def test_vision_naming_separates_colour_and_handles_ambiguous_wheeled_toys(monkeypatch, language):
    h = Harness(monkeypatch, [])
    props = h.module._ispy_response_schema(language)["properties"]["candidates"]["items"]["properties"]
    description = props["object_name"]["description"]
    assert "colour" in description and "wheels alone" in description
    assert "broader" in description and "toy" in description


@pytest.mark.parametrize("language", ["en", "nl", "es", "de", "fr", "it", "pt"])
def test_success_does_not_reassert_a_fallible_vision_label(monkeypatch, language):
    h = Harness(monkeypatch, [])
    text, count, hints, done = h.module._ispy_reply(
        candidate("fallible-vision-label"),
        language=language,
        matched=True,
        previous_count=0,
        intent="answer",
        hints_given=0,
    )
    assert done and count == 1 and hints == 0
    assert "fallible-vision-label" not in text
    assert text == h.module._kids_copy(language, "right")


def test_judge_diagnostics_do_not_retain_guess_or_target(monkeypatch, caplog):
    h = Harness(monkeypatch, [])
    h.bridge.http = JudgeHTTP()
    with caplog.at_level("INFO", logger="hermes_reachy_bridge"):
        asyncio.run(
            h.bridge._judge_ispy_guess(
                "private-guess-marker", candidate("private-target-marker"), language="en", openai_key="test"
            )
        )
    assert "match=True" in caplog.text
    assert "private-guess-marker" not in caplog.text and "private-target-marker" not in caplog.text
