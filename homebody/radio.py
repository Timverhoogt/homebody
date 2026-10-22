"""One fixed internet radio stream decoded locally into Reachy's existing playback gate.

No microphone, camera, motor, shell command, arbitrary URL or separate audio device.
GStreamer is optional: unsupported hosts fail explicitly, without installing anything.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

STATION_NAME = "laut.fm Lofi"
STATION_URL = "https://stream.laut.fm/lofi"
# Software scheduling targets, not measurements of the opaque hardware queue.
_RESERVE_SECONDS = 0.12

_LOGGER = logging.getLogger(__name__)
_ERRORS = {
    "decoder_unavailable": "Radio needs the optional GStreamer audio decoder on this robot",
    "decoder_start_failed": "Radio decoder could not start",
    "stream_decode_failed": "Radio stream ended or could not be decoded",
    "stream_timeout": "Radio stream timed out",
    "invalid_audio": "Radio decoder returned invalid audio",
    "output_failed": "Radio could not enqueue audio on Reachy's speaker",
    "cleanup_failed": "Radio audio cleanup failed",
    "startup_timeout": "Radio stream did not become ready in time",
}


class RadioFailure(RuntimeError):
    """Only fixed diagnostic codes/text cross the status and log boundaries."""

    def __init__(self, code: str):
        self.code = code if code in _ERRORS else "stream_decode_failed"
        super().__init__(_ERRORS[self.code])


class GstRadioDecoder:
    def __init__(self, sample_rate: int):
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except (ImportError, ValueError) as exc:
            raise RadioFailure("decoder_unavailable") from exc
        Gst.init(None)
        self.gst = Gst
        self.pipeline = Gst.parse_launch(
            'uridecodebin uri="' + STATION_URL + '" ! audioconvert ! audioresample ! '
            f"audio/x-raw,format=F32LE,rate={sample_rate},channels=1 ! "
            # Let our sample-count clock pace playback. Backpressure, not dropping,
            # preserves PCM continuity when decoding briefly outruns consumption.
            "appsink name=radio_sink emit-signals=false sync=false max-buffers=4 drop=false"
        )
        self.sink = self.pipeline.get_by_name("radio_sink")
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RadioFailure("decoder_start_failed")

    def read(self) -> np.ndarray | None:
        sample = self.sink.emit("try-pull-sample", 100_000_000)
        if sample is None:
            message = self.pipeline.get_bus().pop_filtered(self.gst.MessageType.ERROR | self.gst.MessageType.EOS)
            if message is not None:
                raise RadioFailure("stream_decode_failed")
            return None
        buffer = sample.get_buffer()
        # Copy before releasing the GStreamer sample; never retain a mapped native buffer.
        return np.frombuffer(buffer.extract_dup(0, buffer.get_size()), dtype="<f4").copy()

    def close(self) -> None:
        self.pipeline.set_state(self.gst.State.NULL)


class RadioPlayer:
    """Single bounded worker. Pause drains/clears its own output before voice may start.

    Stop is nonblocking: an opaque SDK push may finish late, but the worker then clears
    it before relinquishing ownership. New playback/voice refuses while that push is held.
    """

    def __init__(self, playback, sample_rate: int, *, allowed: Callable[[], bool], decoder_factory=GstRadioDecoder):
        self.playback, self.sample_rate = playback, sample_rate
        self.allowed, self.decoder_factory = allowed, decoder_factory
        self.lock = threading.RLock()
        self.push_lock = threading.Lock()
        self.cancelled = threading.Event()
        self.ready = threading.Event()
        self.paused = True
        self._playback_generation = 0
        self.state = "connecting"
        self.error = ""
        self.error_code = ""
        self.enqueued = 0
        self.dirty = False
        self.thread = threading.Thread(target=self._run, name="homebody-radio", daemon=True)

    def start(self) -> dict:
        self.thread.start()
        if not self.ready.wait(8):
            self._fail("startup_timeout")
            self.stop()
            raise RadioFailure("startup_timeout")
        if self.error:
            raise RuntimeError(self.error)
        return self.status()

    def status(self) -> dict:
        with self.lock:
            return {
                "station": STATION_NAME,
                "state": self.state,
                "paused_for_voice": self.paused,
                "player_enqueued": self.enqueued > 0,
                "audible_verified": False,
                "error": self.error,
                "error_code": self.error_code,
            }

    def _fail(self, code: str) -> None:
        failure = RadioFailure(code)
        with self.lock:
            self.error_code = failure.code
            self.error = str(failure)
            self.state = "failed"
        _LOGGER.warning("Radio failure category=%s", failure.code)

    def pause(self) -> None:
        with self.lock:
            self.paused = True
            self._playback_generation += 1
        _LOGGER.info("Radio playback paused for voice")
        if not self.push_lock.acquire(timeout=2):
            raise RuntimeError("Radio output is still in flight; try again shortly")
        try:
            if self.dirty:
                self.playback.clear()
                self.dirty = False
        finally:
            self.push_lock.release()

    def resume(self) -> None:
        with self.lock:
            if not self.cancelled.is_set():
                if self.paused:
                    self._playback_generation += 1
                self.paused = False
                _LOGGER.info("Radio playback resumed")

    def stop(self) -> None:
        if not self.cancelled.is_set():
            _LOGGER.info("Radio stop requested")
        self.cancelled.set()
        with self.lock:
            if not self.error:
                self.state = "stopping" if self.thread.is_alive() else "stopped"
        # The producer owns cleanup, including a push which returns after Stop.
        # Never call an opaque SDK clear on the caller's Stop thread.

    def _run(self) -> None:
        decoder = None
        queued_until = None
        pacing_generation = None
        pending = deque()
        pending_samples = 0
        prefill = True
        stage = "decoder_start_failed"
        _LOGGER.info("Radio connecting (sample_rate=%d)", self.sample_rate)
        try:
            decoder = self.decoder_factory(self.sample_rate)
            last_sample = time.monotonic()
            while not self.cancelled.is_set():
                if not self.allowed():
                    self.cancelled.set()
                    break
                with self.lock:
                    generation = self._playback_generation
                    paused = self.paused
                if generation != pacing_generation:
                    pending.clear()
                    pending_samples = 0
                    queued_until = None
                    prefill = True
                    pacing_generation = generation
                if paused or not pending or (prefill and pending_samples / self.sample_rate < _RESERVE_SECONDS):
                    stage = "stream_decode_failed"
                    samples = decoder.read()
                    now = time.monotonic()
                    if samples is None or not samples.size:
                        if now - last_sample > 8:
                            raise RadioFailure("stream_timeout")
                        continue
                    last_sample = now
                    if samples.size > self.sample_rate or not np.isfinite(samples).all():
                        raise RadioFailure("invalid_audio")
                    with self.lock:
                        if self.paused or not self.enqueued:
                            self.state = "ready"
                        current = generation == self._playback_generation
                    if not self.ready.is_set():
                        _LOGGER.info("Radio stream decoded and ready")
                    self.ready.set()  # Decoder readiness is not acoustic acceptance.
                    if not current:
                        continue
                    if paused:
                        # Consume/discard while voice owns the speaker. No old PCM
                        # is retained for a later resume; avoid a busy decoder loop.
                        self.cancelled.wait(min(samples.size / self.sample_rate, 0.04))
                        continue
                    step = max(1, self.sample_rate // 25)
                    pending.extend(samples[offset : offset + step] for offset in range(0, samples.size, step))
                    pending_samples += samples.size
                    # The deque is bounded by the reserve plus one validated <=1 s
                    # decoder buffer. Actually collect PCM, not merely sleep.
                    if prefill and pending_samples / self.sample_rate < _RESERVE_SECONDS:
                        continue
                prefill = False
                now = time.monotonic()
                if queued_until is not None and now > queued_until + 0.001:
                    _LOGGER.info("Radio rebuffering after producer stall")
                    queued_until = None
                    prefill = True
                    continue  # Refill instead of an unbounded catch-up burst.
                delay = max(0.0, queued_until - _RESERVE_SECONDS - now) if queued_until is not None else 0.0
                # Wait outside speaker ownership: pause/clear remains responsive.
                if self.cancelled.wait(delay):
                    break
                with self.push_lock:
                    with self.lock:
                        play = (
                            not self.paused and not self.cancelled.is_set() and generation == self._playback_generation
                        )
                    if not play:
                        continue
                    if self.allowed():
                        stage = "output_failed"
                        chunk = pending.popleft()
                        pending_samples -= chunk.size
                        self.dirty = True
                        sent_at = time.monotonic()
                        self.playback.push_audio_sample(np.clip(chunk, -1, 1) * 0.5)
                        queued_until = max(sent_at, queued_until or sent_at) + chunk.size / self.sample_rate
                        with self.lock:
                            if not self.enqueued:
                                _LOGGER.info("Radio first PCM enqueued (audibility_unverified=true)")
                            self.enqueued += chunk.size
                            self.state = "playing"

        except Exception as exc:
            self._fail(exc.code if isinstance(exc, RadioFailure) else stage)
        finally:
            self.ready.set()
            if decoder is not None:
                try:
                    decoder.close()
                except Exception:
                    self._fail("cleanup_failed")
            with self.push_lock:
                if self.dirty:
                    try:
                        self.playback.clear()
                    except Exception:
                        self._fail("cleanup_failed")
                    finally:
                        self.dirty = False
            with self.lock:
                if not self.error:
                    self.state = "stopped"
            _LOGGER.info("Radio worker exited (state=%s samples_enqueued=%d)", self.state, self.enqueued)
