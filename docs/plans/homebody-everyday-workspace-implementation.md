# Homebody Everyday/Workspace — implementation checklist

Branch `feat/live-voice-settings`, baseline `1305d16`. States: **implemented** (source),
**tested** (local fixtures/fakes only), **remaining**. Provider access, deployment and physical
acceptance are separate states and none of them is claimed here.

| Item | State | Evidence / note |
|---|---|---|
| Config: `assistant_mode`, migration notice, Live fields, eagerness; saved engine preserved; unknown Realtime model preserved | implemented, tested | `tests/test_live_voice.py`, browser settings test |
| Public status has no location/secrets | implemented, tested | location is host-only; `redacted_dict` checked |
| Everyday tools (HA status/on-off via existing allowlists, exact aliases, ambiguity, refused domains, verification, undo, budgets, at-most-once, Kids/generation, timeouts) | implemented, tested | `tests/test_everyday_tools.py` |
| Weather: HA entity first, optional Open-Meteo only with explicit lat/lon + disclosure; freshness; unavailable | implemented, tested | same |
| Time from host clock | implemented, tested | same |
| Everyday text backend; OpenRouter preset; host-only secrets; default `gpt-4.1-mini` | implemented, tested | fake chat server payload test |
| Pipeline Everyday → `/v1/everyday/turn` | implemented, tested | lifecycle/workspace tests, bridge route test |
| Realtime Everyday: no `ask_hermes`, host tool execution, eagerness | implemented, tested | fake OpenAI WS in `tests/test_everyday_workspace_bridge.py` |
| GPT-Live relay (start shape, started wait, transcript assembly/corrections/late delivery, clarification, revisions, budget refusal, ≤480 B UTF-8, odd PCM, filtering, bounded close, Stop/late discard, boundary) | implemented, tested | `tests/test_live_transport.py` |
| GPT-Live robot client/loop (gated audio, playback-owned audible state, bounded queue, Stop flush before close) | implemented, tested | `tests/test_live_voice.py` |
| Workspace native discussion in bound Hermes session with session/run identity; broker answers labelled | implemented, tested | fake Hermes `/v1/runs` server + bridge `/v1/agent/ask` tests |
| Park on explicit switch, badge, un-park, Kids revoke, plain profile change detaches | implemented, tested | bridge + owner route tests |
| Stop speaking vs Stop all Agent work | implemented, tested | `tests/test_owner_assistant_modes.py`, browser |
| Owner-only text-free timings endpoint + offline report | implemented, tested | owner + unit tests |
| UI: mode switch, Everyday card, notice, Stop speaking, running badge, Advanced engine panels/disclosure, late-reply/owner-loss/Kids handling | implemented, tested | `tests/test_assistant_modes_browser.py` (Chromium 390/1440) |
| PWA shell v79 + new asset in service worker | implemented, tested | existing revision-sync tests |
| Docs | implemented | `docs/everyday-workspace.md`, CHANGELOG, companion README |
| Host registration of a native discussion target (`discussion_enabled`) | **remaining — deployment gate** | not provisioned by design |
| Live provider calls, latency measurements | **remaining** | needs supervised run with owner authorization |
| Physical acceptance (echo, barge-in, HA device, Kids switch) | **remaining** | supervised robot session |
