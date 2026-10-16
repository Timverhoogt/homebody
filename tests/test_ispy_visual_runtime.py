"""Visual player-picker admission and cancellation; synthetic HTTP, media and motion only."""
from __future__ import annotations

import base64
import json
import threading
from dataclasses import replace

import httpx
import pytest
from test_ispy_expected_cancellation import Rig

from homebody.config import AppConfig
from homebody.hermes_client import HermesBridgeClient, HermesBridgeError
from homebody.ispy import ISpyCancelled
from homebody.kids_game_master import ROUND_TURN_LIMITS

TOKEN = "a1" * 16
SESSION = "kids-" + "b" * 32


def view_reply(**updates):
    return {
        "text": "I will look around. Then tell me your clue.",
        "speech_approval": "approved-text",
        "fallback_speech_approval": "approved-fallback",
        "ispy_next_action": "prepare_player_view",
        "ispy_role": "player_picker",
        "ispy_phase": "awaiting_view",
        "ispy_view_token": TOKEN,
        "game_state": "playing",
        **updates,
    }


def ordinary_reply():
    return view_reply(ispy_next_action="", ispy_view_token="", ispy_phase="awaiting_confirmation")


def bridge_client(payload, *, hosted=False):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    transport = httpx.Client(transport=httpx.MockTransport(handle))
    config = AppConfig(
        api_key="synthetic", bridge_url="http://synthetic.invalid", kids_mode_enabled=True,
        kids_session_id=SESSION, kids_age_band="7-9", kids_activity="ispy", kids_host_enabled=hosted,
    )
    return HermesBridgeClient(config, client=transport), requests


@pytest.mark.parametrize("hosted", [False, True])
def test_client_accepts_exact_view_metadata_and_resets_every_chat(hosted):
    payload = view_reply()
    client, requests = bridge_client(payload, hosted=hosted)
    assert client.chat("I picked something") == payload["text"]
    assert client.last_kids_next_action == "prepare_player_view"
    assert client.last_kids_ispy_view_token == TOKEN
    assert json.loads(requests[-1].content).get("hosted", False) is hosted
    payload.clear()
    payload.update(ordinary_reply())
    client.chat("no")
    assert client.last_kids_next_action == client.last_kids_ispy_view_token == ""
    payload.update(view_reply())
    client.chat("look")
    with pytest.raises(HermesBridgeError):
        client.chat(" ")
    assert client.last_kids_ispy_view_token == client.last_kids_next_action == ""


@pytest.mark.parametrize("updates", [
    {"ispy_view_token": None}, {"ispy_view_token": ""}, {"ispy_view_token": TOKEN.upper()},
    {"ispy_view_token": "a" * 31}, {"ispy_view_token": "a" * 33}, {"ispy_view_token": "g" * 32},
    {"ispy_view_token": " " + TOKEN}, {"ispy_view_token": 123},
    {"ispy_next_action": "http://elsewhere/observe"}, {"ispy_next_action": []},
    {"ispy_next_action": ""}, {"ispy_role": "reachy_picker"}, {"ispy_role": []},
    {"ispy_phase": "awaiting_clue"}, {"ispy_phase": []},
])
def test_client_rejects_incoherent_action_metadata(updates):
    client, _ = bridge_client(view_reply(**updates))
    with pytest.raises(HermesBridgeError):
        client.chat("look")
    assert client.last_kids_next_action == client.last_kids_ispy_view_token == ""
    assert client._kids_speech_approval == ""


def test_missing_token_is_not_repaired():
    payload = view_reply()
    del payload["ispy_view_token"]
    client, _ = bridge_client(payload)
    with pytest.raises(HermesBridgeError):
        client.chat("look")


@pytest.mark.parametrize("hosted", [False, True])
def test_old_robot_round_action_retained_only_for_standalone(hosted):
    client, _ = bridge_client(view_reply(
        ispy_next_action="prepare_robot_round", ispy_role="reachy_pending",
        ispy_phase="complete", ispy_view_token="",
    ), hosted=hosted)
    if hosted:
        with pytest.raises(HermesBridgeError):
            client.chat("yes")
        assert client.last_kids_next_action == ""
    else:
        client.chat("yes")
        assert client.last_kids_next_action == "prepare_robot_round"


