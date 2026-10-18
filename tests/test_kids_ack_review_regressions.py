"""Inverse acceptance for the independent review of cached Kids acknowledgements.

Synthetic fixtures only: held SDK pushes, mocked bridge transport, no hardware or network.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from test_kids_interaction_fixes import load_bridge_module
from test_kids_stt_acknowledgements import VOICE_PROOF, Harness, cancel, wait_for

from homebody.hermes_client import HermesBridgeClient, HermesBridgeError
from homebody.kids_acknowledgements import SETTLE_JOIN_SECONDS

BOUNDARIES = ["stop", "end", "privacy", "new_start", "timeout"]


def invalidate(harness: Harness, boundary: str) -> None:
    if boundary == "timeout":
        with harness.runtime._kids_lock:
            harness.runtime._kids_ends_at = time.monotonic() - 1
    else:
        cancel(harness.runtime, boundary)


def held_player(monkeypatch, tmp_path, *, hold_push: int, clip_seconds: float = 0.3):
    harness = Harness(monkeypatch, tmp_path, clip_seconds=clip_seconds)
    harness.runtime._accept_wake_turn()
    events: list[tuple[str, float]] = []
    held, release = threading.Event(), threading.Event()
    count = 0

    def push(_samples) -> None:
        nonlocal count
        count += 1
        events.append(("push_enter", time.monotonic()))
        if count == hold_push:
            held.set()
            release.wait(5)  # an SDK call that stays blocked past cancellation and the deadline
        events.append(("push_return", time.monotonic()))

    harness.media.push_audio_sample = push
    harness.runtime._clear_streamed_audio = lambda: events.append(("clear", time.monotonic()))
    player = harness.runtime._start_kids_ack(harness.config, harness.proof)
    assert player is not None
    return harness, player, events, held, release


@pytest.mark.parametrize("hold_push", [1, 3])  # single first chunk, and the final chunk
@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_blocked_push_settle_is_bounded_quarantines_then_flushes_late_chunk(
    monkeypatch,
    tmp_path,
    boundary,
    hold_push,
) -> None:
    harness, player, events, held, release = held_player(monkeypatch, tmp_path, hold_push=hold_push)
    runtime = harness.runtime
    try:
        assert held.wait(2)
        invalidate(harness, boundary)
        time.sleep(0.4)  # the estimated audible deadline elapses while the push is still blocked
        started = time.monotonic()
        quiet = player.settle()  # cleanup starts while the IO is STILL held
        assert time.monotonic() - started < SETTLE_JOIN_SECONDS + 0.4  # bounded, no unbounded lock
        assert quiet is False
        # Fail closed while quarantined: no new filler, no stream, no microphone turn.
        assert runtime._ack_output_quarantined()
        assert runtime._start_kids_ack(harness.config, harness.proof) is None
        assert runtime._play_kids_stream(SimpleNamespace(config=harness.config), "late") is False
        runtime._accept_wake_turn()
        records_before = [label for label, _ in harness.calls].count("record")
        runtime._run_conversation(harness.config)
        assert [label for label, _ in harness.calls].count("record") == records_before
    finally:
        release.set()
    player._thread.join(2)
    assert not player._thread.is_alive()
    late_return = [at for kind, at in events if kind == "push_return"][-1]
    assert any(kind == "clear" and at >= late_return for kind, at in events)  # late admission flushed
    assert [kind for kind, _ in events].count("push_enter") == hold_push  # no chunk after cancel
    assert not runtime._ack_output_quarantined()


@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_healthy_cancel_is_fast_and_never_decides_from_estimated_time(monkeypatch, tmp_path, boundary) -> None:
    harness = Harness(monkeypatch, tmp_path, clip_seconds=0.3)
    harness.runtime._accept_wake_turn()
    events: list[tuple[str, float]] = []
    harness.media.push_audio_sample = lambda _s: events.append(("push", time.monotonic()))
    harness.runtime._clear_streamed_audio = lambda: events.append(("clear", time.monotonic()))
    player = harness.runtime._start_kids_ack(harness.config, harness.proof)
    assert wait_for(lambda: any(kind == "push" for kind, _ in events), 2)
    time.sleep(0.5)  # well past the estimated end of the clip
    invalidate(harness, boundary)
    started = time.monotonic()
    assert player.settle() is True
    assert time.monotonic() - started < 0.3
    last_push = [at for kind, at in events if kind == "push"][-1]
    assert any(kind == "clear" and at >= last_push for kind, at in events)  # admitted => flushed


# --- Effective voice proof ----------------------------------------------------------------------


def bare_bridge(monkeypatch, tmp_path, voice: dict[str, str]):
    module = load_bridge_module()
    bridge = module.Bridge.__new__(module.Bridge)
    bridge.profile = None
    # This fixture models the legacy agent-backed bridge, not household-only mode.
    bridge.backends = {"hermes": SimpleNamespace()}
    bridge.hermes_enabled = True
    bridge.hermes_url = "http://synthetic.invalid"
    bridge.http = None
    bridge.openclaw = None
    bridge.llm = SimpleNamespace(public_status=lambda: {})
    bridge._llm_key = lambda: "synthetic-key"
    bridge.warm_agents = SimpleNamespace(settings=SimpleNamespace(enabled=False), status=lambda: {"available": False})
    home = tmp_path / "synthetic-host"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text("stt:\n  provider: local_command\n")
    monkeypatch.setattr(module, "_hermes_home", lambda _profile: home)
    monkeypatch.setattr(
        module,
        "_resolve_secret",
        lambda key, _profile: (
            voice.get("id") if key == "ELEVENLABS_KIDS_VOICE_ID"
            else "" if key == "ELEVENLABS_KIDS_NL_VOICE_ID" else "synthetic-key"
        ),
    )
    return module, bridge


def stream_url(module, bridge) -> str:
    urls: list[str] = []

    class Seen(Exception):
        pass

    class HTTP:
        def post(self, url, **_kwargs):
            urls.append(url)
            raise Seen()

    bridge.http = HTTP()
    bridge.require_auth = lambda _request: None
    bridge._consume_kids_speech_approval = lambda *_a: None  # approval gate tested elsewhere; unchanged

    class Request:
        async def json(self):
            return {"input": "fixed synthetic text", "session_id": "kids-" + "a" * 32, "speech_approval": "x"}

    with pytest.raises(Seen):
        asyncio.run(bridge.kids_speech_stream(Request()))
    bridge.http = None
    return urls[0]


def test_host_voice_override_changes_health_proof_and_disables_filler(monkeypatch, tmp_path) -> None:
    voice = {"id": "cgSgspJ2msm6clMCkdW9"}
    module, bridge = bare_bridge(monkeypatch, tmp_path, voice)
    before = json.loads(asyncio.run(bridge.health(None)).text)
    assert before["kids_voice_sha256"] == VOICE_PROOF["kids_voice_sha256"]
    assert before["kids_tts_model"] == "eleven_flash_v2_5"
    assert "cgSgspJ2msm6clMCkdW9" not in json.dumps(before)  # digest only, never the raw id
    voice["id"] = "synthetic_changed_Kids_voice"
    after = json.loads(asyncio.run(bridge.health(None)).text)
    assert after["kids_voice_sha256"] == hashlib.sha256(b"synthetic_changed_Kids_voice").hexdigest()
    assert "synthetic_changed_Kids_voice" in stream_url(module, bridge)  # same resolver as streaming
    assert "synthetic_changed_Kids_voice" not in json.dumps(after)

    for proof, expected in [
        (before, True),  # the clips' voice is the effective voice
        (after, False),  # override after packaging: mismatch
        ({}, False),  # old bridge without proof
        ({
            **before, "kids_tts_model": "eleven_multilingual_v2",
            "kids_voice_proofs": {
                language: {**entry, "kids_tts_model": "eleven_multilingual_v2"}
                for language, entry in before["kids_voice_proofs"].items()
            },
        }, False),
    ]:
        harness = Harness(
            monkeypatch, tmp_path / f"h{len(proof)}{expected}{proof.get('kids_tts_model', '')}", stt_seconds=0.5
        )
        harness.health_proof = {k: v for k, v in proof.items() if k.startswith("kids_")}
        harness.run()  # real pipeline: proof comes from this conversation's health
        assert bool(harness.media.pushes()) is expected


def test_invalid_voice_override_yields_no_proof(monkeypatch, tmp_path) -> None:
    _module, bridge = bare_bridge(monkeypatch, tmp_path, {"id": "bad voice/../id"})
    health = json.loads(asyncio.run(bridge.health(None)).text)
    assert "kids_voice_sha256" not in health and "kids_tts_model" not in health


# --- Per-turn STT provenance through the real client --------------------------------------------


@pytest.mark.parametrize(
    ("first_receipt", "second_pending"),
    [("groq", "cloud"), ("local_command", "local"), ("custom-thing", "unknown"), (None, "unknown")],
)
def test_actual_transcription_receipt_drives_next_pending_locality(
    monkeypatch,
    tmp_path,
    first_receipt,
    second_pending,
) -> None:
    harness = Harness(monkeypatch, tmp_path, catalog=False, transcripts=(), speech=[True, True, False, False, False])
    runtime = harness.runtime
    seen: list[tuple[str, dict]] = []
    replies = iter(["synthetic one", "synthetic two"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok", "stt_locality": "local"})
        if request.url.path.endswith("/v1/audio/transcriptions"):
            seen.append((runtime._status.stt_locality, runtime.status()))  # what the UI shows while pending
            body = {"text": next(replies)}
            if first_receipt and len(seen) == 1:
                body["provider"] = first_receipt
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    def factory(config):
        client = HermesBridgeClient(config)
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
        client.chat = lambda _text: "Goed zo!"
        return client

    runtime._new_bridge_client = factory  # type: ignore[method-assign]
    harness.run()
    assert [locality for locality, _ in seen] == ["local", second_pending]
    for _locality, status in seen:
        assert set(status) >= {"stt_locality"} and "stt_provider" not in status  # child lock: enum only
        assert status["stt_locality"] in {"local", "cloud", "unknown"}
    assert runtime._status.stt_locality == "unknown"  # cleared when the conversation ends


def test_transcription_error_clears_local_claim(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path, catalog=False)
    runtime = harness.runtime

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "ok", "stt_locality": "local"})
        return httpx.Response(502, text="private upstream detail")

    def factory(config):
        client = HermesBridgeClient(config)
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
        return client

    runtime._new_bridge_client = factory  # type: ignore[method-assign]
    with pytest.raises(HermesBridgeError):
        harness.run()
    assert runtime.status()["stt_locality"] == "unknown"
    assert "private upstream detail" not in json.dumps(runtime.status())


# --- Common quarantine gate across engines after Kids End ---------------------------------------


class _AdultSession:
    def __init__(self, admits: list) -> None:
        self.admits = admits

    def start(self):
        self.admits.append("session_start")

    def wait_started(self):
        return [SimpleNamespace(type="session.closed", payload={})]

    def send_audio(self, _frame):
        self.admits.append("microphone_send")

    def events(self):
        return []

    def close(self):
        pass


def adult_engine(harness, tmp_path, admits):
    from pathlib import Path

    import numpy as np

    from homebody.runtime import HermesVoiceRuntime

    runtime = harness.runtime
    runtime._new_live_session = lambda *a, **k: _AdultSession(admits)  # type: ignore[method-assign]
    runtime._new_realtime_session = lambda *a, **k: _AdultSession(admits)  # type: ignore[method-assign]
    runtime.assets = tmp_path / "earcons"
    runtime.assets.mkdir(exist_ok=True)
    (runtime.assets / "listening.wav").write_bytes(b"synthetic-audio")
    runtime._play_asset = HermesVoiceRuntime._play_asset.__get__(runtime)  # type: ignore[method-assign]
    harness.media.play_sound = lambda path: admits.append(("earcon", Path(path).name))
    runtime._read_16k_frame = lambda: np.ones(160, dtype=np.float32)  # type: ignore[method-assign]


@pytest.mark.parametrize("entry", ["dispatcher_live", "dispatcher_realtime", "direct_live", "direct_realtime"])
def test_held_kids_push_blocks_adult_wake_on_every_engine_then_healthy(monkeypatch, tmp_path, entry) -> None:
    from homebody.config import AppConfig

    harness, player, events, held, release = held_player(monkeypatch, tmp_path, hold_push=3)
    runtime = harness.runtime
    admits: list = []
    adult_engine(harness, tmp_path, admits)
    mode = "live" if entry.endswith("live") else "realtime"
    config = AppConfig(api_key="synthetic", conversation_mode=mode)

    def attempt() -> None:
        runtime._accept_wake_turn()
        if entry.startswith("dispatcher"):
            runtime._run_selected_voice_conversation(config, "button")
        elif mode == "live":
            runtime._run_live_conversation(config)
        else:
            runtime._run_realtime_conversation(config)

    try:
        assert held.wait(2)
        started = time.monotonic()
        runtime.stop_kids_mode(fold=False)  # End returns promptly while the SDK push is held
        assert time.monotonic() - started < 1.0
        assert player.settle() is False and runtime._ack_output_quarantined()
        stop_generation = runtime._conversation_stop_generation
        attempt()  # adult wake while the old push is STILL held
        assert admits == []  # no earcon, no session start, no microphone
        assert runtime._conversation_stop_generation == stop_generation  # gate never resamples stop state
    finally:
        release.set()
    player._thread.join(2)
    late_return = [at for kind, at in events if kind == "push_return"][-1]
    assert any(kind == "clear" and at >= late_return for kind, at in events)
    assert not runtime._ack_output_quarantined()
    # Healthy afterwards: a legitimate adult wake works (Live engine; its fake session closes cleanly).
    runtime._accept_wake_turn()
    runtime._run_selected_voice_conversation(AppConfig(api_key="synthetic", conversation_mode="live"), "button")
    assert ("earcon", "listening.wav") in admits and "session_start" in admits


# --- Per-turn voice proof inside one conversation ------------------------------------------------


def bridge_transport(module, bridge, *, proof_route=True, stt_seconds=0.5):
    """Real client -> mocked transport -> the real bridge health/proof handlers."""
    bridge.require_auth = lambda _request: None

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/health"):
            # Real health handler (its voice proof and STT locality); the synthetic bridge has no
            # Hermes agent, so mark only the agent as up for the client's readiness check.
            payload = json.loads(asyncio.run(bridge.health(None)).text)
            payload.update(status="ok", agent_api=True, hermes_api=True)
            return httpx.Response(200, json=payload)
        if path.endswith("/v1/kids/voice-proof"):
            if not proof_route:
                return httpx.Response(404, text="old bridge")
            return httpx.Response(200, text=asyncio.run(
                bridge.kids_voice_proof(SimpleNamespace(query=request.url.params))
            ).text)
        if path.endswith("/v1/audio/transcriptions"):
            time.sleep(stt_seconds)  # slow STT: the filler delay elapses
            return httpx.Response(200, json={"text": "synthetic", "provider": "local_command"})
        return httpx.Response(404)

    return handler


@pytest.mark.parametrize("scenario", ["override_then_return", "old_bridge_without_refresh"])
def test_voice_proof_is_refreshed_per_turn_within_one_conversation(monkeypatch, tmp_path, scenario) -> None:
    from homebody import kids_acknowledgements as acks

    monkeypatch.setattr(acks, "ACK_COOLDOWN_SECONDS", 0.3)  # cooldown honoured, but kept short
    voice = {"id": "cgSgspJ2msm6clMCkdW9"}
    module, bridge = bare_bridge(monkeypatch, tmp_path, voice)
    harness = Harness(monkeypatch, tmp_path / "h", speech=[True, True, True, False, False, False])
    runtime = harness.runtime
    handler = bridge_transport(module, bridge, proof_route=scenario == "override_then_return")
    per_turn: list[int] = []
    urls: list[str] = []
    voices = ["synthetic_changed_Kids_voice", "cgSgspJ2msm6clMCkdW9", "cgSgspJ2msm6clMCkdW9"]

    def chat(_text: str) -> str:
        per_turn.append(len(harness.media.pushes()) - sum(per_turn))
        voice["id"] = voices[len(per_turn) - 1]  # host setting changes between turns
        urls.append(stream_url(module, bridge))  # the actual stream endpoint now uses this voice
        time.sleep(0.35)  # past the (shortened) cooldown
        return "Goed zo!"

    def factory(config):
        client = HermesBridgeClient(config)
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
        client.chat = chat
        return client

    runtime._new_bridge_client = factory  # type: ignore[method-assign]
    harness.run()
    assert per_turn[0] > 0  # turn 1: matching proof from this conversation's health => clip played
    assert "synthetic_changed_Kids_voice" in urls[0]
    if scenario == "override_then_return":
        assert per_turn[1] == 0  # turn 2: fresh proof shows the override => no old-voice filler
        assert per_turn[2] > 0  # turn 3: back to the matching voice => healthy again
    else:
        assert per_turn[1:] == [0, 0]  # refresh unavailable (old bridge) => fail closed
    assert "kids_voice_sha256" not in json.dumps(runtime.status())
    assert "cgSgspJ2msm6clMCkdW9" not in json.dumps(runtime.status())
