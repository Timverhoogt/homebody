# Model stack, speech routing and efficiency

This page explains Homebody's speech, language, vision and local detection paths, where they run, and where efficiency improvements are worth investigating.

**Deployment snapshot:** 16 October 2026, Homebody **0.5.22**, commit `c289f7f592df155389b84c292287a1e8736ba7ea`, on the reference Reachy Mini Lite + Jetson setup with a separate Hermes host. Model names and selected settings below describe that installation, **not universal defaults or requirements for every Homebody installation**. Recheck deployed configuration before relying on this snapshot.

**Status:** documentation and recommendations only. Proposed optimisations are not implemented or approved by being listed here. Source tests, production deployment and supervised microphone/speaker acceptance are separate gates.

## The important distinction: purpose versus transport

Everyday, Workspace and Kids determine what the assistant is allowed to do. Pipeline, Live and Realtime describe how adult voice conversation is transported.

- **Everyday:** narrow household/time/weather/general assistance, without private project authority.
- **Workspace:** project discussion and approved work through native Hermes sessions and their actual permissions, approvals and receipts.
- **Kids:** a dedicated moderated, tool-free child route with caregiver-selected language, activity and duration.
- **Pipeline:** local endpointing, selected recognition, a text backend and separate speech synthesis.
- **Live / Realtime:** cloud audio models that already handle input audio and generate speech; they can delegate requests to the appropriate backend.

These models are **not all invoked for every turn**. In particular:

1. Adult Live does not first run Parakeet or Whisper for the same ordinary input.
2. Kids explicitly uses Pipeline even when the saved adult engine is Live.
3. `tts_provider=configured` inherits the Hermes host's speech provider. A neighbouring saved TTS model/voice field does not override that provider.
4. Local Camera Ask and Kids I Spy are separate vision paths.
5. A downloaded model is not necessarily selected, loaded or used.
6. A saved purpose-mode label, current capability profile and verified native session binding are different facts. Never infer execution authority from the label alone.

## Model and processing inventory