@pytest.mark.parametrize("outcome", [None, [], "unknown"])
def test_invalid_hosted_outcome_never_publishes_view_action(outcome):
    client, _ = bridge_client(view_reply(game_state=outcome), hosted=True)
    with pytest.raises(HermesBridgeError):
        client.chat("look")
    assert client.last_kids_next_action == client.last_kids_ispy_view_token == ""


def observe(client, frames=None, token=TOKEN):
    return client.observe_ispy_scene(
        [b"jpeg"] * 5 if frames is None else frames,
        session_id=SESSION, age_band="7-9", language="nl", view_token=token,
    )


@pytest.mark.parametrize("count", [0, 1, 4])
def test_observe_fixed_route_exact_frames_no_target_or_speech(count):
    client, requests = bridge_client({"observed": True, "candidate_count": count, "view_token": TOKEN})
    assert observe(client) == count
    [request] = requests
    assert request.url.path == "/v1/kids/ispy/observe" and request.method == "POST"
    assert json.loads(request.content) == {
        "session_id": SESSION, "age_band": "7-9", "language": "nl", "view_token": TOKEN,
        "frames_jpeg": [base64.b64encode(b"jpeg").decode()] * 5,
    }
    assert client._kids_speech_approval == ""


@pytest.mark.parametrize("patch", [
    {"observed": 1}, {"observed": "true"}, {"observed": False},
    {"candidate_count": True}, {"candidate_count": 1.0}, {"candidate_count": "1"},
    {"candidate_count": -1}, {"candidate_count": 5}, {"candidate_count": None},
    {"view_token": "c" * 32}, {"view_token": None},
])
def test_observe_receipt_is_strict(patch):
    client, _ = bridge_client({"observed": True, "candidate_count": 2, "view_token": TOKEN, **patch})
    with pytest.raises(HermesBridgeError):
        observe(client)


@pytest.mark.parametrize("payload", [None, [], {}, "observed"])
def test_observe_rejects_malformed_receipt(payload):
    client, _ = bridge_client(payload)
    with pytest.raises(HermesBridgeError):
        observe(client)


@pytest.mark.parametrize("frames", [[], [b"x"] * 4, [b"x"] * 6, [b""] * 5, [b"x" * 1_500_001] * 5, ["x"] * 5])
def test_observe_bounds_fail_before_http(frames):
    client, requests = bridge_client({})
    with pytest.raises(HermesBridgeError):
        observe(client, frames)
    assert requests == []


def test_observe_invalid_token_fails_before_http():
    client, requests = bridge_client({})
    with pytest.raises(HermesBridgeError):
        observe(client, token=TOKEN.upper())
    assert requests == []


class VisualRig(Rig):
    """Real client parsing, real Pipeline dispatch and real scan, with fixed fake boundaries."""

    def __init__(self, monkeypatch, tmp_path, *, hosted=False, frame=lambda: b"jpeg"):
        super().__init__(monkeypatch, tmp_path, frame=frame)
        self.reply = view_reply()
        self.observations = []
        self.clients = []
        self.on_observe = lambda: None
        self.on_chat = lambda: None
        self.observe_error = None
        self.spoken = []
        self.host_offers = []
        runtime = self.runtime
        runtime.set_power_mode("awake")  # Rig's fake confirms motors; no hardware call
        if hosted:
            runtime._kids_profile = replace(runtime._kids_profile, host=True)
            runtime._kids_host_phase = "playing"
            runtime._kids_host_activity = "ispy"
            self.config = runtime._kids_voice_config(self.config)
        rig = self

        def handle(request):
            body = json.loads(request.content)
            if request.url.path == "/v1/kids/chat":
                rig.on_chat()
                return httpx.Response(200, json=rig.reply)
            if request.url.path == "/v1/kids/ispy/observe":
                rig.events.append("observe")
                rig.observations.append(body)
                rig.on_observe()
                if rig.observe_error:
                    raise rig.observe_error
                return httpx.Response(200, json={
                    "observed": True, "candidate_count": 2, "view_token": body["view_token"],
                })
            if request.url.path == "/v1/kids/ispy/cancel":
                rig.cancelled_sessions.append(body["session_id"])
                return httpx.Response(200, json={})
            pytest.fail(f"Unexpected endpoint: {request.url.path}")

        def make_client(config):
            client = HermesBridgeClient(config, client=httpx.Client(transport=httpx.MockTransport(handle)))
            client.health = lambda: {"status": "ok", "stt_locality": "local"}
            client.transcribe = lambda _audio: "a blue thing"
            rig.clients.append(client)
            return client

        def speak(_client, text, **_kwargs):
            rig.events.append("tts")
            rig.spoken.append(text)
            return False

        def host_offer(generation, **kwargs):
            rig.host_offers.append((generation, kwargs))
            return True

        runtime._new_bridge_client = make_client
        runtime._play_kids_stream = speak
        runtime._kids_host_offer = host_offer

    def prepared_client(self):
        self.runtime._accept_wake_turn()
        client = self.runtime._new_bridge_client(self.config)
        client.chat("look")
        return client


