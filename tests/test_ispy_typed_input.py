import threading
import time
from types import SimpleNamespace

import pytest

from homebody.config import AppConfig
from homebody.kids_mode import KidsProfile
from homebody.runtime import HermesVoiceRuntime


@pytest.fixture
def game(monkeypatch):
    config = AppConfig()
    rt = HermesVoiceRuntime(SimpleNamespace(), threading.Event(), config_loader=lambda: config)
    monkeypatch.setattr(rt, "_effective_power_mode", lambda: "awake")
    rt._kids_active = rt._kids_locked = True
    rt._kids_profile = KidsProfile(activity="ispy", camera_consent=True)
    rt._kids_session_id = "kids-" + "b" * 32
    rt._kids_ends_at = time.monotonic() + 600
    rt._motors_enabled = True
    calls = []
    client = SimpleNamespace(
        config=rt._kids_voice_config(config),
        health=lambda: {},
        last_stt_provider="unused",
        last_answer_source={},
        last_kids_ispy_role="robot_picker",
        last_kids_ispy_phase="guessing",
        last_kids_next_action="",
        close=lambda: None,
    )

    def chat(text):
        calls.append(("moderated_chat", text))
        return "Try another object!"

    client.chat = chat
    client.transcribe = lambda *_: pytest.fail("Typed input must not invoke STT")
    client.everyday_turn = lambda *_: pytest.fail("No adult route")
    client.ask_agent = lambda *_args, **_kw: pytest.fail("No full agent route")
    monkeypatch.setattr(rt, "_new_bridge_client", lambda _: client)
    monkeypatch.setattr(rt, "_play_kids_stream", lambda *_a, **_kw: calls.append(("approved_stream",)) or False)
    monkeypatch.setattr(rt, "_record_utterance", lambda *_a, **_kw: pytest.fail("No microphone recording"))
    monkeypatch.setattr(rt, "_play_asset", lambda *_a: pytest.fail("No input earcons"))
    return rt, calls, client


def test_typed_guess_shares_kids_reply_path_without_stt(game):
    rt, calls, _ = game
    token = rt.ispy_input_context()["token"]
    assert rt.submit_ispy_guess("  blue cup  ", token) == {"accepted": True}
    assert calls == [("moderated_chat", "blue cup"), ("approved_stream",)]
    assert rt.ispy_input_context()["token"] != token
    with pytest.raises(RuntimeError):
        rt.submit_ispy_guess("blue cup", token)
    assert "blue cup" not in str(rt.status())


@pytest.mark.parametrize(
    "block", ["inactive", "wrong_activity", "expired", "privacy", "standby", "camera", "busy", "announcement"]
)
def test_typed_guess_fails_closed(game, block, monkeypatch):
    rt, calls, _ = game
    token = rt.ispy_input_context()["token"]
    if block == "inactive":
        rt._kids_active = False
    elif block == "wrong_activity":
        rt._kids_profile = KidsProfile(activity="story")
    elif block == "expired":
        rt._kids_ends_at = time.monotonic() - 1
    elif block == "privacy":
        rt._privacy_requested.set()
    elif block == "standby":
        monkeypatch.setattr(rt, "_effective_power_mode", lambda: "standby")
    elif block == "camera":
        rt._kids_camera_active = True
    elif block == "busy":
        rt._voice_activity_lock.acquire()
    elif block == "announcement":
        rt._announcement_active.set()
    assert rt.ispy_input_context() == {"available": False, "token": ""}
    with pytest.raises(RuntimeError):
        rt.submit_ispy_guess("cup", token)
    assert not calls


@pytest.mark.parametrize("text", ["", "   ", "a" * 81])
def test_typed_guess_rejects_invalid_length(game, text):
    rt, calls, _ = game
    with pytest.raises(ValueError):
        rt.submit_ispy_guess(text, rt.ispy_input_context()["token"])
    assert not calls


def test_end_during_model_response_cannot_speak(game):
    rt, calls, client = game
    token = rt.ispy_input_context()["token"]

    def late_reply(text):
        rt._kids_active = False
        return "This reply must not play"

    client.chat = late_reply
    with pytest.raises(RuntimeError):
        rt.submit_ispy_guess("cup", token)
    assert not calls


def test_expiry_during_model_response_cannot_speak(game):
    rt, calls, client = game
    token = rt.ispy_input_context()["token"]

    def late_reply(text):
        rt._kids_ends_at = time.monotonic() - 1
        return "This reply must not play"

    client.chat = late_reply
    with pytest.raises(RuntimeError):
        rt.submit_ispy_guess("cup", token)
    assert not calls


def test_stop_during_config_admission_rejects_old_token(game):
    rt, calls, _ = game
    token = rt.ispy_input_context()["token"]
    original = rt.config_loader

    def slow_config():
        rt._request_conversation_stop()
        return original()

    rt.config_loader = slow_config
    with pytest.raises(RuntimeError):
        rt.submit_ispy_guess("cup", token)
    assert not calls
    assert rt._conversation_stop_requested.is_set()


def test_new_wake_cannot_resurrect_a_stopped_typed_turn(game):
    rt, calls, client = game
    token = rt.ispy_input_context()["token"]

    def reply_after_stop_and_new_wake(text):
        rt._request_conversation_stop()
        rt._accept_wake_turn()
        return "Must not speak"

    client.chat = reply_after_stop_and_new_wake
    with pytest.raises(RuntimeError):
        rt.submit_ispy_guess("cup", token)
    assert not calls
    assert rt._typed_stop_generation is None


def test_stop_invalidates_old_token_even_when_new_context_is_requested(game):
    rt, calls, _ = game
    token = rt.ispy_input_context()["token"]
    rt._request_conversation_stop()
    assert rt.ispy_input_context()["token"] != token
    with pytest.raises(RuntimeError):
        rt.submit_ispy_guess("cup", token)
    assert not calls