| Job | Reference selection | Placement | Routing / status |
|---|---|---|---|
| Wake phrases | Sherpa-ONNX Zipformer GigaSpeech **3.3M**, quantized ONNX | Robot | Bundled Homebody/Hermes/Nabu/Reachy wake phrases; lifecycle/privacy-gated. |
| Pipeline end-of-utterance detection | Adaptive RMS and noise-floor logic | Robot | Deterministic, not an LLM or neural VAD; saved end-silence **0.8 s**. |
| Primary Pipeline/Kids STT | **Parakeet TDT 0.6B v3**, Q8 GGUF, pinned NeMo-Speech.cpp C ABI | Jetson | Selected `robot` provider; model identity comes from its provisioning manifest, not the generic saved `stt_model=base` field. |
| STT infrastructure fallback | **Faster-Whisper base**, CPU int8, two threads, beam 5 | Hermes host | One explicit confirmed-local replay of still-current audio; resident private worker. No silent cloud fallback. |
| Installed alternative ASR | **Nemotron 3.5 ASR Streaming 0.6B**, Q8 GGUF | Jetson | Installed, **not the active recognizer**. The current adapter submits whole endpointed utterances; the model name alone does not establish incremental recognition. |
| Selected adult audio engine | **gpt-live-1**, voice **cedar** | OpenAI cloud audio API | Separate from subscription-funded text inference. Delegation does not itself prove native execution or a verified result. |
| Alternative adult audio engine | **gpt-realtime-2.1**, voice **marin**, reasoning `low`, semantic eagerness `auto` | OpenAI cloud audio API | Not selected in this snapshot. Bridge also advertises `gpt-realtime-2.1-mini`; Realtime requests `gpt-realtime-whisper` transcription, not local Pipeline STT. |
| Everyday text reasoning | **gpt-6-sol**, `openai-codex`, reasoning **none** | OpenAI cloud via ChatGPT subscription | Narrow tool loop; existing conservative standalone read shortcuts can avoid the model. |
| Restricted Agent/broker reasoning | **gpt-6-sol**, reasoning **none** | ChatGPT subscription | Bounded tool/roadmap path, not automatically a shared native Desktop session. |
| Native Workspace | **gpt-6-sol**, reasoning **none**, subscription-only runtime lock | Hermes host orchestration and OpenAI cloud inference | Real session/run/history/approval contracts. Do not describe frontend speech or a successful Bind as full end-to-end acceptance. |
| Legacy Reachy API alias | Alias `reachy`; profile default **gpt-6-astra**, reasoning setting `medium`; saved fallback Nous **z-ai/glm-5.2** | Hermes API/gateway | Distinct from the subscription-locked bridge/native routes. Alias availability does not prove it handles every turn. |
| Kids dialogue | **gpt-6-luna**, reasoning **none** | ChatGPT subscription | Isolated child route with bounded state and screened input/output. |
| Kids I Spy vision | **gpt-6-luna** | Cloud subscription inference | No local I Spy vision URL selected. Camera consent, candidate validation and target moderation still apply. |
| I Spy synonym judge and player guesses | **gpt-6-luna** | Cloud subscription inference | Model handles bounded semantic judgement; counters, hints, give-up and other game-state transitions are deterministic. |
| Kids safety screening | **omni-moderation-latest** | OpenAI moderation API | Input/output, fixed-speech and target screening as applicable. Separate from the text subscription. |
| Kids generated/fixed speech | **eleven_flash_v2_5**, language-specific voices | ElevenLabs cloud | Approved 24 kHz PCM streaming; exact text/session/language ownership is retained. |
| Kids waiting phrases | Packaged, voice/model/digest-bound audio | Local playback | Already cached; not synthesized each turn. Missing/mismatched proofs remain silent. |
| Ordinary Pipeline speech and announcements | `configured` → **Edge TTS**, **en-US-AriaNeural** | Microsoft's online Edge speech service | Returns a complete audio file. Does not control Live, Realtime or selected Kids voices. |
| Optional ordinary ElevenLabs speech | **eleven_flash_v2_5** or **eleven_multilingual_v2** | ElevenLabs cloud | Available alternative. Ordinary synthesis currently buffers a complete MP3; changing the model alone does not provide PCM streaming. |
| Local Camera Ask | **qwen3-vl:2b-instruct**, Q4_K_M, Ollama loopback endpoint | Jetson GPU on demand | Selected local vision path. Client limits: 300 generated tokens, 1,200 returned characters, 60 s timeout. |
| Face tracking | SDK-owned **YuNet** ONNX detector | Robot | Configured on, but successful initialization and current privacy/lifecycle admission must be checked separately. |
| Gesture recognition | **HaGRID** ONNX hand detector and crop classifier | Robot | Configured off in this snapshot. |
| Presence and initiative | HA occupancy, policy, cooldowns and budgets | Local | No generative model for deciding that occupancy changed. |
| Shared physical-context trigger | Small grayscale frame comparisons and stability gate | Robot | Nonsemantic change detection, **not object identification or OCR**. Semantic looking is a separate operation. |

### Kids voice selections

All selected language voices use `eleven_flash_v2_5`. Preserve the language-specific selections rather than replacing them with one generic voice:

| Language | Voice | ID |
|---|---|---|
| Dutch, Netherlands | Ruth | `yO6w2xlECAQRFP6pX7Hw` |
| Spanish, Spain | Eva Dorado | `RgXx32WYOGrd7gFNifSf` |
| German, Germany | Annika | `ViKqgJNeCiWZlYgHiAOO` |
| French, France | Gaëlle | `tKaoyJLW05zqV0tIH9FD` |
| Italian, Italy | Giulia | `CnVVMwhKmKZ6hKBAkL6Y` |
| Portuguese, Europe | Joana | `nJ5NFqyKb8kn9JBPmo6i` |

English has a separate bridge voice proof. With no general Kids voice override, the inspected bridge defaults to `cgSgspJ2msm6clMCkdW9`. Ordinary speech's saved app voice was `pNInz6obpgDQGcFmaJgB`; that is not evidence that Kids or Live uses it.

Voice configuration is not acoustic acceptance. Trace introduction, game-master prompts, generated replies, waiting clips and fallback speech separately when investigating mixed voices.

## What the measured timings establish

A bounded read-only daemon-journal query found **15 native STT completion receipts**:

