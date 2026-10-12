# Subscription text models, reasoning off, native Workspace

Homebody separates OpenAI speech API sessions from subscription-funded text/vision inference. The host bridge uses the installed Hermes provider router and Codex Responses adapter, not an API-key-backed chat provider. It never exports OAuth tokens to the robot or dashboard.

## Host model configuration

- `REACHY_EVERYDAY_PROVIDER=openai-codex`, `REACHY_EVERYDAY_MODEL=gpt-6-sol`: narrow Everyday text/tool requests.
- `REACHY_LLM_PROVIDER=openai-codex`, `REACHY_AGENT_MODEL=gpt-6-sol`: the remaining restricted planning/discussion calls.
- `REACHY_KIDS_MODEL=gpt-6-luna`: Kids chat.
- `REACHY_ISPY_MODEL=gpt-6-luna`: I Spy vision, synonym judging and player guesses.
- Native Homebody Workspace: `gpt-6-sol` through a real, project-bound Hermes session.

The subscription adapter sends `reasoning.enabled=false` / `effort=none` and checks the generated Responses wire payload contains `reasoning.effort=none` before submitting. A model without genuine off support fails closed; do not relabel low/minimal as off. The account's `gpt-6.1-sol` currently has no `none` effort, while `gpt-6-sol` and `gpt-6-luna` do. Test actual server acceptance as well as local capability metadata.

`SubscriptionHttp` routes the existing bounded bridge text/vision calls to this adapter. It does not replace the underlying transport for moderation, speech, Home Assistant or native-session HTTP. Existing Kids input/output moderation, strict I Spy response/target validation, privacy, session-generation checks and deterministic game rules remain mandatory. Structured JSON is explicitly requested in the model instructions and strictly validated by the host; do not claim the Codex chat shim's ignored response_format guarantees a schema.

## Runtime and credential boundaries

Launch the bridge through `subscription_bridge_entry.py` with trusted `HOMEBODY_HERMES_SOURCE`. Bootstrap the installed managed Hermes runtime before importing the server. Preserve bridge CLI arguments through a runtime re-exec without interpreting `--profile reachy` as a Hermes CLI profile. An unbootstrapped subscription adapter refuses inference. Keep credentials on the trusted host, use only the supported saved-auth provider router and reject custom/non-subscription inference URLs. There is no paid text API fallback.

Everyday and Kids use direct, narrow prompts; they do not instantiate AIAgent or inherit Workspace memory, repository authority or shell/file tools. Native Workspace intentionally uses the real Hermes session, tools and normal approvals.

## Native Workspace listener

`subscription_workspace_entry.py` hosts the official Hermes session/run API on a dedicated authenticated loopback listener, sharing the owner's native session database. It runs only one exact host-registered Homebody session and project root. `native_subscription_policy.py` locks provider/model/reasoning before the official agent factory is called. Its `confirmed_runtime_lock` contract disables the native fallback chain without changing the primary gateway or its ongoing sessions. Unsupported reasoning-off models are refused.

Register `REACHY_NATIVE_TARGETS_JSON` against the existing Homebody project catalog and exact newly created session. Enable native discussion explicitly. Execution still requires the existing exact phone approval and configured verifier/artifact receipts; registration is not consent to arbitrary work. Keep native session identity, run identity and actual effective runtime distinct from the older restricted planning backend. Do not claim end-to-end Workspace readiness until a real native turn, stored history and bridge binding have been exercised.

## Verification and deployment

Back up bridge modules and host configuration with private permissions. Update the host component independently of robot releases/APKs. Verify safely idle motor-off Standby without triggering speech or movement. Read back actual provider/models and reasoning flags. Exercise harmless real text requests, Kids safe/blocked inputs, structured I Spy matching, synthetic-image vision (not the robot camera), and native session/run/history identity. Retain local negative tests for missing auth, wrong endpoint/model/session, inability to encode none, denied tools and stale callbacks.

OpenAI Realtime/GPT-Live speech and `omni-moderation-latest` safety screening remain API services; they are not subscription text chat. Do not remove moderation to claim an API-free setup. The primary Telegram/Desktop conversation model is not changed by these Homebody-specific policies.