@pytest.fixture
def make_rig(monkeypatch, tmp_path):
    rigs = []

    def make(**kwargs):
        rig = VisualRig(monkeypatch, tmp_path, **kwargs)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        for timer in (rig.runtime._kids_timer, rig.runtime._kids_warning_timer):
            if timer:
                timer.cancel()
        for client in rig.clients:
            client._client.close()


@pytest.mark.parametrize("hosted", [False, True])
def test_pipeline_approved_handoff_then_five_frame_observe_and_normal_listen(make_rig, hosted):
    rig = make_rig(hosted=hosted)
    rig.run()
    assert rig.events[0] == "tts"
    assert rig.events.count("camera") == 6  # one discarded readiness + five retained
    assert rig.events.count("motion") == 9
    assert rig.events[-1] == "observe"
    assert rig.spoken == [rig.reply["text"]]  # observe never invents speech or a target/clue
    [body] = rig.observations
    assert body["session_id"] == rig.session_id and body["view_token"] == TOKEN
    assert body["language"] == "nl" and body["frames_jpeg"] == ["anBlZw=="] * 5
    assert any(name == "record" for name, _ in rig.harness.calls)
    assert sum(name == "record" for name, _ in rig.harness.calls) > 1  # ordinary next-listen loop
    assert not rig.stops and not rig.host_offers
    assert rig.clients[0].last_kids_next_action == rig.clients[0].last_kids_ispy_view_token == ""
    assert rig.runtime._kids_camera_active is False


@pytest.mark.parametrize("hosted", [False, True])
def test_typed_view_scans_once_without_microphone_or_followup(make_rig, hosted):
    rig = make_rig(hosted=hosted)
    rig.runtime._record_utterance = lambda *_: pytest.fail("Typed input must not open a microphone")
    token = rig.runtime.ispy_input_context()["token"]
    assert rig.runtime.submit_ispy_guess("a blue thing", token) == {"accepted": True}
    assert len(rig.observations) == 1 and rig.spoken == [rig.reply["text"]]
    assert rig.events.count("camera") == 6


@pytest.mark.parametrize("hosted", [False, True])
def test_ordinary_player_guess_only_keeps_subtle_gesture_not_camera(make_rig, hosted):
    rig = make_rig(hosted=hosted)
    rig.reply = ordinary_reply()
    rig.run()
    assert rig.events == ["motion", "tts"]
    assert not rig.observations


def test_awaiting_view_state_alone_never_authorizes_capture(make_rig):
    rig = make_rig()
    rig.reply = view_reply(ispy_next_action="", ispy_view_token="")
    rig.run()
    assert rig.events == ["tts"] and not rig.observations


@pytest.mark.parametrize("finish", ["bridge", "turn_limit"])
def test_host_completion_precedes_view_dispatch(make_rig, finish):
    rig = make_rig(hosted=True)
    if finish == "bridge":
        rig.reply["game_state"] = "finished"
    else:
        rig.runtime._kids_host_round_turns = ROUND_TURN_LIMITS["ispy"] - 1
    rig.run()
    assert rig.events == ["tts"] and len(rig.host_offers) == 1
    assert not rig.observations


