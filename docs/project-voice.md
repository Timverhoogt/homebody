# Project-aware voice: read-only first increment

This is the first implemented slice of delivery goal 2 in the [embodied Hermes roadmap](plans/2026-07-19-agent-mode-roadmap.md). It lets the existing restricted voice broker discover registered projects, read their actual roadmap, and resolve narrow roadmap follow-ups in the current adult session. It does **not** launch coding or create a native Hermes/Desktop project session.

## Host-owned configuration

`REACHY_AGENT_PROJECTS_JSON` is a **Homebody bridge** environment setting on the Hermes host, not a Hermes core configuration key. Empty/unset means disabled. No project file is granted automatically. The host operator registers up to 32 exact project IDs, titles, absolute repository roots and one relative `.md` roadmap per project. IDs are preserved and validated, never repaired. Example shape (paths illustrative, not deployed):

```json
{
  "homebody": {
    "title": "Homebody",
    "root": "/srv/projects/homebody",
    "roadmap": "docs/plans/2026-07-19-agent-mode-roadmap.md"
  }
}
```

The robot sends an ID, never a file path, repository URL, command, toolset or credential. The catalog exposes IDs/titles only; absolute roots stay on the host. Deploy `companion/reachy_projects.py` beside the updated broker and bridge; the robot wheel alone does not install the host companion service. No live service or catalog has been configured by this increment.

## Conversation flow

- An unlocked adult Agent session asks about a project or roadmap.
- The reasoning loop exposes **only** `list_projects` and `read_project_roadmap` for this project discussion. It also rejects out-of-scope calls server-side, even if a model emits one.
- Discovery can call the catalog, then read exactly one registered roadmap. That is the only new adaptive tool sequence; home-action plans keep their existing preview/approval behavior.
- Roadmap results carry line-numbered text, the hash of the actual bytes read, source ranges, observation time, explicit truncation and `execution_available: false`. The broker applies its existing secret redaction before model/browser boundaries.
- Small roadmaps are returned whole; large ones use bounded head/tail excerpts. Summaries must disclose omitted material and distinguish **recorded roadmap status** from verified implementation.
- After a successful read, the selected project and at most three short redacted user/reply pairs are held in the current device/generation lease. Narrow follow-ups such as “What's next?” and “What did we finish?” can refer to it without repeating the project name. Fresh progress answers must read the file again; previous assistant replies are not evidence.
- There is no generic intent parser yet. Other ambiguous follow-ups should name the project/roadmap. “Let's work on this” does not authorize or launch project execution in this increment.

Unconfigured access returns an explicit not-configured response without invoking a reasoning provider. Unknown IDs, unavailable files and invalid sources fail closed rather than guessing progress or substituting cached evidence.

## Retention and safety

The existing bridge authentication, adult/profile/power/privacy/availability checks and authoritative session registration apply to both capabilities. Private access requires intent in the current turn; the narrow follow-up exception applies only to the already-selected project's roadmap. Project turns are serialized per device lease; data is not shared between robots or included in unrelated conversations.

Brief project context expires in RAM after ten idle minutes (an actual timer clears it), and clears immediately when the lease generation or authorization state changes. Runtime Stop/Kids/privacy/power/profile teardown publishes those existing lease invalidations. No project text, prompts or responses are added to durable broker activity; the activity list retains capability/event metadata only. The optional Homebody transcript-display lease remains separate from this short upstream dialogue context, as it is from existing provider-side conversations.

File reads use no-follow directory/file descriptors, reject traversal, symlinks, nonregular files, invalid UTF-8 and oversized content, and are bounded to the existing 512 KB limit. Nonblocking opens ensure a FIFO cannot pin the worker before the regular-file check. Cancellation rejects late results and cannot grant project execution.

## Verification and remaining gates

Local checks: **927 non-browser tests passed**, **23 Chromium UI checks passed**, Ruff and whitespace checks passed. The wheel was built and exact companion project/broker/bridge resources and robot policy were verified inside it. A separate local read-only probe read the actual Homebody roadmap through the broker, returned source ranges/hash and completed activity, and confirmed no side effect or execution authority. The companion CLI also starts correctly through its direct-script import fallback.

Local verification exercises real filesystem reads and an actual aiohttp authenticated route. Reasoning replies in tests are explicitly deterministic provider fixtures, not evidence of real-model quality or live speech acceptance. The two voice transports already delegate adult Agent requests into this shared broker path; no new microphone, camera, movement or provider authority is added.

Pending: actual raw-photo-agent repository registration, real-provider/spoken acceptance, visible project/session selection in Workspace, stable native Hermes session identity across wake/reconnect, separately authorized host-side project execution, native run/tool-event streaming, durable work and ready-to-test notifications. Do not call delivery goal 2 complete until those gates pass.
