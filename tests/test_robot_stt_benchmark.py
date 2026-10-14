"""The device benchmark drives the real worker protocol; recognition timing report spans.

The worker backend here is the explicit test fake, so no number from this test is a device or
model measurement.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import wave
from pathlib import Path

import numpy as np
from test_asr_worker import FAKE, short_dir, write_manifest  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def test_benchmark_runs_through_worker_protocol(short_dir):  # noqa: F811
    bench = load_tool("robot_stt_benchmark")
    corpus_dir = short_dir / "corpus"
    corpus_dir.mkdir()
    tone = np.sin(np.linspace(0, 400 * np.pi, 8000)) * 0.3
    write_wav(corpus_dir / "nl.wav", tone, 16000)
    write_wav(corpus_dir / "en48k.wav", np.repeat(tone, 3), 48000)
    write_wav(corpus_dir / "silence.wav", np.zeros(16000), 16000)
    corpus = corpus_dir / "corpus.json"
    corpus.write_text(json.dumps({"clips": [
        {"wav": "nl.wav", "language": "nl", "expected": "ja", "kind": "command"},
        {"wav": "en48k.wav", "language": "en", "expected": "blue", "kind": "command"},
        {"wav": "silence.wav", "language": "auto", "expected": "", "kind": "silence"},
    ]}))
    before = sorted(p.name for p in Path(bench.tempfile.gettempdir()).glob("hbstt-bench-*"))
    report = bench.run(
        corpus, write_manifest(short_dir / "prov"), repeats=2, include_hypotheses=False,
        load_timeout=10, decode_timeout=10,
        worker_argv=lambda sock, manifest: [sys.executable, str(FAKE), "ok", str(sock), str(manifest)],
    )
    assert report["identity"]["backend"] == "fake" and report["accelerator_reported"] == "cpu"
    assert report["failures"] == []
    assert report["cold"]["worker_load_ms"] >= 0 and report["memory"]["worker_peak_rss_kb"] > 0
    assert report["warm"]["client_round_trip"]["n"] == 3
    assert report["accuracy"]["silence_false_transcripts"] == "0/1"
    assert report["accuracy"]["command_exact_match"] == "0/2"  # the fake does not recognise words
    assert all("hypothesis" not in row for row in report["clips"])
    assert [row["audio_s"] for row in report["clips"]] == [0.5, 0.5, 1.0]
    after = sorted(p.name for p in Path(bench.tempfile.gettempdir()).glob("hbstt-bench-*"))
    assert after == before  # private run directory removed, no audio written


def test_built_wheel_carries_robot_stt_source_and_no_native_artifacts():
    import hashlib
    import zipfile

    import pytest

    from homebody import __version__

    wheels = sorted((ROOT / "dist").glob(f"reachy_mini_homebody-{__version__}-*.whl"))
    if not wheels:
        pytest.skip("no wheel has been built in dist/")
    with zipfile.ZipFile(wheels[-1]) as wheel:
        names = set(wheel.namelist())
        sources = [p for p in (ROOT / "homebody").rglob("*") if p.is_file() and "__pycache__" not in p.parts]
        for path in sources:
            name = path.relative_to(ROOT).as_posix()
            if name in names:
                assert wheel.read(name) == path.read_bytes(), name
        for module in ("asr_worker.py", "robot_stt.py", "stt_routing.py", "voice_pipeline.py", "proactive.py"):
            assert f"homebody/{module}" in names, module
        # The generic wheel never embeds a native ASR runtime or ASR weights (the existing small
        # gesture ONNX models are a separate, already-shipped feature).
        assert not [n for n in names if n.endswith((".so", ".gguf", ".nemo", ".engine"))]
        assert not [n for n in names if any(k in n.casefold() for k in ("parakeet", "nemo_speech", "whisper"))]
        assert not [n for n in names if "robot-stt" in n and n.endswith(".json")]
    print("WHEEL", wheels[-1].name, hashlib.sha256(wheels[-1].read_bytes()).hexdigest())


def test_timing_report_includes_recognition_spans():
    report = load_tool("voice_timing_report")
    summary = report.summarize([{"engine": "pipeline", "mode": "everyday", "warm": True, "marks": {
        "speech_last_voiced": 900, "local_speech_end": 1700, "endpoint_final": 1700, "stt_submitted": 1710,
        "stt_final": 2100, "tool_result": 2600,
    }}])["pipeline/everyday/warm"]
    assert summary["speech_last_voiced->endpoint_final"]["p50_ms"] == 800
    assert summary["endpoint_final->stt_final"]["p50_ms"] == 400
    assert summary["stt_submitted->stt_final"]["p50_ms"] == 390
