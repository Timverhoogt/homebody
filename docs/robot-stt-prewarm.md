# Bounded robot STT prewarming (source candidate, not deployed)

Baseline: installed 0.5.18 / source `612fea265a2d69b26d8d0b0e4be2c8624f277f07`.
No version, model, native runtime, provisioning manifest or saved provider changes.

## Admission and retention

- The existing endpointed Pipeline/Kids conversation entry requests prewarming only for an
  audio turn whose saved STT provider is `robot`. Adult Live/Realtime and typed input do not
  trigger it. It starts alongside bridge health and ordinary endpoint recording; it neither
  opens media nor captures microphone audio. This deliberately does **not** warm on boot,
  status polling, idle wake detection, power Awake, or caregiver setup alone.
- One runtime helper thread and one owned worker. The helper does not retry or synthesize
  fallback speech. A concurrent first transcription waits (cancellably, bounded by load +
  decode budgets) for admitted prewarming; competing real decodes remain refused, not queued.
- `warmup` accepts no PCM/path/language fields. The worker creates one second of zero float32
  PCM in RAM, decodes it once with the already trusted native model and discards all returned
  text. Status distinguishes `model_ready` from `inference_warm`, and records `warmup_ms`.
  A successful nonempty ordinary decode also establishes inference warmth. Further warmup
  calls are idempotent, including EN → NL → auto → NL and activity/session changes.
- Stop increments the immutable runtime generation and client cancellation epoch. Pending
  load/decode is killed and reaped without waiting on a native Python/GIL cleanup hook; an
  idle model contains no turn audio/history and may survive ordinary Stop/activity switches.
  Privacy/Meeting/Sleep releases even the idle model. Existing app shutdown permanently
  closes the client. A later deliberate admitted wake may load again, but a stale callback
  cannot resurrect a worker or accept a readiness/final result.
- Manifest shape, owner/mode and referenced file trust are rechecked before warm reuse.
  Manifest contents and manifest/library/model inode, size, mtime and ctime bind reuse to
  the loaded provisioning. Drift fails closed (`provisioning_changed`) until a new client
  lifecycle, rather than silently loading changed assets. Full weight/library SHA-256
  verification still occurs inside the owned worker before native load. Every connection
  retains private-directory/socket trust and owned PID/UID peer checks.
- No keepalive loop. Idle retention is now **120 seconds**, down from 600: a policy tradeoff
  against the measured ~1.2 GiB RSS and ~2 s cold load + warmup. It retains the likely next
  endpointed turns while releasing memory after a short unused interval. This is not an
  empirically optimized family-session inactivity threshold. Existing kernel-enforced idle,
  load (90 s) and request watchdogs remain; the client additionally kills load at 90 s and
  decode/warmup at 20 s. Warmup failures back off for 60 s rather than immediately causing a
  second native load on the first real turn. Client cancellation checks poll every 20–100 ms.
- Existing currentness-fenced, at-most-once confirmed-local host fallback applies only to
  actual utterances. Prewarming never invokes host health/transcription or TTS.

## Synthetic Jetson evidence

Executed using candidate adapters from a private SSH scratch overlay with the installed
app interpreter; no installation, app/service restart, SDK construction, camera, microphone,
speaker or motion. Pinned current NeMo-Speech.cpp C ABI / Parakeet TDT 0.6B v3 Q8_0 assets
were reused without downloads. Live tuple: Orin Nano Super developer kit, ARM64,
L4T R39.2.1, kernel 6.8.12-1021-tegra. Normal services remained running.

Three sequential model-cold cycles, each followed by complete synthetic zero-PCM warmup
and six synthetic speech decodes alternating EN/NL and activity IDs:

| Cycle | Model-ready wall ms | Synthetic warmup wall ms | Cold → inference-warm ms | First EN speech wall ms | First NL speech wall ms | Peak RSS KiB |
|---|---:|---:|---:|---:|---:|---:|
| 0 | 1181.9 | 852.2 | 2034.1 | 54.4 | 42.6 | 1275112 |
| 1 | 1121.4 | 846.9 | 1968.3 | 53.7 | 42.2 | 1273368 |
| 2 | 1202.7 | 833.5 | 2036.2 | 53.7 | 42.3 | 1273256 |

Later EN/NL speech request walls: 33.8–56.3 ms; native decode: 31–54 ms.
All six speech requests in each cycle used that cycle's single PID. All three workers were
explicitly killed, waited/reaped, and independently absent from `/proc`; isolated socket/lock
directories were removed. Production config and provisioning manifest SHA-256 matched their
immediately pre-run values afterward. The isolated manifest also remained unchanged.

These measurements move the first-inference cost into an admitted synthetic operation;
they do not remove it. Full cold readiness still costs about two seconds and only benefits
conversation latency when it overlaps other admitted work. The previous non-prewarmed probe
had ~865 ms first native inference, versus ~47–52 ms warm recognition. This is contextual
prior evidence, not a paired end-to-end comparison. The benchmark's first speech means the
first **synthetic speech** after warmup, never a real microphone turn.

Raw final receipt (operator scratch):
`/home/timv/.hermes/cache/scratch/prewarm-jetson-final.json`.
Reproducible harness: `tools/robot_stt_prewarm_benchmark.py` with explicit `--overlay`,
`--manifest`, `--corpus`, `--socket-root` and `--output`; corpus must assert `synthetic: true`.
Use an isolated private manifest/socket and the provisioned app interpreter, never production
worker endpoints. The overlay contains candidate `robot_stt.py` and `asr_worker.py` only.

## Verification and remaining gates

- Ruff and whitespace checks passed.
- Focused final Python 3.12 gate: **397 passed, zero skipped/failed/errors**, covering ASR
  subprocess protocol, cold/warm reuse, concurrent first turn, Stop during load/submitted
  warmup (including child-only `ctypes.PyDLL(...).sleep` retaining the GIL), hung deadlines,
  post-privacy reply rejection, manifest/socket drift, runtime admission/config drift,
  idle-model privacy release, existing host-fallback/currentness, power, Kids activity
  switches, greeting/cancel/End and acknowledgement/turn deadline regressions.
- JUnit: `/home/timv/.hermes/cache/scratch/prewarm-final-junit.xml`.
- Built an unpublished same-version development wheel, **not updater/release eligible**:
  `/home/timv/.hermes/cache/scratch/prewarm-dist/reachy_mini_homebody-0.5.18-py3-none-any.whl`.
  SHA-256: `f9b6d39aebe80eb8699cadf7f14d77834cc8e5defe88869dd6f577913059409b`.
  All 136 `homebody/` resources matched the source mapping; all 142 hashed RECORD entries
  were verified. This private build has no canonical `release.json`, unlike the installed
  137-resource release. Existing setuptools package-data warnings remain non-blocking;
  the resources were independently checked rather than inferred from build success.

Still required before shipping: independent review, combined-feature full release/browser
suite, canonical versioned release artifact, and separately authorized deployment. Physical
owner-supervised Pipeline/Kids first turn, language/activity switching, Stop/End/privacy,
co-tenancy with separately enabled vision, and actual audible latency remain unverified.
Adult Live remains independent and unchanged. Native Dutch short-command accuracy limitations
are unchanged; prewarming and infrastructure fallback do not correct a wrong native final.
