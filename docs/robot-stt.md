# On-robot speech recognition (Stage A)

**Status: available in 0.5.14, off by default; per-robot activation is separate from publication.**

* A native ARM64/CUDA build of NeMo-Speech.cpp at the pinned revision has been loaded on one
  Reachy (Jetson) through this worker. Model: Parakeet TDT 0.6b v3 q8_0, sha256
  `e3880d0aaaaf2c308ea2c35016b2b895c423eb3fda924c1b463d1c19b7f4d32e`. C library sha256
  `609f164087697e90cc01fdcccf2360859869616dd108b3173d7d4ab1fdf1920f`.
* Follow-up synthetic benchmarks measured a real GPU peak of 99%, board memory peak of
  4,436 MB out of 7,485 MB with normal services running, and a maximum temperature of 52.625°C.
  No local vision model was newly loaded, so this is not a worst-case VLM co-tenancy guarantee.
* A harsh eSpeak corpus (24 clips) produced only 3/10 exact command matches and Dutch WER 1.0,
  despite no false transcripts in 4 silence/noise clips. Keep this stress failure visible.
* A separate natural synthetic Flash v2.5 corpus (21 clips) produced 8/10 exact command matches,
  English WER 0 and Dutch WER 0.2857, with no false transcripts in 3 silence clips. Stop and Help
  were exact in both languages. Dutch `een hint` became `and hint`; `iets anders` became
  `it's anders`. Local infrastructure fallback does not repair every misheard word.
* That natural run measured 1.088 s cold worker-ready time, 36.6 ms warm round-trip median,
  62.9 ms p95 and 1.54 GB peak worker RSS. These are bounded synthetic completed-utterance
  worker measurements, not real-room, child, streaming-first-word or speaker measurements.
* The requested robot's owner approved activation as a **trial** with local host fallback after
  these accuracy gaps were disclosed. This does not mean every accuracy gate below passed;
  physical/adult/child acceptance remains open. Other installations remain off by default.
* None of these figures is an end-to-end speaker latency. Endpointing silence, the reply model and
  TTS still come after recognition.
* Stage B (streaming during recording) is not implemented. It depends on measured Stage A results.

`stt_provider: "robot"` recognizes Pipeline and Kids turns on Reachy itself. It is off by default
(`AppConfig.stt_provider` stays `"configured"`, `stt_robot_fallback` stays `"none"`). It stays
unusable until an owner provisions the pinned native runtime and model below. It does not change
the saved adult conversation engine: Live and Realtime keep their own speech recognition.

## What runs where

* Homebody stays the only microphone reader. At the endpoint it sends the 16 kHz float32 samples
  it already holds in RAM to one owned worker process (`homebody/asr_worker.py`). Nothing is
  uploaded, and no WAV, audio or transcript file is written or logged.
* The worker uses only the standard library and is started as `python -I asr_worker.py --serve …`.
  Its environment is scrubbed to `PATH`, `LANG` and `PYTHONNOUSERSITE`, plus `LD_LIBRARY_PATH` and
  `CUDA_VISIBLE_DEVICES` if they are already set. It has no credentials, tools or media client.
  A Python-level guard refuses non-Unix sockets. That guard does not cover native code.
* Backend: the NVIDIA NeMo-Speech.cpp stable C ABI (`include/nemo_speech/asr.h` at
  `083861c241061562b8ec29eb06d7f6cdc3af8775`), loaded through `ctypes`. The adapter calls only
  these functions: `nemo_speech_asr_recognition_options_default`, `nemo_speech_asr_create`,
  `nemo_speech_asr_recognize_f32` (sample rate 16000), `nemo_speech_asr_result_alternative_count`,
  `nemo_speech_asr_result_transcript`, `nemo_speech_asr_result_destroy` and
  `nemo_speech_asr_version`. The config structs carry `size = sizeof(struct)`. Subsystems other
  than backend and model are NULL, which means library defaults. `backend.gpu = -1` is reported
  as `accelerator: "cpu"` and is never labelled GPU.
* `language_code` is sent as `en` or `nl` for Kids turns (the parent-selected profile language)
  and as NULL (auto) for adult turns. The header calls this "prompt selection". Whether Parakeet
  TDT v3 actually honours a fixed language has not been verified on the device.

## Provisioning (owner, on the robot; not shipped in the wheel)

The generic Homebody wheel ships the Python adapter only. Build or obtain the native ARM64/CUDA
library and the model separately. Then write a manifest to the fixed path
`<Homebody config dir>/robot-stt/manifest.json` (next to `config.json`). No API or settings field
can choose this path, an executable, a library, a model or an audio path.