| Stage | Median | Range |
|---|---:|---:|
| Native decoder | **68 ms** | 58–94 ms |
| Recognition client round trip | **71.6 ms** | 61.3–96.7 ms |

These are actual logged requests, not a controlled A/B benchmark. Their utterance lengths and transcription accuracy are not exposed by the timing-only receipts. They exclude model startup/prewarming, last-voiced-to-endpoint delay, model/tool work, moderation, synthesis and playback.

Pipeline's saved **0.8 s** end-silence is therefore a more obvious latency floor than these warm decoding samples. Test adult and child pauses separately before reducing it. Faster interruption of thoughtful pauses is not necessarily better conversation.

The existing host Whisper worker already retains its model and receives caregiver-selected language hints. The Kids waiting-clip mechanism is already cached. Neither should be presented as an unimplemented optimisation.

The subscription adapter waits for a complete model result. Ordinary configured speech also waits for a completed audio file. These are separate sequential waits, even when the upstream service internally streams. Kids speech streams **approved** PCM after safe text is available; never stream unscreened child output merely to improve a latency number.

## Edge TTS: online service and recovery boundaries

[edge-tts](https://github.com/rany2/edge-tts) accesses Microsoft's **online Edge speech service**. It is not local Jetson inference despite the word “Edge”.

The reviewed announcement incident failed with an upstream WebSocket **403**. Subsequent isolated recovery work produced a successful Hermes synthesis receipt in **1.603 s**; inspection confirmed a **3.528 s**, **21,168-byte** MP3.

That establishes a real candidate synthesis artifact. It does **not** establish first-audio latency, audible quality or repaired production announcement routing. A healthy Kids ElevenLabs stream does not prove the ordinary Edge route works. Conversely, the earlier 403 does not prove Edge can never work.

Complete and verify the existing recovery before choosing whether to retain Edge as the budget route or switch ordinary speech to an explicitly chosen alternative. Changing a global Hermes TTS setting may affect other surfaces and needs its own scope approval.

## Local models: storage is not residency

At the resource snapshot, Ollama reported **no resident models**. The native ASR worker is separate from Ollama, so this does not mean there is no local inference elsewhere.

The installation had **18 Ollama models**, with listed sizes totaling **46.60 GiB**. This is not guaranteed reclaimable disk space because models can share blobs:

```text
qwen3-vl:2b-instruct      qwen3-vl:2b
qwen3:4b                 qwen3.5:4b
gemma3:4b                llama3.1:8b
deepseek-r1:7b           qwen2.5-coder:7b
qwen3:8b                 gemma3:1b
deepseek-r1:1.5b         qwen2.5-coder:3b
translategemma:4b        qwen3-embedding:0.6b
llama3.2:3b              qwen3:0.6b
gemma4:e2b-it-qat        nemotron-3-nano:4b
```

Only Qwen VL **Instruct** is selected for local Camera Ask. The bare `qwen3-vl:2b` is a thinking-capable alternative, not an equivalent selection.

Deleting inactive models is storage housekeeping, not a fix for latency on a route that never loads them. Review dependencies, experiments and rollback needs before deletion. Keep local vision on demand: larger resident multimodal models compete with robotics and ASR for the Jetson's shared memory, and this snapshot had no swap.

## Other saved providers and auxiliary models

The host speech configuration also contains these presets. Their presence does **not** prove installation, credentials, weights, active selection or actual use:

- STT: OpenAI `whisper-1`, Mistral `voxtral-mini-latest`, ElevenLabs `scribe_v2`.
- TTS: OpenAI `gpt-4o-mini-tts`, Gemini `gemini-2.5-flash-preview-tts`, xAI voice Eve, Mistral `voxtral-mini-tts-2603`, local `neuphonic/neutts-air-q4-gguf`, Piper `en_US-lessac-medium`.

The reference owner's main Hermes used `gpt-6.1-sol`, distinct from the dedicated Workspace and legacy Reachy profile. Older saved default-profile routes included Gemini 3.5 Flash, Gemini 3.1 Flash Lite and OpenRouter GPT-5.6-Luna aliases; the inspected Reachy model-list endpoint advertised only `reachy`.

Hermes auxiliary purposes such as vision, extraction, compression, titles and approvals were configured `auto` with empty model overrides; memory used Honcho. Exact auxiliary/remote-memory models cannot be truthfully inferred from those fields. They are possible on-demand native-agent work, not proven additional resident robot models. The restricted Everyday/Kids subscription path is not the complete Hermes auxiliary loop.

## Efficiency priorities — recommendations, not shipped changes

1. **Make ordinary speech routing explicit.** Show the actual inherited provider and deliberately select the route for Pipeline replies, announcements and contextual offers. Preserve Live/Realtime voices and Kids language voices. Verify the Edge recovery rather than duplicating it.
2. **One authoritative reasoner per purpose.** Everyday uses direct authorized handling or one narrow model loop. Workspace uses the native Hermes session, not a broker planning pass followed by another independent native planning pass. Kids retains its isolated route. A fluent frontend must not invent execution results.
3. **Keep Parakeet plus one confirmed-local fallback.** Warm decoding is already fast. Evaluate Nemotron against Dutch short commands, Stop handling and actual incremental integration before considering a switch. Do not add cloud STT to solve a reply/TTS bottleneck.
4. **Remove unnecessary calls conservatively.** Investigate an exact normalized target-match path for I Spy after input screening and current-target validation; retain the model for ambiguous synonyms. Extend voice-bound caching to suitable fixed copy without caching arbitrary dynamic replies or weakening approvals.
5. **Measure the complete useful-answer path.** Separate last voiced sample, endpoint completion, recognizer submission/final, native admission, useful verified result, approved first PCM and SDK enqueue. Enqueue is not proof of audible playback. Do not use a waiting phrase as the useful-answer metric.
6. **Benchmark transport, not just model brands.** Compare local recognition → native Hermes → streaming speech against Live frontend → native Hermes → Live speech using matched bounded fixtures. Full-duplex benefits may justify an extra audio-model layer, but need evidence. Do not silently migrate the saved engine.
7. **Keep vision disclosures precise.** Presence/stability detection is cheap and nonsemantic. Camera Ask is local Qwen; this installation's Kids I Spy vision is cloud-based. Local STT or one local vision button does not make the whole app local.
8. **Treat model cleanup as optional housekeeping.** Do not delete selected models, experimental assets or rollback artifacts merely to reduce an inventory count.

Subscription inference consumes subscription quota. Direct audio APIs and ElevenLabs remain separate usage categories. Exact financial savings require actual route-specific usage and billing receipts; this audit does not invent a cost forecast.

## Source map and maintaining this page

Source paths below refer to the **audited deployed package and host bridge**, not a guarantee that every checkout or the local `main` branch contains those modules. The reference deployment can include released work not yet integrated into that checkout. Host bridge resources are installed separately from robot resources:

- `homebody/wakeword.py`: local keyword spotting.
- `homebody/audio.py`: deterministic endpoint recorder.
- `homebody/asr_worker.py`, `robot_stt.py`, `stt_routing.py`: manifest-backed native recognition, receipts, currentness and local-only fallback.
- `homebody/voice_pipeline.py`, `voice_live.py`, `voice_realtime.py`: engine-specific flow.
- `homebody/kids_runtime.py`, `announcements.py`, `hermes_client.py`: Kids engine override, speech production and ordinary full-file synthesis.
- `homebody/local_vision.py`, `vision_runtime.py`, `face_model.py`, `gesture_detection.py`: local vision and detectors.
- `homebody/proactive.py`, `presentation.py`: nonsemantic presence/visual-change policies.
- `companion/hermes_reachy_bridge.py`, `llm_providers.py`, `subscription_reasoning.py`, `subscription_workspace_entry.py`: host reasoning, moderation, provider selection and native listener.
- `homebody/voice_timing.py`: text-free, bounded, RAM-only latency marks.

A robot wheel and a host bridge are separate deployments. Inspect both actual source locations and effective process configuration when refreshing this page. Record the observation date/release, distinguish defaults from deployment choices, and preserve the distinction between installed, selected, exercised and acoustically accepted.

Do not put credentials, private transcripts, child recordings or room images into documentation or diagnostic evidence. A documentation refresh does not authorize changing models, permissions, pairing, camera/microphone use or robot power.