def test_camera_opt_in_required_no_motion_or_capture(make_rig):
    rig = make_rig()
    # Profile construction already rejects this; exercise the scan's independent consent guard too.
    object.__setattr__(rig.runtime._kids_profile, "camera_consent", False)
    with pytest.raises(RuntimeError, match="Camera opt-in"):
        rig.run()
    assert rig.events == ["tts"] and not rig.observations
    assert [s["reason"] for s in rig.stops] == ["ispy_view_failed"]


def test_view_dispatch_consumes_token_and_rejects_replay_but_accepts_unique_rescan(make_rig):
    rig = make_rig()
    client = rig.prepared_client()
    assert rig.runtime._continue_ispy_player_turn(client, barge_in=False) is False
    before = list(rig.events)
    with pytest.raises(ISpyCancelled):
        rig.runtime._continue_ispy_player_turn(client, barge_in=False)
    client.chat("look again")  # malicious/stale replay of the same opaque token
    with pytest.raises(ISpyCancelled):
        rig.runtime._continue_ispy_player_turn(client, barge_in=False)
    assert rig.events == before
    rig.reply["ispy_view_token"] = "c" * 32
    client.chat("look again")
    assert rig.runtime._continue_ispy_player_turn(client, barge_in=False) is False
    assert [body["view_token"] for body in rig.observations] == [TOKEN, "c" * 32]
    assert rig.spoken == []  # continuation itself never speaks/listens
    assert rig.harness.calls == []


@pytest.mark.parametrize("field,value", [
    ("last_kids_next_action", ""), ("last_kids_ispy_phase", "awaiting_clue"),
    ("last_kids_ispy_role", "reachy_picker"), ("last_kids_ispy_view_token", ""),
    ("last_kids_ispy_view_token", "A" * 32),
])
def test_runtime_rejects_missing_or_stale_action_without_camera(make_rig, field, value):
    rig = make_rig()
    client = rig.prepared_client()
    setattr(client, field, value)
    with pytest.raises(ISpyCancelled):
        rig.runtime._continue_ispy_player_turn(client, barge_in=False)
    assert rig.events == [] and rig.stops == []


@pytest.mark.parametrize("signal", ["stop", "end", "privacy", "sleep", "app_stop"])
@pytest.mark.parametrize("boundary", ["capture", "provider"])
@pytest.mark.parametrize("late_error", [False, True])
def test_held_view_cancellation_has_no_late_effect_or_status_overwrite(make_rig, signal, boundary, late_error):
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(5)
        if late_error:
            raise HermesBridgeError("late boundary failure")
        return b"jpeg"

    rig = make_rig(frame=hold if boundary == "capture" else lambda: b"jpeg")
    if boundary == "provider":
        rig.on_observe = hold
    thread, raised = rig.run_in_thread()
    assert entered.wait(5)
    runtime = rig.runtime
    if signal == "stop":
        runtime._request_conversation_stop()
    elif signal == "end":
        runtime.stop_kids_mode(reason="parent", fold=False)
    elif signal == "privacy":
        runtime._privacy_requested.set()
    elif signal == "sleep":
        runtime._power_mode = "sleep"
    else:
        runtime.stop_event.set()
    before = list(rig.events)
    runtime._set_status("waiting_for_wake_word", "Control transition owns status")
    release.set()
    thread.join(5)
    assert not thread.is_alive() and raised == []
    assert rig.events == before
    assert runtime._status.detail == "Control transition owns status"
    assert runtime._status.state != "error" and rig.errors == []
    assert [s["reason"] for s in rig.stops] == (["parent"] if signal == "end" else [])
    assert rig.spoken == [rig.reply["text"]]
    assert runtime._kids_camera_active is False
    if boundary == "capture":
        assert before == ["tts", "camera"] and not rig.observations
    assert set(rig.cancelled_sessions) <= {rig.session_id}


@pytest.mark.parametrize("boundary", ["capture", "provider"])
def test_actual_current_view_failure_stops_kids_safely(make_rig, boundary):
    def broken():
        raise HermesBridgeError("real camera or provider fault")

    rig = make_rig(frame=broken if boundary == "capture" else lambda: b"jpeg")
    if boundary == "provider":
        rig.on_observe = broken
    with pytest.raises(HermesBridgeError, match="real camera or provider fault"):
        rig.run()
    assert [s["reason"] for s in rig.stops] == ["ispy_view_failed"]
    assert rig.runtime._kids_active is False
    assert rig.runtime._kids_camera_active is False
    assert len(rig.spoken) == 1