```json
{
  "schema": "homebody-robot-stt/1",
  "backend": "nemo_speech_c_abi",
  "runtime_revision": "083861c241061562b8ec29eb06d7f6cdc3af8775",
  "library": {"path": "/abs/path/libnemo_speech_asr.so", "sha256": "<64 hex>"},
  "model": {"path": "/abs/path/model.gguf", "sha256": "<64 hex>", "name": "parakeet-tdt-0.6b-v3"},
  "gpu": 0
}
```

The worker refuses to load if any of these checks fails:

* the manifest has exactly these keys;
* the manifest, library and model are regular files (not symlinks), owned by root or the app
  user, and not group- or world-writable;
* `runtime_revision` equals the pinned revision;
* both sha256 values match. The hashes are recomputed on every worker load, inside the load
  deadline.

If any check fails, the worker never loads and the turn reports an infrastructure failure.
The settings page's "provisioned" flag only means the manifest passed the cheap shape and
ownership check. It never means recognition has run.

To check the artifacts by hand: `sha256sum <library> <model>`, `stat -c '%U %a %F' <file>`.

## Protocol v1 (private Unix socket)

* Socket: `$XDG_RUNTIME_DIR/homebody-robot-stt-<uid>/worker.sock` (falls back to the temp dir).
  Before the client connects, it checks that the directory is 0700 and owned by the user, and that
  the socket is a 0600 socket owned by the user (not a symlink). After connecting, it checks
  `SO_PEERCRED`: the pid must be the client's own spawned worker and the uid must be the user's.
  If any check fails, no bytes are sent.
* Request: one JSON header line of at most 1024 bytes with an exact key set, then
  `samples × 4` bytes of float32 LE:
  `{"v":1,"op":"recognize","session":"<[A-Za-z0-9_-]{0,64}>","turn":N,"generation":N,"language":"en|nl|auto","rate":16000,"samples":N}`.
  The maximum is 120 s × 16 kHz. `{"v":1,"op":"status"}` returns `load_ms`, `rss_peak_kb`,
  `accelerator` and the runtime/model identity.
* Reply: one JSON line that echoes `session`/`turn`/`generation`, with `status: ok`, `text`,
  `processing: robot`, `accelerator` and `decode_ms`. On failure it has a categorical `error` only.
  The client rejects any reply whose echo does not match exactly, then kills the worker.
* Deadlines:
  * Client: load (90 s), decode (20 s) and Stop. On any of these the client SIGKILLs the owned
    worker and reaps it on a daemon thread, so it never blocks Stop.
  * Worker: a kernel SIGALRM watchdog caps load, decode and idle time (idle default 600 s), even
    inside native code.
  * After a failed load there is a 60 s backoff, so the app never reloads repeatedly.
* One turn is admitted at a time. A second concurrent request gets `busy` and is never queued.

## Turn routing

The shared helper is `homebody/stt_routing.py`, used by `voice_pipeline.py` and `proactive.py`.

* Every request is bound to an immutable snapshot of the turn stop generation and the Kids
  session id, and is also checked against privacy, power and the runtime stop.
  * A game-master switch (`_kids_host_transition`) rotates the Kids session id and advances the
    stop generation. The outer play id and the play deadline stay the same. Either change fences
    a decode that started in the previous game.
  * If ownership is lost during the request, it is cancelled and the worker is killed.
  * A final that arrives after Stop, End or a game switch is discarded. It never reaches the host
    command parser or the model.
  * A failure that arrives after ownership was lost is also discarded. It shows no error notice
    or earcon in the new game.
  * A cancelled turn never falls back.
  * A child Stop during a decode still ends the outer session.
* An infrastructure failure, such as an unprovisioned model, a load or decode timeout, a crash or a
  stale reply, is not treated as silence. With `stt_robot_fallback: "none"` (the default), the turn
  ends with "Speech recognition on Reachy is unavailable" (or a setup notice when unprovisioned)
  and an error earcon. It does not enter the Kids empty-turn retry. An empty robot transcript is
  ordinary silence and keeps the bounded Kids re-listen.
* With `stt_robot_fallback: "host"`, the fallback runs at most once per turn, and only if the
  host's own health reports `stt_locality == "local"`. It sends the same in-RAM samples to the
  bridge with `provider=local`. That dedicated request calls Hermes's `transcribe_audio_local_fallback`,
  never the mutable configured-provider dispatcher; it fails closed if that local-only API is
  absent, fails, or returns an unconfirmed/non-local receipt. The robot route never uses a
  configured cloud provider. Late host health/decode errors are cancelled before error reporting.
* The cached Kids acknowledgement filler keeps working while a robot decode is pending. It is
  fenced by the same turn generation and session.
