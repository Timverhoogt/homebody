# Voice workspace: first increment

The Agent page defaults to Companion (presence, timing and shared context). Workspace is a simpler conversation view; the existing bounded plan runner remains under Advanced home actions. This is not yet an unrestricted Hermes coding session.

## Use after deployment

1. Open Homebody on a paired owner device and choose Agent → Workspace.
2. If you want Agent capabilities, select the existing profile **before** starting conversation display; profile changes deliberately clear the display.
3. Choose **Show next conversation**. This only opens a one-hour RAM display lease. It does not wake Reachy, start audio, enable a camera or authorize any additional tools.
4. When Reachy is in an ordinary listening mode, say the wake word and speak. Accepted user transcripts and generated assistant replies appear, separated from actual voice activity. Pipeline and Realtime are hooked independently; Realtime partial transcripts are not treated as accepted messages.
5. Open **Tool activity & session details** for the existing sanitized broker activity. Arbitrary Hermes tools/background project work are not streamed by this increment.
6. **Clear & disconnect…** asks for confirmation first, because it does more than hide the view: it invalidates producer generations, clears memory, stops the selected project's work and disconnects the project (backend behaviour unchanged). Afterwards tap **Show conversation** and **Open project** again. Use **Stop speaking** to only end the current reply. Realtime capture begins with the next wake session; it does not retroactively capture an already open Realtime session.

Since 0.5.12 the conversation/projects view exists only in a status-confirmed Workspace. In Everyday, while a switch is pending, offline, after owner loss or in Kids Mode it is hidden and cannot be opened; any preserved project work shows a badge with **Stop all Agent work**. Showing the conversation is display consent only: it is not a project connection and does not start listening. Reachy listens after “Hey Homebody”.

## Where spoken Workspace requests go

The Workspace card shows **Spoken requests go to** separately from display, project connection and listening.

- **Open project with host voice discussion enabled:** each spoken turn (Pipeline, Realtime and GPT-Live) goes to that bound Hermes session and is labelled with its session/run.
- **No project session configured on the host, no open project, project paused by Everyday, or voice discussion off on the host:** ordinary chat is **not** sent anywhere. Reachy says what to do (ask the host owner to add a session, Open project, switch to Workspace, or that the host has discussion off) and reports a `not_ready` outcome rather than a success. Only explicitly named roadmap or capability requests still use the restricted broker, labelled “not your Hermes session”. Examples are a project roadmap, status, timers, reminders, media, home devices, calendar, notes, messages, search and weather.
- GPT-Live reports `answered` only for a verified reply. Failures report `failed` with fixed text, and only a text-free category is logged (for example `answer_unverified` or `provider_unavailable`). A Clear, profile change or other new generation during a turn discards the late answer and ends the Live session silently.

## Retention and privacy

- Default off; explicit owner opt-in. No transcript persistence to disk, audit log, localStorage, sessionStorage or service-worker cache.
- At most 120 events and 4000 displayed characters per event. Long messages are labeled as shortened; common credential patterns are redacted.
- One-hour hard lease, checked on append/read; no polling or refresh can extend it. RAM is discarded on process restart.
- Kids, privacy, power teardown, profile changes, runtime shutdown and Agent Stop clear and invalidate captured content. An ordinary new wake preserves the opted-in display so subsequent turns can continue.
- Private endpoints require paired owner authentication, CSRF for writes and no-store responses. No new transcript field is added to public `/api/status`.
- The browser drops late poll responses after privacy, owner loss or Clear. Offline hides already rendered conversation; remote RAM is governed by the same lease and runtime privacy transitions.
- A generated reply is not proof every word was played aloud. Barge-in can interrupt playback. Neither hidden reasoning nor raw tool arguments/results are captured.

## Verified acceptance

Local unit suite: 872 passed. Chromium suite: 23 passed, including phone/desktop rendering, text-only messages, clear/privacy and late-response protection. Visual inspection used actual Chromium screenshots with explicit API fixtures. Ruff and whitespace checks passed.

Still required: deploy under the normal review/green-check workflow; then owner-supervised Pipeline and Realtime voice checks, a follow-up, clear/Stop, and Kids/privacy transitions. Do not call the raw-photo-project work example complete before roadmap/session binding and authorized host execution exist (roadmap Goal 2).
