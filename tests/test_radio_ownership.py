"""Radio speaker ownership across a real announcement/new-conversation handoff."""
import threading
from types import SimpleNamespace

from test_everyday_runtime import runtime
from test_radio import Output, finish, install_fake_decoder


def test_old_announcement_cleanup_cannot_resume_radio_under_new_voice(monkeypatch):
    install_fake_decoder(monkeypatch)
    r, config = runtime(api_key='synthetic-local-only')
    r._audio_ready = True
    r._announcement_worker = object()
    r._playback = Output()
    r._new_bridge_client = lambda config: SimpleNamespace(
        synthesize=lambda text: SimpleNamespace(provider='synthetic'), close=lambda: None
    )
    r._play_announcement_audio = lambda *args: None
    old_end_entered, release_old_end, old_end_completed = (threading.Event() for _ in range(3))
    voice_entered, release_voice = threading.Event(), threading.Event()
    original_end = r._radio_voice_end

    def held_end():
        if threading.current_thread().name == 'old-announcement':
            old_end_entered.set()
            assert release_old_end.wait(3)
            original_end()
            old_end_completed.set()
        else:
            original_end()

    r._radio_voice_end = held_end

    def held_conversation(*args):
        voice_entered.set()
        assert release_voice.wait(3)

    r._run_selected_voice_with_radio_paused = held_conversation
    assert r.execute_everyday_tool('start_reachy_radio', {})['ok']
    assert r._playback.received.wait(1)
    r.queue_announcement('Synthetic silent line', behavior='voice_only')
    announcement = threading.Thread(target=r._run_announcement_worker, name='old-announcement', daemon=True)

    def new_wake():
        with r._voice_activity_lock:
            r._run_selected_voice_conversation(config, 'button')

    voice = threading.Thread(target=new_wake, name='new-conversation', daemon=True)
    try:
        announcement.start()
        assert old_end_entered.wait(2)
        assert not r._announcement_active.is_set()
        voice.start()
        assert voice_entered.wait(1) and r._radio.paused
        count = len(r._playback.samples)
        release_old_end.set()
        assert old_end_completed.wait(1)
        assert voice.is_alive() and r._radio.paused and r._radio_voice_active
        r._playback.received.clear()
        assert not r._playback.received.wait(0.1)
        assert len(r._playback.samples) == count
        release_voice.set()
        voice.join(1)
        assert not voice.is_alive()
        assert r._playback.received.wait(1)  # Positive control: last owner releases radio.
    finally:
        r.stop_event.set()
        release_old_end.set()
        release_voice.set()
        announcement.join(2)
        if voice.ident is not None:
            voice.join(2)
        finish(r._radio)
