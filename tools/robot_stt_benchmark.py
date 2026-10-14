"""Benchmark the on-robot ASR worker through its real protocol with synthetic WAV fixtures.

Usage (on the device, from a source checkout; no microphone, network or cloud credentials):

    python3 tools/robot_stt_benchmark.py --corpus corpus.json --provision /path/robot-stt/manifest.json \
        [--repeats 3] [--include-hypotheses] > report.json

``corpus.json`` lists supplied recorded or generated synthetic clips with expected transcripts::

    {"clips": [{"wav": "nl-ja.wav", "language": "nl", "expected": "ja", "kind": "command"},
               {"wav": "silence-fan.wav", "language": "auto", "expected": "", "kind": "silence"}]}

``wav`` paths are relative to the corpus file. The tool starts the same owned worker the app uses
(``homebody/asr_worker.py`` with the pinned NeMo-Speech.cpp C ABI named by the provisioning
manifest, hashes verified before load), sends samples from RAM over the private socket and writes
no audio. It reports runtime/model identity, process cold start (spawn to first ready status, plus
the worker's own load_ms), warm per-clip client round trip and worker decode_ms, peak worker RSS,
WER per language, command exact-match and silence false transcripts. Hypotheses are omitted unless
``--include-hypotheses`` is given. Results are measurements of this run only; this script never
decides acceptance. The worker is killed on exit, error or Ctrl-C.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import signal
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homebody.audio import resample_linear  # noqa: E402
from homebody.robot_stt import RobotSttClient, RobotSttUnavailable  # noqa: E402

_WORD = re.compile(r"[^\w']+", re.UNICODE)


def load_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as reader:
        if reader.getsampwidth() != 2:
            raise ValueError(f"{path.name}: only 16-bit PCM WAV fixtures are supported")
        rate, channels = reader.getframerate(), reader.getnchannels()
        pcm = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return resample_linear(pcm, rate, 16000) if rate != 16000 else pcm


def words(text: str) -> list[str]:
    return [w for w in _WORD.split(text.casefold()) if w]


def edit_distance(reference: list[str], hypothesis: list[str]) -> int:
    row = list(range(len(hypothesis) + 1))
    for i, ref in enumerate(reference, 1):
        previous, row[0] = row[0], i
        for j, hyp in enumerate(hypothesis, 1):
            previous, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, previous + (ref != hyp))
    return row[-1]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def summarize(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50_ms": round(percentile(values, 0.5), 1),
            "p95_ms": round(percentile(values, 0.95), 1), "max_ms": round(max(values), 1)}


def run(corpus_path: Path, provision: Path, *, repeats: int, include_hypotheses: bool,
        load_timeout: float, decode_timeout: float, worker_argv=None) -> dict[str, object]:
    """``worker_argv`` is for source tests with an explicit fake backend only (not a CLI option)."""
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    clips = corpus["clips"] if isinstance(corpus, dict) else []
    if not clips:
        raise SystemExit("corpus has no clips")
    loaded = []
    for clip in clips:
        if clip.get("language") not in {"en", "nl", "auto"}:
            raise SystemExit(f"{clip.get('wav')}: language must be en, nl or auto")
        loaded.append((clip, load_wav(corpus_path.parent / clip["wav"])))

    run_dir = Path(tempfile.mkdtemp(prefix="hbstt-bench-"))
    client = RobotSttClient(
        socket_path=run_dir / "worker.sock", manifest_path=provision,
        load_timeout=load_timeout, decode_timeout=decode_timeout, idle_seconds=max(60.0, decode_timeout * 4),
        worker_argv=worker_argv,
    )

    def cleanup(*_args):
        client.close()
        shutil.rmtree(run_dir, ignore_errors=True)
        raise SystemExit(130)

    previous = {sig: signal.signal(sig, cleanup) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        if not client.provisioned():
            raise SystemExit("provisioning manifest is missing, untrusted or malformed")
        started = time.monotonic()
        try:
            status = client.status()  # spawn, verify hashes, load, publish
        except RobotSttUnavailable as exc:
            raise SystemExit(f"worker did not become ready: {exc.category}") from None
        cold_ms = (time.monotonic() - started) * 1000
        results, failures = [], []
        rtt, decode = [], []
        for repeat in range(repeats):
            for index, (clip, samples) in enumerate(loaded):
                t0 = time.monotonic()
                try:
                    receipt = client.transcribe(samples, session="bench", generation=repeat, language=clip["language"],
                                                cancelled=lambda: False)
                except RobotSttUnavailable as exc:
                    failures.append({"clip": clip["wav"], "repeat": repeat, "error": exc.category})
                    continue
                elapsed = (time.monotonic() - t0) * 1000
                rtt.append(elapsed)
                decode.append(receipt.decode_ms)
                if repeat == 0:
                    results.append((index, clip, receipt.text, elapsed, receipt.decode_ms, samples.size / 16000))
        final_status = client.status()
    finally:
        client.close()
        shutil.rmtree(run_dir, ignore_errors=True)
        for sig, handler in previous.items():
            signal.signal(sig, handler)

    per_language: dict[str, dict[str, int]] = {}
    commands = command_hits = silence = silence_false = 0
    rows = []
    for _index, clip, text, elapsed, decode_ms, seconds in results:
        reference, hypothesis = words(clip.get("expected", "")), words(text)
        kind = clip.get("kind", "sentence")
        if kind == "silence":
            silence += 1
            silence_false += bool(hypothesis)
        else:
            bucket = per_language.setdefault(clip["language"], {"errors": 0, "words": 0})
            bucket["errors"] += edit_distance(reference, hypothesis)
            bucket["words"] += len(reference)
        if kind == "command":
            commands += 1
            command_hits += reference == hypothesis
        row = {"wav": clip["wav"], "language": clip["language"], "kind": kind, "audio_s": round(seconds, 3),
               "first_pass_rtt_ms": round(elapsed, 1), "decode_ms": decode_ms}
        if include_hypotheses:
            row["hypothesis"] = text
        rows.append(row)
    return {
        "identity": status.get("identity", {}),
        "accelerator_reported": status.get("accelerator"),
        "cold": {"spawn_to_ready_ms": round(cold_ms, 1), "worker_load_ms": status.get("load_ms")},
        "warm": {"client_round_trip": summarize(rtt[len(loaded):] or rtt),
                 "worker_decode": summarize([d for d in decode[len(loaded):] or decode if d >= 0])},
        "memory": {"worker_peak_rss_kb": final_status.get("rss_peak_kb")},
        "accuracy": {
            "wer": {lang: round(v["errors"] / v["words"], 4) if v["words"] else None
                    for lang, v in per_language.items()},
            "command_exact_match": f"{command_hits}/{commands}",
            "silence_false_transcripts": f"{silence_false}/{silence}",
        },
        "repeats": repeats,
        "clips": rows,
        "failures": failures,
        "note": "Single-run measurements; GPU memory, temperature and co-tenancy are not sampled by this tool.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--provision", type=Path, required=True, help="robot-stt manifest.json (pinned hashes)")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--include-hypotheses", action="store_true")
    parser.add_argument("--load-timeout", type=float, default=180.0)
    parser.add_argument("--decode-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    report = run(args.corpus.resolve(), args.provision.resolve(), repeats=max(1, args.repeats),
                 include_hypotheses=args.include_hypotheses, load_timeout=args.load_timeout,
                 decode_timeout=args.decode_timeout)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
