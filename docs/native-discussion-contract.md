# Native Workspace discussion: contract v1 and handoff

Status: implemented and tested in isolated source trees only. Nothing has been installed, restarted, published or pushed. **The live host is not ready:** the running Hermes (`/home/timv/.hermes/hermes-agent`) does not advertise the contract, so Homebody stays at `discussion_unenforced` there and sends nothing.

Baselines: Homebody `74f8770` (worktree `/home/timv/homebody-native-discussion`), Hermes `41cd7b649d` (isolated tree `/home/timv/hermes-native-discussion`). Changes are isolated on `fix/native-discussion-enforced` in each repository; neither installed tree was modified.

## Gap in the baseline

`POST /v1/runs` (`gateway/platforms/api_server_runs.py::_handle_runs`, `_execute_run`, `_run_agent_sync`) creates the agent through `APIServerAdapter._create_agent` with the full `api_server` platform toolset. Admission accepts caller-supplied `conversation_history`, `instructions`, model, provider and room-policy fields. It may also hand the turn to a live Bot Chat owner's mailbox. No per-run policy existed that limited tools at dispatch. A prompt or an empty schema would not have been enforcement.

## Wire contract (v1)

**Handshake.** `GET /v1/capabilities` (bearer auth) returns:

```json
"features": {"native_discussion": {"version": 1, "request_field": "hermes_discussion", "mode": "discussion",
  "tools": "none", "enforcement": "dispatch", "history": "session", "profile": "<served profile>"}}
```

The profile comes from the server's authenticated request scope. It is the URL-selected `/p/<profile>/` scope if there is one, else the launch home's own name from `hermes_cli.profiles.get_active_profile_name()`: `default`, a named profile, or `custom` for an unnamed `HERMES_HOME`. It is never an assumed `default`, and a caller can never set it.

**Request.** `POST /v1/runs` with exactly the following body. An `Idempotency-Key` header is allowed.

```json
{"input": "<one plain user message>", "session_id": "<S>",
 "hermes_discussion": {"version": 1, "mode": "discussion", "tools": "none", "session_id": "<S>", "profile": "<P>"}}
```

**Admission refusals.** No run, agent or session is created for any of these:

| Condition | Status / code |
|---|---|
| Any other body field: `conversation_history`, `previous_response_id`, `instructions`, `model`, `provider`, `model_options`, `_room_execution_policy`, `hosted_room_dispatch`, author fields, … | 400 `discussion_policy_rejected` |
| List `input` | 400 |
| Policy fields not exact, or `version` not the int `1` (`True` refused) | 400 / `discussion_version_unsupported` |
| `mode`/`tools` other than `discussion`/`none` | 400 |
| Policy `session_id` ≠ body `session_id` | 400 |
| `X-Hermes-Session-Key` present | 400 |
| Room-grant authentication | 403 |
| `profile` ≠ served profile | 403 `discussion_profile_mismatch` |
| Session missing | 404 `session_not_found` (never a fallback to a new or `run_id` session) |
| Compression-rotated id | 409 `discussion_session_rotated` |
| Session held by a live Bot Chat owner (never routed to the owner mailbox) | 409 `discussion_session_live_owner` |
| Transcript unreadable | 503 `discussion_session_unavailable` (no silent empty history) |

**Echo.** The 202 admission response and every `GET /v1/runs/{id}` status include:

```json
"discussion": {<the 5 policy fields>, "enforcement": "dispatch", "policy_digest": sha256(canonical policy JSON), "tool_calls_refused": n}
```

Canonical JSON is `sort_keys`, separators `(",", ":")`, ASCII. Idempotent replays echo it too.

**Execution.** The run uses:
- the persisted canonical history of `S`, reloaded at execution (see below); caller history is impossible;
- a fixed server-side system note;
- `enabled_toolsets=[]`, max 4 iterations, `skip_memory`, `skip_background_review`, `checkpoints_enabled=False`, `compression_enabled=False` (no auxiliary summary call, no rotation);
- no memory-provider checkout or check-in;
- no wake/delivery authority;
- no owner mailbox.

The policy is attached to the agent (`_hermes_discussion_policy`). It is also bound in a contextvar from **agent construction** through the whole turn, on the loop thread for `_create_agent` and on the executor thread for `run_conversation`.

**History at execution, not admission.** `_discussion_session_history` runs at admission (fast refusal) and again in the executor thread immediately before `run_conversation`. The second load therefore includes turns that landed after admission, such as earlier queued work. If at execution the session was rotated, a live owner claimed it, the session vanished or the store is unreadable, the run fails (`failed`, with the error code in `error`) before the model runs and writes nothing. It never runs on an empty or different context, and never through the owner mailbox.

**Plugins, hooks and auxiliary work.** While the policy is bound, `PluginManager` delivers nothing:
- `invoke_hook` / `ainvoke_hook` return nothing;
- `has_hook` and `iter_hook_callbacks` report no callbacks;
- `render_system_prompt_sections` renders none;
- the event bus (`_dispatch_event`) and middleware (`has_middleware`, `invoke_middleware`, `middleware._run_execution_chain`) are skipped.

