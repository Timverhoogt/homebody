"""Cached Kids acknowledgements during pending STT, and categorical STT locality.

Synthetic PCM fixtures only; no real voice, child speech, network or hardware.
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from test_kids_interaction_fixes import load_bridge_module, profile_for

from homebody import kids_acknowledgements as acks
from homebody import voice_pipeline
from homebody.audio import EndpointResult
from homebody.config import AppConfig
from homebody.kids_mode import KIDS_TTS_MODEL, KIDS_VOICE_ID
from homebody.runtime import HermesVoiceRuntime

PHRASES = {"en": ["Let me think…", "Hmm…"], "nl": ["Even denken…", "Eens kijken…"]}
# The synthetic catalog is "generated" with the default Kids voice; health proves the same voice.
VOICE_PROOF = {
    "kids_voice_sha256": hashlib.sha256(KIDS_VOICE_ID.encode()).hexdigest(),
    "kids_tts_model": KIDS_TTS_MODEL,
}


def synthetic_wav(seconds: float = 0.6, rate: int = 16000, channels: int = 1) -> bytes:
    tone = (np.sin(np.linspace(0, 200 * np.pi, int(seconds * rate))) * 2000).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(np.repeat(tone, channels).tobytes())
    return buffer.getvalue()


def write_catalog(directory: Path, *, seconds: float = 0.6, **overrides) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    clips = []
    for language, phrases in PHRASES.items():
        for index, text in enumerate(phrases):
            name = f"{language}-{index}.wav"
            data = synthetic_wav(seconds)
            (directory / name).write_bytes(data)
            clips.append(
                {
                    "id": name[:-4],
                    "language": language,
                    "text": text,
                    "file": name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "duration_seconds": seconds,
                }
            )
    manifest = {
        "schema_version": 1,
        "voice_sha256": hashlib.sha256(KIDS_VOICE_ID.encode()).hexdigest(),
        "model": KIDS_TTS_MODEL,
        "sample_rate": 16000,
        "clips": clips,
        **overrides,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


# --- Static catalog verification ---------------------------------------------------------------


def test_valid_catalog_loads_language_matched_clips(tmp_path: Path) -> None:
    catalog = acks.load_catalog(write_catalog(tmp_path / "c"))
    assert [clip.name for clip in catalog["en"]] == ["en-0.wav", "en-1.wav"]
    assert [clip.name for clip in catalog["nl"]] == ["nl-0.wav", "nl-1.wav"]
    assert all(0.5 < clip.duration < 0.7 for clip in catalog["nl"])


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "voice",
        "model",
        "unearned_phrase",
        "digest",
        "symlink",
        "traversal",
        "stereo",
        "too_long",
        "extra_key",
    ],
)
def test_malformed_or_unmatched_catalog_yields_no_filler(tmp_path: Path, problem: str) -> None:
    directory = tmp_path / "c"
    if problem == "missing":
        assert acks.load_catalog(directory) == {"en": (), "nl": ()}
        return
    write_catalog(directory, seconds=3.0 if problem == "too_long" else 0.6)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    first = manifest["clips"][0]
    if problem == "voice":
        manifest["voice_sha256"] = "cgSgspJ2msm6clMCkdW9"  # a raw id, not a digest
    elif problem == "model":
        manifest["model"] = "eleven_multilingual_v2"
    elif problem == "unearned_phrase":
        first["text"] = "Good guess!"
    elif problem == "digest":
        first["sha256"] = "0" * 64
    elif problem == "symlink":
        (directory / "real.wav").write_bytes((directory / first["file"]).read_bytes())
        (directory / first["file"]).unlink()
        (directory / first["file"]).symlink_to(directory / "real.wav")
    elif problem == "traversal":
        first["file"] = "../en-0.wav"
    elif problem == "stereo":
        data = synthetic_wav(channels=2)
        (directory / first["file"]).write_bytes(data)
        first["sha256"] = hashlib.sha256(data).hexdigest()
    elif problem == "extra_key":
        first["voice"] = "override"
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert acks.load_catalog(directory) == {"en": (), "nl": ()}


@pytest.mark.parametrize("problem", ["duration", "rate", "id"])
def test_metadata_must_match_audio(tmp_path: Path, problem: str) -> None:
    directory = write_catalog(tmp_path / "c")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if problem == "duration":
        manifest["clips"][0]["duration_seconds"] = 1.4
    elif problem == "rate":
        manifest["sample_rate"] = 24000
    else:
        manifest["clips"][0]["id"] = "nl-0"  # an English file posing as Dutch
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert acks.load_catalog(directory) == {"en": (), "nl": ()}


def test_packaged_parent_catalog_verifies_against_the_kids_voice() -> None:
    """The parent-generated assets: report precisely, never fabricate readiness."""
    if not (acks.ASSET_DIR / "manifest.json").exists():
        pytest.skip("parent-generated acknowledgement assets are not present in this tree")
    catalog = acks.load_catalog()
    print(
        "PACKAGED_ACKS",
        {lang: [(c.name, round(c.duration, 3), c.sample_rate) for c in clips] for lang, clips in catalog.items()},
    )
    assert len(catalog["en"]) == 3 and len(catalog["nl"]) == 3
    assert all(clip.name.startswith(f"{lang}-") for lang, clips in catalog.items() for clip in clips)


def test_fixed_phrases_make_no_correctness_or_heard_claims() -> None:
    for phrases in acks.ALLOWED_PHRASES.values():
        for phrase in phrases:
            lowered = phrase.casefold()
            for claim in ("good", "right", "correct", "understood", "heard", "yes", "goed", "ja", "begrepen"):
                assert claim not in lowered


# --- Real pipeline with held callbacks ---------------------------------------------------------


class Media:
    def __init__(self) -> None:
        self.events: list[tuple[str, float, int]] = []
        self.audio = SimpleNamespace(clear_player=lambda: self.events.append(("clear", time.monotonic(), 0)))
        self.on_push = None

    def push_audio_sample(self, samples: np.ndarray) -> None:
        self.events.append(("push", time.monotonic(), int(samples.size)))
        if self.on_push is not None:
            self.on_push()

    def play_sound(self, _path: str) -> None:
        pass

    def pushes(self) -> list[tuple[str, float, int]]:
        return [event for event in self.events if event[0] == "push"]


class Harness:
    def __init__(
        self,
        monkeypatch,
        tmp_path,
        *,
        activity="quiz",
        language="nl",
        stt_seconds=0.5,
        transcripts=("een stoel",),
        catalog=True,
        clip_seconds=0.6,
        speech=None,
    ) -> None:
        self.media = Media()
        robot = SimpleNamespace(media=self.media, cancel_move=lambda: None)
        runtime = HermesVoiceRuntime(robot, threading.Event())
        self.runtime = runtime
        runtime._audio_ready = True
        runtime._power_mode = "awake"
        runtime._play_asset = lambda _name: None  # type: ignore[method-assign]
        runtime._discard_audio = lambda _seconds: None  # type: ignore[method-assign]
        runtime._notify_bridge_kids_session_async = lambda *_a, **_k: None  # type: ignore[method-assign]
        runtime._publish_remote_agent_session = lambda: None  # type: ignore[method-assign]
        loaded = (
            acks.load_catalog(write_catalog(tmp_path / "c", seconds=clip_seconds)) if catalog else {"en": (), "nl": ()}
        )
        monkeypatch.setattr(voice_pipeline, "kids_ack_catalog", lambda: loaded)
        monkeypatch.setattr(acks, "ACK_DELAY_SECONDS", 0.2)
        runtime.start_kids_mode(profile_for(activity, language), greet=False)
        self.config = runtime._kids_voice_config(AppConfig(api_key="k"))
        self.calls: list[tuple[str, float]] = []
        self.transcripts = list(transcripts)
        self.speech = list(speech if speech is not None else [True])
        self.stt_release = threading.Event()
        self.stt_seconds = stt_seconds
        self.t0 = 0.0
        self.health_proof = dict(VOICE_PROOF)
        self.proof = (VOICE_PROOF["kids_voice_sha256"], VOICE_PROOF["kids_tts_model"])
        harness = self

        class Client:
            last_stt_provider = "local"
            last_kids_ispy_role = ""
            last_kids_ispy_phase = ""
            last_kids_next_action = ""

            def __init__(self, cfg):
                self.config = cfg

            def health(self):
                return {"status": "ok", "stt_locality": "local", **harness.health_proof}

            def transcribe(self, _audio):
                harness.calls.append(("stt_start", time.monotonic()))
                harness.stt_release.wait(harness.stt_seconds)
                harness.calls.append(("stt_end", time.monotonic()))
                return harness.transcripts.pop(0) if harness.transcripts else ""

            def chat(self, text):
                harness.calls.append(("chat", time.monotonic()))
                return "Goed zo!"

            def close(self):
                pass

        runtime._new_bridge_client = Client  # type: ignore[method-assign]

        def record(_config):
            harness.calls.append(("record", time.monotonic()))
            speech = harness.speech.pop(0) if harness.speech else False
            samples = np.ones(160, dtype=np.float32) if speech else np.empty(0, dtype=np.float32)
            return EndpointResult(samples, speech, "end_silence" if speech else "initial_timeout", 0.01)

        runtime._record_utterance = record  # type: ignore[method-assign]

        def answer(*_args, **_kwargs):
            harness.calls.append(("answer", time.monotonic()))
            return False

        runtime._play_kids_stream = answer  # type: ignore[method-assign]

    def run(self) -> None:
        self.runtime._accept_wake_turn()
        self.t0 = time.monotonic()
        self.runtime._run_conversation(self.config)

    def at(self, name: str) -> float:
        return next(when for label, when in self.calls if label == name) - self.t0


def ack_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "kids-stt-ack" and thread.is_alive()]


def test_fast_stt_plays_no_filler(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=0.02)
    harness.run()
    assert harness.media.pushes() == []
    assert harness.at("answer") < 0.5
    assert ack_threads() == []


def test_slow_stt_plays_one_language_matched_clip_without_delaying_the_answer(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=0.5, clip_seconds=1.5)
    started = time.monotonic()
    harness.run()
    pushes = harness.media.pushes()
    assert pushes, "slow STT should trigger exactly one cached clip"
    first_enqueue = pushes[0][1] - started
    assert 0.15 < first_enqueue < 0.45  # after the delay, while STT is still pending
    assert harness.at("answer") < 0.9  # not 0.2 + 1.5 s clip: the answer never waits for the filler
    clears = [when for kind, when, _ in harness.media.events if kind == "clear"]
    answer_at = harness.at("answer") + harness.t0
    assert any(pushes[-1][1] <= when <= answer_at for when in clears)  # flushed before answer speech
    assert harness.runtime._kids_ack_selector._last_name.startswith("nl-")  # profile language
    assert ack_threads() == []
    # Synthetic latency evidence (enqueue, not physical audibility).
    print(f"ACK_LATENCY first_enqueue={first_enqueue:.3f}s answer={harness.at('answer'):.3f}s")


@pytest.mark.parametrize("case", ["calm", "adult", "missing_assets"])
def test_ineligible_turns_get_no_filler(monkeypatch, tmp_path, case) -> None:
    harness = Harness(
        monkeypatch,
        tmp_path,
        activity="calm" if case == "calm" else "quiz",
        catalog=case != "missing_assets",
        stt_seconds=0.5,
    )
    if case == "adult":
        assert harness.runtime._start_kids_ack(AppConfig(api_key="k")) is None
        return
    harness.run()
    assert harness.media.pushes() == []
    assert harness.at("answer") < 0.9


def test_typed_ispy_guess_never_uses_acknowledgement_path(monkeypatch, tmp_path) -> None:
    calls: list[object] = []
    monkeypatch.setattr(HermesVoiceRuntime, "_start_kids_ack", lambda self, config: calls.append(config))
    monkeypatch.setattr(HermesVoiceRuntime, "_prepare_ispy_round", lambda self, *_a, **_k: SimpleNamespace())
    harness = Harness(monkeypatch, tmp_path, activity="ispy")
    harness.runtime._invalidate_ispy_input = lambda: None  # type: ignore[method-assign]
    harness.runtime._ispy_typed_session_is_current = lambda _sid: True  # type: ignore[method-assign]
    harness.runtime._accept_wake_turn()
    harness.runtime._run_conversation(harness.config, typed_input="een stoel")
    assert calls == []
    assert harness.media.pushes() == []


@pytest.mark.parametrize("boundary", ["stop", "end", "privacy", "new_start", "standby"])
def test_cancel_before_delay_prevents_filler_and_discards_late_stt(monkeypatch, tmp_path, boundary) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=5)
    runtime = harness.runtime

    def cancel_soon() -> None:
        assert wait_for(lambda: any(label == "stt_start" for label, _ in harness.calls))
        time.sleep(0.05)  # before the 0.2 s filler delay
        cancel(runtime, boundary)
        harness.stt_release.set()  # the old STT then completes late

    threading.Thread(target=cancel_soon, daemon=True).start()
    harness.run()
    assert harness.media.pushes() == []
    assert not any(label in {"chat", "answer"} for label, _ in harness.calls)  # late STT result discarded
    assert ack_threads() == []


@pytest.mark.parametrize("boundary", ["stop", "end", "privacy", "new_start"])
def test_cancel_between_pcm_chunks_stops_producer_and_flushes_queue(monkeypatch, tmp_path, boundary) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=5, clip_seconds=1.5)
    runtime = harness.runtime

    def on_push() -> None:
        if len(harness.media.pushes()) == 2:  # producer accepted; cancel between samples
            cancel(runtime, boundary)

    harness.media.on_push = on_push
    released = threading.Thread(target=lambda: (time.sleep(0.6), harness.stt_release.set()), daemon=True)
    released.start()
    harness.run()
    pushes = harness.media.pushes()
    assert len(pushes) == 2  # no further chunk after cancellation
    assert any(kind == "clear" and when >= pushes[-1][1] for kind, when, _ in harness.media.events)
    assert not any(label in {"chat", "answer"} for label, _ in harness.calls)
    assert ack_threads() == []


def test_cancel_while_queued_audio_is_audible_flushes_promptly(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=5, clip_seconds=2.0)
    runtime = harness.runtime

    def stop_later() -> None:
        assert wait_for(lambda: bool(harness.media.pushes()), 2)
        time.sleep(0.2)
        runtime.stop_speaking()

    threading.Thread(target=stop_later, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(1.2), harness.stt_release.set()), daemon=True).start()
    harness.run()
    pushes = harness.media.pushes()
    clears = [when for kind, when, _ in harness.media.events if kind == "clear"]
    assert clears and clears[0] - pushes[-1][1] < 0.5  # watcher flushed long before the 2 s clip ends
    assert ack_threads() == []


def test_hung_stt_is_abandoned_on_stop_and_ack_thread_is_joined(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=30)
    runtime = harness.runtime
    threading.Thread(
        target=lambda: (time.sleep(0.4), runtime.stop_speaking(), time.sleep(0.2), harness.stt_release.set()),
        daemon=True,
    ).start()
    started = time.monotonic()
    harness.run()
    assert time.monotonic() - started < 3
    assert not any(label == "chat" for label, _ in harness.calls)
    assert ack_threads() == []


def test_empty_stt_retry_flushes_filler_before_reopening_microphone(monkeypatch, tmp_path) -> None:
    harness = Harness(
        monkeypatch, tmp_path, stt_seconds=0.5, clip_seconds=1.5, transcripts=("",), speech=[True, False, False]
    )
    harness.run()
    pushes = harness.media.pushes()
    assert pushes
    records = [when for label, when in harness.calls if label == "record"]
    clears = [when for kind, when, _ in harness.media.events if kind == "clear"]
    assert len(records) >= 2
    assert any(pushes[-1][1] <= when <= records[1] for when in clears)  # never records its own filler


def test_cooldown_allows_at_most_one_clip_and_varies_later(monkeypatch, tmp_path) -> None:
    harness = Harness(
        monkeypatch, tmp_path, stt_seconds=0.5, transcripts=("een", "twee"), speech=[True, True, False, False, False]
    )
    harness.run()
    names = {harness.runtime._kids_ack_selector._last_name}
    assert sum(1 for kind, *_ in harness.media.events if kind == "push") > 0
    first_turn_pushes = len(harness.media.pushes())
    selector = harness.runtime._kids_ack_selector
    clips = voice_pipeline.kids_ack_catalog()["nl"]
    assert selector.choose(clips, time.monotonic()) is None  # inside cooldown: no second clip
    later = selector.choose(clips, time.monotonic() + acks.ACK_COOLDOWN_SECONDS + 1)
    assert later is not None and later.name not in names  # never the same clip twice in a row
    assert first_turn_pushes == len(harness.media.pushes())


def test_ack_never_admits_microphone_or_motors(monkeypatch, tmp_path) -> None:
    harness = Harness(monkeypatch, tmp_path, stt_seconds=0.5)
    motor: list[object] = []
    harness.runtime._set_motor_mode = lambda *a, **k: motor.append(a)  # type: ignore[method-assign]
    harness.run()
    assert [label for label, _ in harness.calls].count("record") == 4  # 1 turn + 3 bounded silent listens
    assert motor == []


def cancel(runtime: HermesVoiceRuntime, boundary: str) -> None:
    if boundary == "stop":
        runtime.stop_speaking()
    elif boundary == "end":
        runtime.stop_kids_mode(fold=False)
    elif boundary == "privacy":
        runtime._privacy_requested.set()
    elif boundary == "new_start":
        runtime.start_kids_mode(profile_for("story", "en"), greet=False)
    elif boundary == "standby":
        runtime._request_conversation_stop()


def wait_for(predicate, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# --- STT locality metadata ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("local", "local"),
        ("local_command", "local"),
        ("openai", "cloud"),
        ("groq", "cloud"),
        ("", "unknown"),
        ("something-new", "unknown"),
    ],
)
def test_bridge_derives_only_categorical_stt_locality(provider, expected) -> None:
    bridge = load_bridge_module()
    assert bridge._stt_locality(provider, True) == expected
    assert bridge._stt_locality(provider, False) == "unknown"


@pytest.mark.parametrize(
    ("stt_provider", "health", "expected"),
    [
        ("configured", {"stt_locality": "local"}, "local"),
        ("local", {"stt_locality": "local"}, "local"),
        ("configured", {}, "unknown"),
        ("configured", {"stt_locality": "/home/x/.env"}, "unknown"),
        ("elevenlabs", {"stt_locality": "local"}, "cloud"),
        ("configured", {"stt_locality": "cloud"}, "cloud"),
    ],
)
def test_runtime_locality_follows_effective_provider_and_bridge_receipt(stt_provider, health, expected) -> None:
    config = AppConfig(api_key="k", conversation_mode="pipeline", stt_provider=stt_provider)
    assert voice_pipeline.stt_locality(config, health) == expected


def test_child_locked_status_exposes_only_the_categorical_flag() -> None:
    runtime = HermesVoiceRuntime(SimpleNamespace(media=Media(), cancel_move=lambda: None), threading.Event())
    runtime._audio_ready = True
    runtime._notify_bridge_kids_session_async = lambda *_a, **_k: None  # type: ignore[method-assign]
    runtime.start_kids_mode(profile_for("quiz", "en"), greet=False)
    runtime._status.stt_locality = "/private/path"
    assert runtime.status()["stt_locality"] == "unknown"
    runtime._status.stt_locality = "local"
    status = runtime.status()
    assert status["stt_locality"] == "local"
    assert "stt_provider" not in status
