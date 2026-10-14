"""Local STT contract for shared host play sessions and game switches (fake model, no audio).

This is the Hermes-host Whisper worker that the robot route may use as its single local fallback.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from companion import local_stt_command


class _EchoModel:
    """Returns the audio file's bytes as text, so any cached or stale transcript is visible."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def transcribe(self, path: str, **kwargs: object):
        self.calls.append({"path": path, **kwargs})
        text = Path(path).read_text(encoding="utf-8")
        return iter([SimpleNamespace(text=text, no_speech_prob=0.1, avg_logprob=-0.2)]), None


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_language_hint_is_read_from_the_basename_only(tmp_path: Path) -> None:
    hinted_dir = tmp_path / "homebody-stt-nl-dir"
    hinted_dir.mkdir()
    assert local_stt_command.language_hint(hinted_dir / "homebody-stt-en-abc.wav") == "en"
    # A hint-shaped directory never selects a language for an unhinted (adult) upload.
    assert local_stt_command.language_hint(hinted_dir / "homebody-stt-abc.wav") is None
    assert local_stt_command.language_hint(tmp_path / "x-homebody-stt-nl-abc.wav") is None
    assert local_stt_command.language_hint(tmp_path / "homebody-stt-NL-abc.wav") is None


def test_one_warm_model_follows_each_turns_language_without_caching(tmp_path: Path) -> None:
    """A game switch or language change between turns needs no reload and leaks no state."""
    loads: list[_EchoModel] = []

    def factory() -> _EchoModel:
        loads.append(_EchoModel())
        return loads[-1]

    short = tempfile.TemporaryDirectory(prefix="hbs")  # Uses the configured TMPDIR; AF_UNIX paths are length-limited
    socket_path = Path(short.name) / "w" / "worker.sock"
    served: list[int] = []
    server = threading.Thread(
        target=lambda: served.append(local_stt_command.serve(socket_path, idle_seconds=0.5, model_factory=factory)),
    )
    server.start()
    try:
        assert _wait_for(socket_path.exists)
        turns = [
            ("homebody-stt-en-a.wav", "The yellow ball."),  # Kids game A, English session
            ("homebody-stt-nl-b.wav", "De gele bal."),  # switched game/session language
            ("homebody-stt-c.wav", "Adult turn."),  # adult upload in between: auto-detect
            ("homebody-stt-nl-b.wav", "Ik weet het niet."),  # same path name, new audio
        ]
        texts = []
        for name, content in turns:
            audio = tmp_path / name
            audio.write_text(content, encoding="utf-8")
            texts.append(local_stt_command.request(socket_path, audio))
        assert texts == [content for _, content in turns]
    finally:
        server.join(timeout=5)
        short.cleanup()
    assert not server.is_alive()
    assert len(loads) == 1  # warm across the language and game switch
    calls = loads[0].calls
    assert [call["language"] for call in calls] == ["en", "nl", None, "nl"]
    for call in calls:
        # Decoding quality options are identical for every turn, whichever game asked.
        assert {key: call[key] for key in local_stt_command.DECODE_OPTIONS} == local_stt_command.DECODE_OPTIONS
    assert served == [4]


def test_decode_quality_options_are_preserved() -> None:
    assert local_stt_command.MODEL_NAME == "base"
    assert local_stt_command.DECODE_OPTIONS == {
        "beam_size": 5,
        "vad_filter": True,
        "condition_on_previous_text": False,
        "without_timestamps": True,
    }