This covers user plugins, configured shell hooks and outbound webhooks, which all register into the same hook table. The auxiliary title generator is skipped. Ordinary owner runs are unaffected (tested).

**Dispatch enforcement.** All three boundaries are in `agent/discussion_guard.py`:
1. `AIAgent._execute_tool_calls` is checked before segment planning, so it covers every serial, parallel and segmented batch. It also covers agent-loop tools (`memory`, `todo`, `delegate_task`, `session_search`), whatever `valid_tool_names` or a restored agent claims. Every emitted call gets a `discussion_tools_disabled` tool result, and the count is recorded.
2. `model_tools.handle_function_call` covers code-execution RPC, the Hermes-tools MCP server, Tool Search `tool_call` (deferred), connectors and plugins.
3. `tools/registry.ToolRegistry.dispatch` is the last stop before any handler.

**Stop and steer.** Steer on a discussion run returns 409 `discussion_run_not_steerable`. `/stop` on a discussion sets the agent's `_persist_disabled` before interrupting it. A model reply that arrives after Stop is therefore neither persisted to the transcript nor published: the status is `cancelled` and there is no output. The prior transcript is untouched (tested with a fake model that ignores the interrupt and returns late).

**Allowed persistence.** The user turn and the answer (and any refusal tool rows) are appended to `S`'s transcript, exactly as for any conversation turn. There are no memory-provider sync, background memory/skill review, checkpoints, delivery-row claims or tool side effects.

## Hermes changes (isolated tree)

- `gateway/native_discussion.py` (new): policy parsing, capability, digest/echo.
- `agent/discussion_guard.py` (new): contextvar and refusal.
- `gateway/platforms/api_server_runs.py`:
  - parsing, `_admit_discussion_session` and `_discussion_session_history` (admission and execution reload);
  - launch kwargs and the mailbox bypass;
  - status/acceptance echo;
  - contextvar binding during construction and the turn;
  - terminal echo and stop guard, plus `_persist_disabled` on stop;
  - steer refusal.
- `gateway/platforms/api_server.py`: `_create_agent(discussion_policy=…)` (no tools, memory, review, checkpoints or compression) and the `features.native_discussion` capability.
- `run_agent.py`, `model_tools.py`, `tools/registry.py`: the dispatch guards. Ordinary runs are unchanged; the owner's tools are not globally disabled.
- `hermes_cli/plugins_dispatch.py`, `hermes_cli/middleware.py`: no plugin, hook, event-bus or middleware delivery in a discussion scope.
- `agent/turn_context.py`: no title generation for a discussion.
- `tests/gateway/test_api_server_native_discussion.py` (new).

## Homebody changes

- `companion/native_workspace.py`:
  - `DISCUSSION_CAPABILITY`, `discussion_policy`, `discussion_echo` (exact fields, types, digest);
  - `discussion_available` now means host opt-in **and** the latest verified handshake;
  - `verify_discussion`, which is bounded by a 10 s timeout and fails closed;
  - `discuss()` re-handshakes before every submission, tracks the in-flight task, discards answers after epoch/binding change, and is cancelled by `stop()`/`close()`. Prepare/approve/execution are untouched.
- `companion/native_adapters.py`: `HermesNativeAdapter.discussion_handshake` and `discuss`.
  - `discuss` sends no instructions.
  - It verifies the echo on admission and every poll, plus session/run identity.
  - It has a 75 s timeout, after which it stops the run.
  - On a mismatch, Stop or cancellation it stops the run (shielded) and fails.
  - A cancel during admission lets the bounded POST finish, then stops the run it admitted.
- `companion/hermes_reachy_bridge.py`: voice readiness refreshes the handshake before computing state. The `discussion_unenforced` text now says the host did not confirm.
- Docs: `docs/native-workspace.md`, `companion/README.md`.
- Tests:
  - New `tests/test_native_discussion_e2e.py`, which runs Homebody against the real isolated Hermes server subprocess in `tests/fixtures/hermes_discussion_server.py`.
  - The obsolete always-refuse tests in `tests/test_workspace_live_native.py` and `tests/test_everyday_workspace_bridge.py` now assert negotiated enforcement.

## Verification (executed)

Final independent execution (after the last source edit):

| Check | Result |
|---|---|
| Homebody `tests/test_native_discussion_e2e.py tests/test_workspace_live_native.py tests/test_everyday_workspace_bridge.py` | **45 passed**, no skips, 9.41 s |
| Hermes `tests/gateway/test_api_server_native_discussion.py tests/gateway/test_api_server_runs.py tests/gateway/test_api_server.py tests/tools/test_registry.py` | **257 passed**, no skips, 38.79 s |
| All 146 Homebody non-browser modules (`tests/test_*.py` excluding names containing `browser`) | **1988 passed, 4 skipped**, 163.66 s; `.artifacts/native-discussion/unit.xml` |
| Final wheel build, with packaged companion bytes compared to final source | **PASS** |
| Both source trees `git diff --check` | **PASS** |

