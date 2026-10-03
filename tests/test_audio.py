from __future__ import annotations

import itertools
from collections.abc import Iterator

import numpy as np

import homebody.audio as audio_module
from homebody.audio import AdaptiveEndpointRecorder, encode_wav, mono_float32, resample_linear


def test_audio_normalization_and_resample() -> None:
    stereo = np.column_stack((np.full(480, 0.25), np.full(480, -0.25))).astype(np.float32)
    mono = mono_float32(stereo)
    assert mono.shape == (480,)
    assert np.allclose(mono, 0.0)

    source = np.linspace(-1, 1, 480, dtype=np.float32)
    result = resample_linear(source, 48000, 16000)
    assert result.shape == (160,)
    assert result.dtype == np.float32


def test_encode_wav_has_riff_header() -> None:
    encoded = encode_wav(np.zeros(1600, dtype=np.float32), 16000)
    assert encoded[:4] == b"RIFF"
    assert encoded[8:12] == b"WAVE"


def test_endpoint_recorder_keeps_speech_and_stops_after_silence(monkeypatch) -> None:
    clock = itertools.count(step=0.01)
    monkeypatch.setattr(audio_module.time, "monotonic", lambda: next(clock))

    frames: Iterator[np.ndarray] = iter(
        [np.zeros(160, dtype=np.float32) for _ in range(8)]
        + [np.full(160, 0.08, dtype=np.float32) for _ in range(20)]
        + [np.zeros(160, dtype=np.float32) for _ in range(20)]
    )

    def read_frame() -> np.ndarray | None:
        return next(frames, np.zeros(160, dtype=np.float32))

    recorder = AdaptiveEndpointRecorder(
        initial_timeout=1.0,
        max_duration=3.0,
        end_silence=0.12,
        minimum_rms=0.01,
        noise_multiplier=2.0,
    )
    result = recorder.record(read_frame, noise_floor=0.002, should_stop=lambda: False)
    assert result.speech_detected is True
    assert result.reason == "end_silence"
    assert result.samples.size > 20 * 160


def test_endpoint_recorder_times_out_without_speech(monkeypatch) -> None:
    clock = itertools.count(step=0.02)
    monkeypatch.setattr(audio_module.time, "monotonic", lambda: next(clock))
    recorder = AdaptiveEndpointRecorder(initial_timeout=0.2, max_duration=1.0)
    result = recorder.record(
        lambda: np.zeros(160, dtype=np.float32),
        noise_floor=0.002,
        should_stop=lambda: False,
    )
    assert result.speech_detected is False
    assert result.reason == "initial_timeout"
