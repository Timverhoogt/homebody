"""One fixed internet radio stream decoded locally into Reachy's existing playback gate.

No microphone, camera, motor, shell command, arbitrary URL or separate audio device.
GStreamer is optional: unsupported hosts fail explicitly, without installing anything.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import numpy as np

STATION_NAME = "laut.fm Lofi"
STATION_URL = "https://stream.laut.fm/lofi"


class GstRadioDecoder:
    def __init__(self, sample_rate: int):
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except (ImportError, ValueError) as exc:
            raise RuntimeError("Radio needs the optional GStreamer audio decoder on this robot") from exc
        Gst.init(None)
        self.gst = Gst
        self.pipeline = Gst.parse_launch(
            'uridecodebin uri="' + STATION_URL + '" ! audioconvert ! audioresample ! '
            f"audio/x-raw,format=F32LE,rate={sample_rate},channels=1 ! "
            "appsink name=radio_sink emit-signals=false sync=true max-buffers=2 drop=true"
        )
        self.sink = self.pipeline.get_by_name("radio_sink")
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self.close()
            raise RuntimeError("Radio decoder could not start")

    def read(self) -> np.ndarray | None:
        sample = self.sink.emit("try-pull-sample", 100_000_000)
        if sample is None:
            message = self.pipeline.get_bus().pop_filtered(self.gst.MessageType.ERROR | self.gst.MessageType.EOS)
            if message is not None:
                raise RuntimeError("Radio stream ended or could not be decoded")
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
        self.state = "connecting"
        self.error = ""
        self.enqueued = 0
        self.dirty = False
        self.thread = threading.Thread(target=self._run, name="homebody-radio", daemon=True)

    def start(self) -> dict:
        self.thread.start()
        if not self.ready.wait(8):
            self.stop()
            raise RuntimeError("Radio stream did not become ready in time")
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
            }

    def pause(self) -> None:
        with self.lock:
            self.paused = True
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
                self.paused = False

    def stop(self) -> None:
        self.cancelled.set()
        with self.lock:
            self.state = "stopping" if self.thread.is_alive() else "stopped"
        # The producer owns cleanup, including a push which returns after Stop.
        # Never call an opaque SDK clear on the caller's Stop thread.

    def _run(self) -> None:
        decoder = None
        try:
            decoder = self.decoder_factory(self.sample_rate)
            last_sample = time.monotonic()
            while not self.cancelled.is_set():
                if not self.allowed():
                    self.cancelled.set()
                    break
                samples = decoder.read()
                now = time.monotonic()
                if samples is None or not samples.size:
                    if now - last_sample > 8:
                        raise RuntimeError("Radio stream timed out")
                    continue
                last_sample = now
                if samples.size > self.sample_rate or not np.isfinite(samples).all():
                    raise RuntimeError("Radio decoder returned invalid audio")
                with self.lock:
                    if self.paused or not self.enqueued:
                        self.state = "ready"
                self.ready.set()  # Decoded PCM readiness, not proof of audible playback.
                # Use <=40 ms chunks and wall-clock pacing; SDK queue depth is opaque.
                for offset in range(0, samples.size, max(1, self.sample_rate // 25)):
                    chunk = samples[offset : offset + max(1, self.sample_rate // 25)]
                    with self.push_lock:
                        with self.lock:
                            play = not self.paused and not self.cancelled.is_set()
                        if play and self.allowed():
                            self.dirty = True
                            self.playback.push_audio_sample(np.clip(chunk, -1, 1) * 0.5)
                            with self.lock:
                                self.enqueued += chunk.size
                                self.state = "playing"
                    # Pace even while paused so decoding cannot race through the live input.
                    if self.cancelled.wait(chunk.size / self.sample_rate):
                        break
        except Exception as exc:
            with self.lock:
                self.error = str(exc)[:200]
                self.state = "failed"
        finally:
            self.ready.set()
            if decoder is not None:
                try:
                    decoder.close()
                except Exception:
                    with self.lock:
                        self.error = "Radio decoder cleanup failed"
                        self.state = "failed"
            with self.push_lock:
                if self.dirty:
                    self.playback.clear()
                    self.dirty = False
            with self.lock:
                if not self.error:
                    self.state = "stopped"