The four unrelated skips are three legacy tests looking only under `dist/` (this candidate wheel is under `.artifacts/native-discussion/dist/`) and one local probe script not shipped in this checkout. They are not a zero-skip release gate. The combined browser+unit run was deliberately stopped to avoid a redundant unbounded browser pass; no full browser acceptance is claimed for this follow-on. The parent integration candidate must execute its own final gates.

The worker also executed **436 passing** Hermes tests covering discussion, runs, API server, room dispatch, toolsets, memory sessions, declared conversations, async tools, registry, lifecycle, plugins, middleware, event bus and prompt sections before the independent focused rerun.

The initial missing `build`/`aioesphomeapi` failures were resolved by selecting the already existing read-only Homebody integration Python 3.12 environment; no shared environment was modified. Cross-repository E2E explicitly selects the existing Hermes interpreter through `HOMEBODY_HERMES_DISCUSSION_PYTHON` and source through `HOMEBODY_HERMES_DISCUSSION_SOURCE`.

Artifact: `.artifacts/native-discussion/dist/reachy_mini_homebody-0.5.16-py3-none-any.whl`, 2476389 bytes, SHA-256 `c0e5f5f470a7bdcd6fcef93635968f5d4e0adf23088c35e5d732a8519b93a0f5`. This is a development artifact, not a canonical signed/published update. The final wheel's `native_adapters.py`, `native_workspace.py` and `hermes_reachy_bridge.py` exactly match the source bytes.

The end-to-end tests use real HTTP, the real Hermes admission and the real `_create_agent`/`run_conversation`/dispatch code. Only the model transport and the auxiliary title call are replaced, so no provider is contacted. They prove:
- the answer comes from persisted session history, with zero tool schema;
- the transcript was appended to the same session;
- a hostile model plus restored `valid_tool_names` (terminal, write_file, patch, memory, delegate_task, an MCP name, `tool_call`, execute_code, skill_manage, send_message) gives 10 refusals, zero handler resolution, and no file written;
- direct `handle_function_call`/`registry.dispatch` calls on the run thread are refused;
- override, profile, session and malformed requests are refused with no agent created and no session created;
- Stop cancels the backend run and the late answer is never published;
- a downgraded handshake means no submission;
- the approved-work path still runs an ordinary run with full platform tools.

## Admission loss and cancellation (Homebody)

- **Stop or cancel while the admission POST is in flight.** The POST is shielded and allowed to finish; it is bounded at 20 s by `json_request`. If it returns a run id, that run is stopped and never polled, so no answer is accepted (tested).
- **Unresolved: admission outcome genuinely lost.** This covers a connection reset or timeout after Hermes accepted the run but before Homebody read the 202. Homebody cannot learn the run id, so it does not stop the run. The orphan is still a discussion run: no tools, no plugins, no memory, and it is never answered to the owner, because Homebody raised and never polls it. It runs to completion and **will append that user turn and its answer to the session transcript**. A retry with the same `Idempotency-Key` would identify it, but Homebody does not retry, to avoid creating a new run when the first never arrived. Reconciling this case is not implemented.
- Stop during polling, a timeout (75 s), an echo or identity mismatch, and epoch/binding changes all stop the known run and discard any output (tested).

## Known limits

- Built-in first-party observability (`hermes_cli.observability.observe_lifecycle`, metrics/counters) still records discussion turns. It is not plugin code.
- Gating plugins relies on the contextvar. It is bound on the loop thread during agent construction and on the executor thread for the whole turn. A thread that Hermes core starts without copying context would not see it. Model-originated tool calls are still stopped by the agent-attribute guard (boundary 1). No such core thread was found on the discussion path (title generation and background review are skipped, and compression is disabled).
- Disabling compression means a session too long for the model's context fails the discussion turn rather than rotating it.
- A rotated session id (from earlier owner work) is refused until the Homebody target names the live tip.

## Remaining deployment sequence (each step needs separate approval)

1. Review and commit both isolated trees. The Hermes change must reach the installed source through the owner's normal upgrade path. Do not hand-copy into `/home/timv/.hermes/hermes-agent`.
2. Restart the Hermes gateway/API server. Confirm `GET /v1/capabilities` shows `features.native_discussion` exactly as above for the profile Homebody uses.
3. Install the built wheel `.artifacts/native-discussion/dist/reachy_mini_homebody-0.5.16-py3-none-any.whl` on the host bridge, separately from the robot wheel, preserving configuration.
4. Runtime acceptance:
   - set `discussion_enabled: true` on the target;
   - check that voice readiness is `ready` and the snapshot shows `voice_discussion: true`;
   - ask a question answerable only from session history;
   - ask for a write and confirm the run shows `tool_calls_refused` and no side effect;
   - test Stop mid-turn;
   - check that phone-approved execution is unchanged;
   - accept on phone and voice (EN/NL), robot folded/torque policy as usual.

Until then, native discussion is not live.
