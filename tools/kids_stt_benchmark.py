"""Benchmark original vs revised local Kids STT on generated synthetic audio (no child audio).

Runs under a Python that has faster-whisper and cached ``base`` weights. Offline, CPU, 2 threads.
Every measurement is the wall time of the exact process Hermes would run for one turn:
- original: the pre-change command (model load per call, beam 5);
- revised_one_shot: revised decoding in ``--no-worker`` mode (isolates decoding);
- revised_worker: the revised command with its reusable model worker (one model cold start for the
  first request; every later request, including the first in the other language, is warm).
Workers are terminated in ``finally``; results go to ``.artifacts/`` only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMAND = ROOT / "companion" / "local_stt_command.py"
sys.path.insert(0, str(ROOT))

from companion.local_stt_command import worker_running  # noqa: E402

ORIGINAL = r"""
import sys
from pathlib import Path
from faster_whisper import WhisperModel
model = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=2,
                     num_workers=1, local_files_only=True)
segments, _ = model.transcribe(sys.argv[1], beam_size=5, vad_filter=True,
                               condition_on_previous_text=False, language=None)
text = " ".join(s.text.strip() for s in segments
                if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)).strip()
Path(sys.argv[2], "transcript.txt").write_text(text, encoding="utf-8")
"""


PROFILE = r"""
import json, sys, time
from faster_whisper import WhisperModel, decode_audio
t = time.perf_counter()
model = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=2,
                     num_workers=1, local_files_only=True)
out = {"model_load": round(time.perf_counter() - t, 3)}
def run(label, path, **kw):
    t = time.perf_counter()
    segments, info = model.transcribe(path, condition_on_previous_text=False, without_timestamps=True, **kw)
    text = " ".join(s.text.strip() for s in segments)
    out[label] = {"s": round(time.perf_counter() - t, 3), "text": text,
                  "language": info.language, "duration": round(info.duration, 2)}
for name, path, lang in ((n, p, l) for n, p, l in zip(("english", "dutch"), sys.argv[1:3], ("en", "nl"))):
    t = time.perf_counter(); decode_audio(path); out[name + ":decode_audio"] = round(time.perf_counter() - t, 3)
    run(name + ":warmup", path, beam_size=1, vad_filter=True)
    run(name + ":beam5_vad_auto", path, beam_size=5, vad_filter=True)
    run(name + ":beam1_vad_auto", path, beam_size=1, vad_filter=True)
    run(name + ":beam1_novad_auto", path, beam_size=1, vad_filter=False)
    run(name + ":beam1_vad_lang", path, beam_size=1, vad_filter=True, language=lang)
    run(name + ":beam5_vad_lang", path, beam_size=5, vad_filter=True, language=lang)
