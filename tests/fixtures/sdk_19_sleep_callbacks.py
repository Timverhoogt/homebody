"""Unmodified SDK 1.9 callback bodies for offline adversarial interleavings.

Source: pollen-robotics/reachy_mini tag v1.9.0, Apache-2.0.
No native SDK/media imports or hardware construction occur here.
"""
# ruff: noqa: F821
from __future__ import annotations


# https://raw.githubusercontent.com/pollen-robotics/reachy_mini/v1.9.0/src/reachy_mini/motion/head_wobbler.py
def _fire(self, offsets: SpeechOffsets, generation: int) -> bool:
        """GLib timeout callback. Returns False so the source is removed."""
        with self._lock:
            current = self._generation
        if generation == current:
            self._apply_offsets(offsets)
        return False


# https://raw.githubusercontent.com/pollen-robotics/reachy_mini/v1.9.0/src/reachy_mini/media/audio_gstreamer.py
def _on_wobbler_sample(self, appsink: Gst.Element) -> Gst.FlowReturn:
        """GStreamer callback: forward audio buffer to the head wobbler.

        The appsink is ``sync=True``, so this callback fires at the
        buffer's PTS on the pipeline clock — audio is playing NOW.
        """
        sample = appsink.pull_sample()
        if sample is None or self._head_wobbler is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        data = buf.extract_dup(0, buf.get_size())
        pcm = np.frombuffer(data, dtype=np.float32)
        self._head_wobbler.feed(pcm, time.monotonic_ns())
        return Gst.FlowReturn.OK


# https://raw.githubusercontent.com/pollen-robotics/reachy_mini/v1.9.0/src/reachy_mini/media/audio_gstreamer.py
def clear_player(self) -> None:
        """Flush the player's appsrc to drop any queued audio immediately."""
        if self._head_wobbler is not None:
            self._head_wobbler.reset()
        if self._appsrc is not None:
            self._appsrc_pts = -1
            self._pipeline.set_state(Gst.State.PAUSED)
            self._appsrc.send_event(Gst.Event.new_flush_start())
            self._appsrc.send_event(Gst.Event.new_flush_stop(reset_time=True))
            self._pipeline.set_state(Gst.State.PLAYING)
            self.logger.info("Cleared player queue")
        else:
            self.logger.warning(
                "AppSrc is not initialized. Call start_playing() first."
            )