* Status:
  * `stt_processing_location` is `robot`, `host`, `cloud` or `unknown`.
  * `stt_processing_verified` is false while the request is pending and true only after this
    turn's receipt.
  * Both reset on errors, provider changes and conversation end.
  * Child-locked status passes only these allowlisted enums.
  * The UI shows "Transcribing on Reachy" or "Transcribing on Hermes host" from that per-turn
    value, never from the saved selection.
* Timing marks:
  * `speech_last_voiced`: the last voiced frame.
  * `endpoint_final` and `local_speech_end`: when the endpoint returned. `local_speech_end` is
    unchanged.
  * `stt_submitted` and `stt_final`.
  * `tools/voice_timing_report.py` reports within-turn spans.
  * None of these marks proves when audio was audible.

## Native ABI expectation for the provisioned library

* The library must be a shared object built from NeMo-Speech.cpp at the pinned revision with the
  CUDA backend.
* It must export the eight symbols that `bind_nemo_abi` declares: the seven listed above plus
  `nemo_speech_asr_destroy`. The adapter never calls `destroy`; the recognizer lives until the
  process exits. The C struct layouts are mirrored in `asr_worker.py` and pinned by
  `tests/test_asr_worker.py`.
* Check the exports with:
  `nm -D --defined-only <lib> | grep -E 'nemo_speech_asr_(create|destroy|recognize_f32|recognition_options_default|result_(alternative_count|transcript|destroy)|version)$'`.
* The worker passes `gpu` from the manifest straight to `nemo_speech_asr_backend_config.gpu`.
  * With `gpu: -1` the receipt says `cpu`.
  * With `gpu >= 0` the receipt says `gpu`, but that only records the requested device. Actual
    GPU execution must be confirmed on the device, for example with `tegrastats` during the
    benchmark.

## Device benchmark (parent-run)

```
python3 tools/robot_stt_benchmark.py --corpus <corpus.json> --provision <manifest.json> --repeats 3 > report.json
```

The benchmark uses the same worker and protocol as the app, with supplied synthetic WAV fixtures
and their expected transcripts. It reports:

* runtime/model identity;
* cold spawn-to-ready time and the worker's `load_ms`;
* warm round-trip and `decode_ms` p50/p95;
* the worker's peak RSS;
* WER per language, command exact-match and silence false transcripts.

It does not sample GPU memory, temperature or co-tenancy. The plan's targets are proposals until
a device receipt exists.

### Exact parent steps (scratch first, no app or service change)

Run from a copied source tree on the device. Use synthetic WAVs only, never the microphone.

```sh
# 1. Check artifact identity. The candidate Parakeet TDT v3 q8_0 model sha256 must equal
#    e3880d0aaaaf2c308ea2c35016b2b895c423eb3fda924c1b463d1c19b7f4d32e
sha256sum "$LIB" "$MODEL"
stat -c '%U %a %F %n' "$LIB" "$MODEL"     # owner root/app user, no group/other write
nm -D --defined-only "$LIB" | grep -c 'nemo_speech_asr_'

# 2. Write a scratch manifest (mode 0644 or stricter, same shape as above), for example
#    $SCRATCH/robot-stt/manifest.json with "gpu": 0 and both sha256 values.

# 3. Benchmark through the real worker protocol (requires numpy; no other dependency).
python3 tools/robot_stt_benchmark.py --corpus "$SCRATCH/corpus/corpus.json" \
    --provision "$SCRATCH/robot-stt/manifest.json" --repeats 3 > "$SCRATCH/robot-stt-report.json"
```

The app reads only `~/.local/share/homebody/robot-stt/manifest.json`, or the same `robot-stt/`
directory next to an overridden config path. Do not create that file until the owner approves
enabling robot STT.

### Acceptance gates

Passed, with limited evidence:

1. The native build at the pinned revision loads through `ctypes` on one Jetson, and the worker
   served an EN and an NL synthetic clip exactly (figures above).
2. Source integration with the current game master. A switch, End, child Stop, privacy or power
   change fences robot finals, failures, the host fallback and the acknowledgement filler. The
   outer play id and deadline never hold STT state. This is covered by
   `tests/test_robot_stt_game_master.py` with a fake recognizer, so it is not device proof.

Still open (source tests do not pass these):

3. Accuracy on the expanded held-out EN/NL synthetic corpus: WER against the host `base` baseline,
   100% command exact-match, and 0 silence false transcripts.
4. GPU use confirmed outside this tool (`tegrastats`), and behaviour with vision and normal
   services running: memory headroom, no swap, temperature.
5. Whether Parakeet v3 honours `language_code` `en`/`nl`.
6. Owner approval to provision the app manifest and select `robot` on the requested robot only.
7. A supervised adult microphone test, then a separately supervised child test.