print(json.dumps(out))
"""


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "OMP_NUM_THREADS": "2",
            "HOMEBODY_STT_WORKER": "1",
        }
    )
    return env


def _run(argv: list[str], output_dir: Path, deadline: float) -> tuple[float, str]:
    remaining = deadline - time.monotonic()
    if remaining <= 5:
        raise TimeoutError("benchmark deadline reached")
    transcript = output_dir / "transcript.txt"
    transcript.unlink(missing_ok=True)
    started = time.perf_counter()
    subprocess.run(
        argv,
        env=_env(),
        check=True,
        timeout=min(60.0, remaining),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    elapsed = time.perf_counter() - started
    return elapsed, transcript.read_text(encoding="utf-8")


def _stop_worker(socket_path: Path) -> None:
    pid = worker_running(socket_path)
    if pid <= 0:
        return
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not worker_running(socket_path):
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio-dir", default=str(ROOT / ".artifacts" / "synthetic-stt"))
    parser.add_argument("--output", default=str(ROOT / ".artifacts" / "kids-stt-benchmark.json"))
    parser.add_argument("--warm-repeats", type=int, default=1)
    parser.add_argument("--deadline-seconds", type=float, default=140.0)
    parser.add_argument("--no-profile", dest="profile", action="store_false")
    args = parser.parse_args()
    deadline = time.monotonic() + args.deadline_seconds
    files = {name: Path(args.audio_dir, f"{name}.wav") for name in ("english", "dutch")}
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        print(json.dumps({"blocked": "missing synthetic audio", "paths": missing}))
        return 2
    repeats = max(1, min(args.warm_repeats, 3))
    results: dict[str, object] = {"threads": 2, "model": "base int8 cpu", "runs": []}
    runs: list[dict[str, object]] = results["runs"]  # type: ignore[assignment]
    with tempfile.TemporaryDirectory(prefix="kids-stt-bench-") as scratch:
        out = Path(scratch)
        socket_path = out / "worker" / "worker.sock"
        # The bridge names Kids uploads homebody-stt-<profile language>-*; mirror that here.
        hinted = {}
        for (name, wav), language in zip(files.items(), ("en", "nl"), strict=True):
            hinted[name] = out / f"homebody-stt-{language}-bench.wav"
            shutil.copyfile(wav, hinted[name])
        try:
            if args.profile:
                started = time.perf_counter()
                completed = subprocess.run(
                    [sys.executable, "-c", PROFILE, str(files["english"]), str(files["dutch"])],
                    env=_env(),
                    check=True,
                    timeout=max(5.0, deadline - time.monotonic() - 40),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                )
                results["profile"] = json.loads(completed.stdout.decode().strip().splitlines()[-1])
                results["profile"]["process_total"] = round(time.perf_counter() - started, 3)
            for name, wav in files.items():
                for index in range(1 + repeats):
                    seconds, text = _run([sys.executable, "-c", ORIGINAL, str(wav), str(out)], out, deadline)
                    runs.append(
                        {
                            "approach": "original",
                            "file": name,
                            "call": index,
                            "seconds": round(seconds, 3),
                            "text": text,
                        }
                    )
                seconds, text = _run(
                    [
                        sys.executable,
                        str(COMMAND),
                        "--input",
                        str(hinted[name]),
                        "--output-dir",
                        str(out),
                        "--no-worker",
                    ],
                    out,
                    deadline,
                )
                runs.append(
                    {
                        "approach": "revised_one_shot",
                        "file": name,
                        "call": 0,
                        "seconds": round(seconds, 3),
                        "text": text,
                    }
                )
            worker_argv = [
                sys.executable,
                str(COMMAND),
                "--output-dir",
                str(out),
                "--socket",
                str(socket_path),
                "--idle-seconds",
                "30",
            ]
            for index in range(1 + repeats):
                for name in files:
                    seconds, text = _run([*worker_argv, "--input", str(hinted[name])], out, deadline)
                    runs.append(
                        {
                            "approach": "revised_worker",
                            "file": name,
                            "call": index,
                            "seconds": round(seconds, 3),
                            "text": text,
                        }
                    )
        except subprocess.CalledProcessError as exc:
            results["blocked"] = f"command failed: {exc.stderr.decode(errors='replace')[-600:]}"
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            results["blocked"] = f"deadline: {type(exc).__name__}"
        finally:
            _stop_worker(socket_path)
            results["worker_left_running"] = bool(worker_running(socket_path))

    def median(values: list[float]) -> float | None:
        return sorted(values)[len(values) // 2] if values else None

    def summary(approach: str) -> dict[str, object]:
        rows = [row for row in runs if row["approach"] == approach]
        texts = {row["file"]: row["text"] for row in rows}
        if approach != "revised_worker":
            # Every call is a separate process that loads the model: each one is a model cold start.
            first = [row["seconds"] for row in rows if row["call"] == 0]
            repeat = [row["seconds"] for row in rows if row["call"] != 0]
            return {
                "model_load_per_call": True,
                "first_call_seconds": first,
                "repeat_call_median_seconds": median(repeat),
                "texts": texts,
            }
        # One shared worker: only the very first request pays the model cold start. The first
        # request in another language reuses the already loaded model, so it is warm.
        cold = rows[:1]
        warm_rows = rows[1:]
        return {
            "model_load_per_call": False,
            "model_cold_start_seconds": cold[0]["seconds"] if cold else None,
            "model_cold_start_file": cold[0]["file"] if cold else None,
            "warm_first_use_other_language_seconds": {
                row["file"]: row["seconds"] for row in warm_rows if row["call"] == 0
            },
            "warm_median_seconds": median([row["seconds"] for row in warm_rows]),
            "texts": texts,
        }

    results["summary"] = {name: summary(name) for name in ("original", "revised_one_shot", "revised_worker")}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 1 if "blocked" in results else 0


if __name__ == "__main__":
    raise SystemExit(main())
