import sys
from types import SimpleNamespace

from companion.local_stt_command import main


def test_local_command_is_cached_cpu_only_and_filters_silence(tmp_path, monkeypatch):
    calls = []

    class Model:
        def __init__(self, model, **kwargs):
            calls.append((model, kwargs))

        def transcribe(self, path, **kwargs):
            calls.append((path, kwargs))
            return iter([
                SimpleNamespace(text="De gele bal.", no_speech_prob=0.1, avg_logprob=-0.2),
                SimpleNamespace(text="silence hallucination", no_speech_prob=0.95, avg_logprob=-2),
            ]), None

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(sys, "argv", ["local_stt_command", "--input", "fixture.wav", "--output-dir", str(tmp_path),
                                      "--no-worker"])
    main()
    assert (tmp_path / "transcript.txt").read_text() == "De gele bal."
    assert calls[0][1]["local_files_only"] is True
    assert calls[0][1]["device"] == "cpu"
    assert calls[0][1]["cpu_threads"] == 2
    assert calls[1][1]["language"] is None
    assert calls[1][1]["vad_filter"] is True
    assert calls[1][1]["condition_on_previous_text"] is False
