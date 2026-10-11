# Native project Workspace

The Workspace can bind an owner-registered project to an exact existing Hermes API-server or OpenClaw Gateway session. The robot remains a voice surface, not a second installed agent. Native identity is separate from the restricted device/generation broker lease.

## Authorization and operation

Select the adult Agent profile, opt into conversation display, then select an owner-registered native target. Selection and roadmap reading do not authorize coding. Voice work requests prepare an exact scope for review; only the paired-owner approval control submits work. Approval is single-use, expires after 120 seconds, and rejects roadmap changes after review. The native input contains fresh scoped roadmap evidence and bounded recent project discussion. Native workers retain their own command and external-action approval policies.

The host setting `REACHY_NATIVE_TARGETS_JSON` is absent by default. Each target requires `backend`, `url`, `token_env`, `project_id`, and `session_id`. Optional fields are `execution_enabled` (default false), `verification_commands`, `artifacts`, `allow_primary` (default false), and `observe_approvals` (default false). Project IDs must exist in `REACHY_AGENT_PROJECTS_JSON`. URLs require TLS or direct loopback and cannot contain credentials or query strings. Do not register aliases for the same native session.

Execution requires fixed absolute-executable argv checks and exact relative artifact paths. Neither browser nor voice can supply URLs, credentials, roots or verification commands. The root is conveyed to the worker as approved context; it is **not a filesystem sandbox**. Provision an appropriately restricted native worker policy and isolated verification runtime before enabling execution. For a remote worker, ensure checks examine the same actual working tree/artifacts; a separate stale checkout is not deployment acceptance. Verifiers run with a scrubbed environment, but that does not isolate filesystem or network privileges.

OpenClaw uses native protocol 4, session resolution/history, chat admission, run observation and exact abort. Optional approval observation requests existing operator approval scope and emits only a generic session-scoped notice, not a claim that this particular run is blocked. Approval decisions remain in the backend owner UI. Secret-reference objects are not bearer tokens: provide a securely resolved host credential through approved deployment mechanisms; do not export write-only stores or stringify references.

## Truthful progress and recovery

Public accepted messages and explicit lifecycle/tool facts are displayed; hidden reasoning, tool arguments/results and generic payloads are excluded. Connection/admission uncertainty is not completion. No submission is replayed after a lost acknowledgement or restart. Existing native jobs are observed again by exact identity. Unknown or unconfirmed stopped work blocks session reuse, including from another device. Configuration changes require reconciliation of the journal before replacing targets.

Ready-to-test requires native completion plus successful fixed checks and artifact hashes, tied to the exact run and session. Owner testing is still needed. Worker prose alone cannot create readiness. Receipts are available under Verification evidence.

Display bodies and approval drafts remain bounded RAM-only. The private atomic journal contains configuration identity, bindings, run identifiers/status, hashes and verification receipts, not conversation or request bodies. An ordinary new wake preserves native work; Stop, Clear, Kids, privacy, profile or explicit power teardown revoke it. Remote Stop is a request, not a guarantee of immediate quiescence when disconnected. No new camera, microphone or motor authority is granted.

## Validation and deployment boundary

Final local verification: 966 non-browser tests and 26 actual Chromium checks passed. Loopback HTTP/SSE/WebSocket fixtures exercise transport, approval, cancellation, recovery and privacy boundaries. Phone/desktop screenshots use explicit fixtures, not a physical demo. Wheel resources include all native modules and JavaScript.

A read-only live Hermes capabilities/session-history probe succeeded. The installed OpenClaw protocol was confirmed as 4; its direct adapter probe is blocked by the configured secret-store reference. No native coding turn, speech, camera or motion was run. Installing this code does not enable native execution: target registration, suitable worker policy, secure authentication, meaningful verifiers, review and owner-supervised acceptance remain activation gates. Update the host bridge separately from the robot wheel, preserve configuration and keep the robot folded with torque disabled during deployment.
