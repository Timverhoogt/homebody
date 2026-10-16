"""Cross-layer typed gameplay: real bridge/client/Pipeline, synthetic media/model/audio only."""
from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from test_ispy_target_variety import FRAMES, SESSION, Request, candidate
from test_ispy_visual_bridge import VisualHarness
from test_ispy_visual_runtime import VisualRig

from homebody.hermes_client import HermesBridgeClient


@pytest.mark.parametrize("hosted", [False, True])
def test_typed_game_observes_before_guess_and_bounds_refresh(monkeypatch, tmp_path, hosted):
    h = VisualHarness(monkeypatch, language="nl", hosted=hosted)
    rig = VisualRig(monkeypatch, tmp_path, hosted=hosted, frame=lambda: base64.b64decode(FRAMES[0]))
    bridge = h.bridge
    bridge._kids_sessions[rig.session_id] = bridge._kids_sessions.pop(SESSION)
    bridge._mark_kids_live(rig.session_id)
    requests, spoken = [], []
    chosen = {"index": 0}
    pool = [candidate("bal"), candidate("lamp", frame_index=2)]

    def handle(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/v1/kids/chat":
            state = bridge._kids_sessions[rig.session_id]
            h.vision.reply = ({"match": True} if state["ispy_role"] == "reachy_picker"
                              else {"candidate_index": chosen["index"]})
            reply = asyncio.run(bridge.kids_chat(Request(body)))
        elif request.url.path == "/v1/kids/ispy/observe":
            h.vision.reply = {"candidates": pool}
            reply = asyncio.run(bridge.kids_ispy_observe(Request(body)))
        else:
            pytest.fail(f"Unexpected integrated endpoint {request.url.path}")
        return httpx.Response(reply.status, json=json.loads(reply.text))

    def client(config):
        value = HermesBridgeClient(config, client=httpx.Client(transport=httpx.MockTransport(handle)))
        value.health = lambda: {"status": "ok", "stt_locality": "local"}
        rig.clients.append(value)
        return value

    def speak(value, text, **_):
        bridge._consume_kids_speech_approval(value._kids_speech_approval, rig.session_id, text)
        spoken.append(text)
        return False

    rig.runtime._new_bridge_client = client
    rig.runtime._play_kids_stream = speak
    rig.runtime._record_utterance = lambda *_: pytest.fail("Typed interaction opened microphone")

    def say(text):
        token = rig.runtime.ispy_input_context()["token"]
        assert rig.runtime.submit_ispy_guess(text, token) == {"accepted": True}

    try:
        say("stoel")
        state = bridge._kids_sessions[rig.session_id]
        assert state["ispy_player_phase"] == "awaiting_clue"
        assert state["ispy_player_scans"] == 1
        assert rig.events.count("camera") == 6
        assert requests[-1][0] == "/v1/kids/ispy/observe"
        assert len(requests[-1][1]["frames_jpeg"]) == 5
        say("Het is rond")
        assert spoken[-1] == "Is het een bal?"
        say("nee")
        chosen["index"] = None
        say("Het heeft toetsen")
        assert state["ispy_player_scans"] == 2
        assert state["ispy_player_guesses"] == ["bal"]
        assert "Het is rond" in state["ispy_player_clues"]
        assert rig.events.count("camera") == 12
        say("Het is groot")
        assert spoken[-1] == h.module._kids_copy("nl", "ispy_player_no_fit")
        assert rig.events.count("camera") == 12
        assert sum(path == "/v1/kids/ispy/observe" for path, _ in requests) == 2
        say("Ik weet het niet")
        assert "ispy_player_candidates" not in state
        assert rig.events.count("camera") == 12
        assert rig.runtime._kids_camera_active is False
    finally:
        for timer in (rig.runtime._kids_timer, rig.runtime._kids_warning_timer):
            if timer:
                timer.cancel()
        for value in rig.clients:
            value._client.close()


@pytest.mark.parametrize("hosted", [False, True])
@pytest.mark.parametrize("boundary", ["speech", "readiness", "response"])
def test_interruption_then_new_user_turn_recovers_without_replay(monkeypatch, tmp_path, hosted, boundary):
    h = VisualHarness(monkeypatch, language="nl", hosted=hosted)
    rig = VisualRig(monkeypatch, tmp_path, hosted=hosted)
    bridge, runtime = h.bridge, rig.runtime
    bridge._kids_sessions[rig.session_id] = bridge._kids_sessions.pop(SESSION)
    bridge._mark_kids_live(rig.session_id)
    fired, requests = [], []
    pool = [candidate("bal")]

    def frame():
        if boundary == "readiness" and not fired:
            fired.append(True)
            runtime._request_conversation_stop()
        return base64.b64decode(FRAMES[0])

    # The original media fake delegates to this callback.
    runtime._capture_camera_jpeg = lambda **_: frame()

    def handle(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/v1/kids/chat":
            state = bridge._kids_sessions[rig.session_id]
            h.vision.reply = {"match": True} if state["ispy_role"] == "reachy_picker" else {"candidate_index": 0}
            reply = asyncio.run(bridge.kids_chat(Request(body)))
        elif request.url.path == "/v1/kids/ispy/observe":
            h.vision.reply = {"candidates": pool}
            reply = asyncio.run(bridge.kids_ispy_observe(Request(body)))
            if boundary == "response" and not fired:
                fired.append(True)
                runtime._request_conversation_stop()
        elif request.url.path == "/v1/kids/ispy/cancel":
            assert "view_token" in body  # never delete the still-live game
            reply = asyncio.run(bridge.kids_ispy_cancel(Request(body)))
        else:
            pytest.fail(f"Unexpected endpoint {request.url.path}")
        return httpx.Response(reply.status, json=json.loads(reply.text))

    def client(config):
        value = HermesBridgeClient(config, client=httpx.Client(transport=httpx.MockTransport(handle)))
        value.health = lambda: {"status": "ok"}
        rig.clients.append(value)
        return value

    def speak(value, text, **_):
        bridge._consume_kids_speech_approval(value._kids_speech_approval, rig.session_id, text)
        rig.spoken.append(text)
        if boundary == "speech" and not fired:
            fired.append(True)
            return True
        return False

    runtime._new_bridge_client, runtime._play_kids_stream = client, speak
    runtime._record_utterance = lambda *_: pytest.fail("Typed recovery opened microphone")

    def say(text):
        token = runtime.ispy_input_context()["token"]
        assert runtime.submit_ispy_guess(text, token) == {"accepted": True}

    try:
        if boundary == "speech":
            say("stoel")
        else:
            from homebody.kids_runtime import ISpyInputUnavailable
            with pytest.raises(ISpyInputUnavailable, match="turn ended"):
                say("stoel")
        assert fired and runtime._kids_active and not rig.stops
        state = bridge._kids_sessions[rig.session_id]
        assert state["ispy_role"] == "player_picker"
        initial_token = (state.get("ispy_view_token") or next(
            body["view_token"] for path, body in requests if path.endswith("/cancel")
        ))
        assert state["ispy_player_scans"] == (0 if boundary == "speech" else 1)
        assert state["ispy_player_phase"] == ("awaiting_view" if boundary == "speech" else "awaiting_clue")
        if boundary != "speech":
            assert state["ispy_player_candidates"] == []
        say("Het is rond")  # explicit new input / wake, not automatic resumption
        assert state["ispy_player_phase"] == "awaiting_clue"
        observations = [body for path, body in requests if path.endswith("/observe")]
        if boundary == "speech":
            assert observations[-1]["view_token"] == initial_token
        else:
            assert observations[-1]["view_token"] != initial_token
        assert state["ispy_player_scans"] == (1 if boundary == "speech" else 2)
        say("Het is rood")
        assert rig.spoken[-1] == "Is het een bal?"
        assert state["ispy_player_phase"] == "awaiting_confirmation"
        assert not rig.stops and runtime._kids_camera_active is False
    finally:
        for timer in (runtime._kids_timer, runtime._kids_warning_timer):
            if timer:
                timer.cancel()
        for value in rig.clients:
            value._client.close()
