"""Local field regressions: no SDK, robot, credentials or provider access."""
import asyncio
import json
import threading
import time

import httpx
import pytest
from test_kids_end_single_fold import Rig
from test_motion import FakeRobot

from companion.hermes_reachy_bridge import _brief_kids_reply, _build_bridge_kids_prompt
from homebody.config import AppConfig
from homebody.kids_mode import KidsProfile, build_kids_prompt
from homebody.main import Homebody
from homebody.motion import VoiceMotion


def test_sleep_sdk_stop_can_drain_callback_without_shared_lock_cycle():
    robot = FakeRobot()
    motion = VoiceMotion(robot)
    motion.speaking()
    generation = motion._wobble_generation
    callback_done = threading.Event()

    def stop_playback():
        # Models an SDK stop waiting for a previously admitted callback to return.
        def callback():
            motion._emit_offsets((0.0,) * 6, generation)
            callback_done.set()
        callback_thread = threading.Thread(target=callback, daemon=True)
        callback_thread.start()
        assert callback_done.wait(1), "SDK stop deadlocked behind the motion lock"
        callback_thread.join(1)

    motion.prepare_sleep(stop_playback)
    assert callback_done.is_set()
    assert robot.wobbling_events == [True, False]
    motion.speaking()
    assert robot.wobbling_events == [True, False]


def test_end_revokes_voice_before_waiting_for_motor_owner(monkeypatch):
    rig = Rig(monkeypatch, wakes_at={})
    rig.start_kids()
    runtime = rig.runtime
    runtime._accept_wake_turn()
    done = threading.Event()
    result = {}
    def end():
        result.update(runtime.stop_kids_mode())
        done.set()
    with runtime._motor_transition_lock:
        worker = threading.Thread(target=end, daemon=True)
        worker.start()
        assert runtime._conversation_stop_requested.wait(1)
        assert not done.is_set()
        assert not runtime._kids_active
    worker.join(3)
    assert done.is_set()
    assert result['last_fold_succeeded'] is True
    assert rig.daemon.sleeps == 1
    assert not rig.enables_after_first_release()


def test_management_lock_wait_does_not_park_asgi_loop_or_end(monkeypatch):
    import homebody.main as main
    monkeypatch.setattr(main, 'load_config', lambda: AppConfig())
    entered, release = threading.Event(), threading.Event()

    class Runtime:
        @property
        def kids_controls_locked(self):
            entered.set()
            release.wait(2)
            return True

        def stop_kids_mode(self, **_kwargs):
            return {'active': False, 'last_fold_succeeded': True}

    app = Homebody(False)
    app._runtime = Runtime()

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.settings_app),
                                    base_url='http://test') as client:
            started = time.monotonic()
            pending = asyncio.create_task(client.get('/api/unused-management'))
            try:
                assert await asyncio.to_thread(entered.wait, 1)
                # A blocked management lock must not delay an admitted End request.
                response = await asyncio.wait_for(client.post('/api/kids/stop'), .5)
                assert not release.is_set()
                assert response.status_code == 200
                assert time.monotonic() - started < 1, "management lock blocked the ASGI event loop"
            finally:
                release.set()
            assert (await pending).status_code == 423
    asyncio.run(exercise())


@pytest.mark.parametrize('age,limit', [('4-6', 30), ('7-9', 45), ('10-12', 60)])
@pytest.mark.parametrize('activity', ['buddy', 'story'])
@pytest.mark.parametrize('hosted', [False, True])
def test_age_brevity_prompts_and_complete_sentence_budget(age, limit, activity, hosted):
    robot_prompt = build_kids_prompt(KidsProfile(age_band=age, activity=activity, host=hosted))
    host_prompt = _build_bridge_kids_prompt(age_band=age, activity=activity, language='en', hosted=hosted)
    for prompt in (robot_prompt, host_prompt):
        assert (f'under {limit} words' in prompt) == (activity == 'buddy' and not hosted)
        assert ('two short sentences' in prompt) == (activity == 'buddy' and not hosted)
        if activity == 'story':
            assert 'two simple choices' in prompt
    answer = 'A little fox found a red kite. Which way should it go? Then ' + 'more ' * 80 + '.'
    assert _brief_kids_reply(answer, age, 'en') == 'A little fox found a red kite. Which way should it go?'
    fallback = _brief_kids_reply('word ' * 100 + '.', age, 'nl')
    assert len(fallback.split()) <= limit
    assert 'word' not in fallback


@pytest.mark.parametrize('hosted', [False, True])
def test_story_preserves_choices_completion_and_exact_screened_speech(monkeypatch, hosted):
    from test_kids_story_ispy_speech import ModelHttp, chat, story_bridge

    answer = ('The fox found a kite. It belonged to a friendly bear. '
              'Together they flew it over the meadow. Would you choose the red kite or the blue kite?')
    content = json.dumps({'reply': answer, 'game_state': 'finished'}) if hosted else answer
    module, bridge = story_bridge(monkeypatch, ModelHttp(content=content))
    screened = []

    async def moderation(text, _key):
        screened.append(text)
        return False

    bridge._moderation_flagged = moderation
    payload = json.loads(asyncio.run(bridge.kids_chat(chat('en', hosted=hosted))).text)
    assert payload['text'] == answer
    assert answer in screened
    if hosted:
        assert payload['game_state'] == 'finished'
    assert bridge._kids_sessions[next(iter(bridge._kids_sessions))]['history'][-1]['content'] == answer
