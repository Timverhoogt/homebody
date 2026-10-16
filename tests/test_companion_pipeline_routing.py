"""Exercise the actual continued Pipeline route in both purposes without project binding."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from test_companion import calendar_offer, make_runtime, offers_runtime, someone_present

from homebody.audio import EndpointResult


@pytest.mark.parametrize('profile', ['conversation', 'agent'])
def test_pipeline_looks_follows_up_and_refreshes_without_project_or_everyday_tools(profile):
    runtime, client, captures = make_runtime(conversation_mode='pipeline', continuous_conversation=True)
    runtime._capability_profile = profile
    runtime._motion = None
    config = runtime.config_loader()
    client.config = config
    client.last_stt_provider = 'fixture'
    client.health = lambda: {'status': 'ok'}
    words = ['Look at this letter', 'And what day?', 'Kijk nog eens']
    client.transcribe = lambda _audio: words.pop(0)
    client.synthesize = lambda _text: SimpleNamespace(provider='fixture')
    leaked = []
    client.ask_agent = lambda *a, **k: leaked.append(('project', a))
    runtime._begin_agent_request = lambda *a, **k: pytest.fail('A look must not require a project session')
    runtime._play_asset = lambda _name: None
    runtime._discard_audio = lambda _seconds: None
    runtime._record_utterance = lambda _config: EndpointResult(
        np.ones(160, dtype=np.float32) if words else np.empty(0, dtype=np.float32),
        bool(words), 'end_silence' if words else 'initial_timeout', 0.01,
    )
    spoken = []
    runtime._play_response = lambda _speech, text, **kwargs: spoken.append(text) or False
    with runtime._voice_activity_lock:
        runtime._run_conversation(config)
    assert words == []
    assert captures == [1, 1]
    assert client.turns == [] and leaked == []
    assert len(client.follow_ups) == 1
    assert spoken == [client.answer, 'Friday at ten, according to what I read.', client.answer]
    assert runtime._look_context is None


def test_unsolicited_offers_really_pause_after_announcement_failure():
    runtime, _client, _captures = offers_runtime()
    someone_present(runtime)
    runtime._status.announcement_last_error = 'fixture synthesis failure'
    result = runtime.submit_contextual_offer(calendar_offer())
    assert result['queued'] is False and result['reason'] == 'voice_unavailable'
