"""Offline regressions for focused review findings; no provider or hardware access."""
import asyncio
import json

import pytest
from test_ispy_target_variety import CHAIR, SESSION, Harness
from test_kids_story_ispy_speech import ModelHttp, chat, story_bridge


def test_flagged_input_held_across_end_cannot_issue_approval(monkeypatch):
    module, bridge = story_bridge(monkeypatch, ModelHttp())

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def flagged(_text, _key):
            entered.set()
            await release.wait()
            return True

        bridge._moderation_flagged = flagged
        task = asyncio.create_task(bridge.kids_chat(chat()))
        await entered.wait()
        bridge._end_kids_live(SESSION)
        bridge._kids_sessions.pop(SESSION, None)
        release.set()
        with pytest.raises(module.web.HTTPConflict):
            await task
        assert not bridge._kids_speech_approvals

    asyncio.run(scenario())


def test_subscription_provider_unavailable_returns_fixed_retry(monkeypatch):
    module, bridge = story_bridge(monkeypatch, ModelHttp())

    class Unavailable:
        def post(self, *_args, **_kwargs):
            raise module.web.HTTPServiceUnavailable(text="subscription provider failed")

    bridge.http = Unavailable()
    payload = json.loads(asyncio.run(bridge.kids_chat(chat())).text)
    assert payload["retry"] is True
    assert payload["text"] == module._KIDS_MODEL_RETRY_REPLY["nl"]
    assert bridge._kids_sessions[SESSION]["history"] == []


def test_ispy_cannot_override_fixed_speech_language_pin(monkeypatch):
    harness = Harness(monkeypatch, [CHAIR])
    harness.bridge._kids_fixed_profiles[SESSION] = ("7-9", "ispy", "nl")
    # Harness selects English. A previously pinned Dutch greeting cannot silently switch voice.
    with pytest.raises(harness.module.web.HTTPConflict):
        harness.run()
    assert SESSION not in harness.bridge._kids_sessions
    assert not harness.bridge._kids_speech_approvals