@pytest.mark.parametrize("signal", ["stop", "privacy", "sleep", "app_stop", "interrupted"])
def test_control_or_interruption_after_speech_prevents_view_scan(make_rig, signal):
    rig = make_rig()

    def interrupted_speech(*_args, **_kwargs):
        rig.events.append("tts")
        if signal == "stop":
            rig.runtime._request_conversation_stop()
        elif signal == "privacy":
            rig.runtime._privacy_requested.set()
        elif signal == "sleep":
            rig.runtime._power_mode = "sleep"
        elif signal == "app_stop":
            rig.runtime.stop_event.set()
        return signal == "interrupted"

    rig.runtime._play_kids_stream = interrupted_speech
    rig.run()
    assert rig.events == ["tts"] and not rig.observations


def test_initial_select_keeps_default_non_turn_bound_admission(monkeypatch, tmp_path):
    rig = Rig(monkeypatch, tmp_path)
    runtime = rig.runtime
    runtime._conversation_stop_requested.set()  # intentional earlier Stop at initial Start
    client = runtime._new_bridge_client(rig.config)
    target = runtime._prepare_ispy_round(runtime._kids_generation, runtime._kids_profile, client=client)
    assert target.clue("nl") == "clue"
    assert rig.events.count("camera") == 6 and rig.events[-1] == "provider"
    assert runtime._conversation_stop_requested.is_set()  # never reset cancellation to make it pass


def test_view_discards_readiness_and_clears_transient_frame_list(make_rig):
    images = [b"readiness", b"left", b"far-left", b"center", b"right", b"far-right"]
    captures = iter(images)
    rig = make_rig(frame=lambda: next(captures))
    client = rig.prepared_client()
    original = client.observe_ispy_scene
    frame_lists = []

    def observe_and_remember(frames, **kwargs):
        frame_lists.append(frames)
        return original(frames, **kwargs)

    client.observe_ispy_scene = observe_and_remember
    rig.runtime._continue_ispy_player_turn(client, barge_in=True)
    assert [base64.b64decode(f) for f in rig.observations[0]["frames_jpeg"]] == images[1:]
    assert frame_lists == [[]]  # no retained image pool on the runtime or client


@pytest.mark.parametrize("boundary", ["capture", "provider"])
def test_end_replacement_owns_held_view_result(make_rig, boundary):
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        assert release.wait(5)
        return b"jpeg"

    rig = make_rig(frame=hold if boundary == "capture" else lambda: b"jpeg")
    if boundary == "provider":
        rig.on_observe = hold
    thread, raised = rig.run_in_thread()
    assert entered.wait(5)
    replacement = rig.end_and_replace()
    rig.runtime._set_status("waiting_for_wake_word", "Replacement owns status")
    before = list(rig.events)
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    rig.assert_quiet_and_replacement_untouched(replacement, raised)
    assert rig.events == before
    assert rig.runtime._status.detail == "Replacement owns status"


def test_stop_during_motion_allows_no_later_capture_or_provider(make_rig):
    rig = make_rig()

    def stopped_motion(**_kwargs):
        rig.events.append("motion")
        rig.runtime._request_conversation_stop()
        rig.runtime._set_status("waiting_for_wake_word", "Stop owns status")

    rig.runtime.robot.goto_target = stopped_motion
    rig.run()
    assert rig.events == ["tts", "camera", "motion"]
    assert not rig.observations and not rig.stops
    assert rig.runtime._status.detail == "Stop owns status"


def test_stale_client_session_cannot_borrow_new_session_camera(make_rig):
    rig = make_rig()
    client = rig.prepared_client()
    client.config = replace(client.config, kids_session_id=SESSION)
    with pytest.raises(ISpyCancelled):
        rig.runtime._continue_ispy_player_turn(client, barge_in=False)
    assert rig.events == [] and not rig.stops
