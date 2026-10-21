"""Radio failures keep useful categories without exposing native exception bodies."""

import logging

import pytest
from test_radio import Decoder, Output, finish

from homebody import radio


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("constructor", "decoder_start_failed"),
        ("read", "stream_decode_failed"),
        ("output", "output_failed"),
        ("cleanup", "cleanup_failed"),
    ],
)
def test_radio_diagnostics_never_expose_native_exception_text(caplog, stage, expected):
    private = "PrivateUtterance-SecretToken123456789"

    class NativeFailure(Decoder):
        def __init__(self, rate):
            if stage == "constructor":
                raise RuntimeError(private)
            super().__init__(rate)

        def read(self):
            if stage == "read":
                raise RuntimeError(private)
            return super().read()

        def close(self):
            if stage == "cleanup":
                raise RuntimeError(private)
            return super().close()

    out = Output()
    if stage == "output":

        def fail(data):
            raise OSError(private)

        out.push_audio_sample = fail
    p = radio.RadioPlayer(out, 48000, allowed=lambda: True, decoder_factory=NativeFailure)
    caplog.set_level(logging.INFO, logger="homebody.radio")
    try:
        try:
            p.start()
            p.resume()
        except RuntimeError:
            pass
        if stage == "output":
            p.thread.join(1)
    finally:
        finish(p)
    assert p.status()["error_code"] == expected
    assert private not in str(p.status()) + caplog.text
    assert "Radio failure category=" + expected in caplog.text
    assert "Radio worker exited" in caplog.text


def test_arbitrary_error_code_cannot_enter_diagnostics():
    error = radio.RadioFailure("PrivateToken123456789")
    assert error.code == "stream_decode_failed"
    assert "PrivateToken" not in str(error)
