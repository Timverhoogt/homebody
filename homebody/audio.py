"""Audio normalization, adaptive endpointing, and WAV helpers."""

from __future__ import annotations

import io
import math
import time
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

FloatAudio = npt.NDArray[np.float32]


def mono_float32(frame: npt.ArrayLike) -> FloatAudio:
    """Normalize Reachy audio frames to contiguous mono float32 samples."""
    audio = np.asarray(frame)
    if audio.size == 0:
        return np.empty(0, dtype=np.float32)
    if audio.ndim == 2:
        # Reachy normally returns channels-last (samples, channels).
        if audio.shape[0] <= 8 < audio.shape[1]:
            audio = audio.T
        audio = audio.mean(axis=1)
    elif audio.ndim > 2:
        audio = audio.reshape(-1)
    if np.issubdtype(audio.dtype, np.integer):
        max_value = float(np.iinfo(audio.dtype).max)
        audio = audio.astype(np.float32) / max_value
    else:
        audio = audio.astype(np.float32, copy=False)
    return np.ascontiguousarray(np.clip(audio, -1.0, 1.0), dtype=np.float32)


def resample_linear(samples: FloatAudio, source_rate: int, target_rate: int = 16000) -> FloatAudio:
    if source_rate <= 0:
        raise ValueError("source_rate must be positive")
    if source_rate == target_rate or samples.size == 0:
        return samples.astype(np.float32, copy=False)
    target_length = max(1, round(samples.size * target_rate / source_rate))
    old_positions = np.linspace(0.0, 1.0, samples.size, endpoint=False)
    new_positions = np.linspace(0.0, 1.0, target_length, endpoint=False)
    return np.interp(new_positions, old_positions, samples).astype(np.float32)


def rms(samples: FloatAudio) -> float:
    if samples.size == 0:
        return 0.0
    return float(math.sqrt(float(np.mean(np.square(samples, dtype=np.float64)))))


def encode_wav(samples: FloatAudio, sample_rate: int = 16000) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())
    return output.getvalue()


@dataclass(frozen=True, slots=True)
class EndpointResult:
    samples: FloatAudio
    speech_detected: bool
    reason: str
    threshold: float


class NoiseFloor:
    """Conservative rolling estimator used before wake activation."""

    def __init__(self, window: int = 100) -> None:
        self._values: deque[float] = deque(maxlen=window)

    def update(self, samples: FloatAudio) -> None:
        level = rms(samples)
        if level > 0:
            self._values.append(level)

    @property
    def value(self) -> float:
        if not self._values:
            return 0.004
        # Lower-middle percentile avoids a short speech burst inflating the floor.
        return float(np.percentile(np.asarray(self._values), 35))


class AdaptiveEndpointRecorder:
    """Record one utterance using adaptive RMS speech endpointing."""

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        initial_timeout: float = 5.0,
        max_duration: float = 20.0,
        end_silence: float = 0.8,
        minimum_rms: float = 0.012,
        noise_multiplier: float = 3.0,
        pre_roll_seconds: float = 0.25,
    ) -> None:
        self.sample_rate = sample_rate
        self.initial_timeout = initial_timeout
        self.max_duration = max_duration
        self.end_silence = end_silence
        self.minimum_rms = minimum_rms
        self.noise_multiplier = noise_multiplier
        self.pre_roll_seconds = pre_roll_seconds

    def record(
        self,
        read_frame: Callable[[], FloatAudio | None],
        *,
        noise_floor: float,
        should_stop: Callable[[], bool],
    ) -> EndpointResult:
        threshold = max(self.minimum_rms, noise_floor * self.noise_multiplier)
        started_at = time.monotonic()
        speech_started_at: float | None = None
        last_speech_at: float | None = None
        consecutive_voice = 0
        captured: list[FloatAudio] = []
        pre_roll: deque[FloatAudio] = deque()
        pre_roll_samples = 0
        pre_roll_limit = int(self.pre_roll_seconds * self.sample_rate)

        while not should_stop():
            now = time.monotonic()
            if now - started_at >= self.max_duration:
                reason = "max_duration" if speech_started_at is not None else "initial_timeout"
                return EndpointResult(self._join(captured), speech_started_at is not None, reason, threshold)
            if speech_started_at is None and now - started_at >= self.initial_timeout:
                return EndpointResult(np.empty(0, dtype=np.float32), False, "initial_timeout", threshold)

            frame = read_frame()
            if frame is None or frame.size == 0:
                time.sleep(0.002)
                continue

            level = rms(frame)
            is_voice = level >= threshold
            if speech_started_at is None:
                pre_roll.append(frame)
                pre_roll_samples += frame.size
                while pre_roll and pre_roll_samples > pre_roll_limit:
                    pre_roll_samples -= pre_roll.popleft().size
                consecutive_voice = consecutive_voice + 1 if is_voice else 0
                if consecutive_voice >= 2:
                    speech_started_at = now
                    last_speech_at = now
                    captured.extend(pre_roll)
                    pre_roll.clear()
                continue

            captured.append(frame)
            if is_voice:
                last_speech_at = now
            elif last_speech_at is not None and now - last_speech_at >= self.end_silence:
                return EndpointResult(self._join(captured), True, "end_silence", threshold)

        return EndpointResult(self._join(captured), speech_started_at is not None, "stopped", threshold)

    @staticmethod
    def _join(frames: list[FloatAudio]) -> FloatAudio:
        if not frames:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(frames).astype(np.float32, copy=False)
