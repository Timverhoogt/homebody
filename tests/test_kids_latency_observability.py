"""Text-free Kids latency regressions; synthetic PCM, no providers or hardware."""
import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import numpy as np
import pytest
from test_kids_stt_acknowledgements import Harness

from homebody import voice_pipeline
from homebody.config import AppConfig
from homebody.robot_stt import RobotReceipt
from homebody.runtime import HermesVoiceRuntime
from homebody.stt_routing import SttTurnCancelled, transcribe_endpoint
from homebody.voice_timing import VoiceTimings


@pytest.mark.parametrize("kids", [True, False])
def test_processing_cue_does_not_delay_kids_submission(monkeypatch, tmp_path, kids):
    h = Harness(monkeypatch, tmp_path, stt_seconds=0, catalog=False)
    assets = []
    monkeypatch.setattr(h.runtime, "_play_asset", lambda name: assets.append(name))
    cfg = h.config if kids else replace(h.config, kids_mode_enabled=False, continuous_conversation=False)

    def cancel_after_submission(*args, **kwargs):
        assert ("processing.wav" in assets) is (not kids)
        raise SttTurnCancelled("synthetic cancellation")

    monkeypatch.setattr(voice_pipeline, "transcribe_endpoint", cancel_after_submission)
    h.runtime._accept_wake_turn()
    h.runtime._run_conversation(cfg)
    assert ("processing.wav" in assets) is (not kids)


@pytest.mark.parametrize("push_fails", [False, True])
def test_kids_stream_records_first_pcm_and_successful_sdk_enqueue(monkeypatch, tmp_path, push_fails):
    h = Harness(monkeypatch, tmp_path, stt_seconds=0, catalog=False)
    r = h.runtime
    now = [0.0]
    r._voice_timings = VoiceTimings(clock=lambda: now[0])
    r._voice_timings.begin("pipeline", "everyday", warm=False)
    r._read_16k_frame = lambda: None
    r._accept_wake_turn()

    def chunks(*args, **kwargs):
        now[0] = 1.25
        yield b"\x01\x00" * 1200

    def push(samples):
        assert samples.size > 0
        if push_fails:
            raise RuntimeError("synthetic SDK refusal")
        now[0] = 1.28

    h.media.push_audio_sample = push
    client = cast(Any, SimpleNamespace(config=h.config, iter_kids_speech=chunks, last_tts_provider="test-pcm"))
    if push_fails:
        with pytest.raises(RuntimeError, match="synthetic SDK refusal"):
            HermesVoiceRuntime._play_kids_stream(r, client, "not retained", barge_in=False)
    else:
        assert HermesVoiceRuntime._play_kids_stream(r, client, "not retained", barge_in=False) is False
    snapshot = r._voice_timings.snapshot()[0]
    marks = snapshot["marks"]
    assert isinstance(marks, dict)
    assert marks["first_provider_audio"] == 1250
    if push_fails:
        assert "player_enqueued" not in marks
    else:
        assert marks["player_enqueued"] == 1280
    assert "not retained" not in str(snapshot)


@pytest.mark.parametrize("current", [True, False])
def test_robot_decode_diagnostics_are_numeric_and_never_log_stale_text(caplog, current):
    receipt = RobotReceipt("PRIVATE SYNTHETIC TRANSCRIPT", "gpu", 41)
    robot = SimpleNamespace(transcribe=lambda *args, **kwargs: receipt)
    runtime = SimpleNamespace(_robot_stt_client=lambda: robot)
    cfg = AppConfig(stt_provider="robot", stt_robot_fallback="none")
    with caplog.at_level(logging.INFO):
        if current:
            result = transcribe_endpoint(runtime, None, cfg, np.zeros(160, dtype=np.float32),
                                         is_current=lambda: True, session_id="synthetic", generation=1)
            assert result.text == receipt.text
        else:
            with pytest.raises(SttTurnCancelled):
                transcribe_endpoint(runtime, None, cfg, np.zeros(160, dtype=np.float32),
                                    is_current=lambda: False, session_id="synthetic", generation=1)
    records = [x.getMessage() for x in caplog.records if "Robot STT final" in x.getMessage()]
    if current:
        assert len(records) == 1
        assert "worker_decode_ms=41" in records[0] and "round_trip_ms=" in records[0]
    else:
        assert records == []
    assert "PRIVATE SYNTHETIC TRANSCRIPT" not in caplog.text
