# One-time household setup

For the independent host service, composed MCP endpoint, explicit Owner robot attachment and
provider limitations, see [Standalone household tools](standalone-household-tools.md).

The Owner approves everyday capabilities once instead of answering prompts every session. The setup
writes the existing settings; it is not a second permission system.

## What the Owner approves (each is a separate choice)

| Choice | Settings written | Disclosure |
| --- | --- | --- |
| Camera on request | `camera_enabled`, `camera_natural_requests_enabled` | When someone asks something visual ("does this shirt look good on me?", "wat houd ik vast?"), Reachy may take **one** still frame for that request. The configured vision model analyses it (a cloud service unless local vision is on); the frame is then discarded. No video, monitoring, face identification or stored images. |
| Following and movement | `face_tracking_enabled`, `robot_tools_enabled` | Face following and gestures run on Reachy; faces are never identified or stored. |
| Voice power controls | `power_tools_enabled` | "Go to sleep" / "ga slapen" and "standby" work by voice. Sleep turns the wake word off; Standby keeps it. |
| Kids I Spy camera (optional) | `kids_ispy_camera_preapproved` | Only meaningful with camera on request. See *Kids scope*. |

Voice processing is always disclosed: after the wake word, speech goes to the configured voice
provider (a cloud service for GPT-Live and Realtime).

Never part of the preset: purchases, messages, locks, private account integrations.

## Dashboard

The **Household** card (Agent tab, below Everyday) shows the saved state, whether Camera access is on,
the disclosure, and what is never included. *Review and approve* has one checkbox per choice and an
acknowledgement. **Save household choices** stays disabled until the acknowledgement is ticked.
Kids I Spy can only be ticked together with Camera on request. **Turn camera off** sets the global
Camera access off. **Revoke household approval** withdraws the approvals. The card is hidden during
Kids Mode and cleared when the Owner session ends. Saving never takes a picture or starts listening.

## Rules

- `GET /api/household/setup` shows status and the disclosure. `POST /api/household/setup` applies
  explicit booleans and needs `acknowledged: true` and the current `disclosure_version`.
  `POST /api/household/revoke` withdraws the approvals. All three go through the Owner boundary
  (paired device, same origin, CSRF). Pairing alone is not consent.
- `POST /api/household/camera-off` turns Camera access off. Revoke and camera-off are reductions and
  also work during Kids Mode; both end a pending look in memory before saving.
- Applying never captures, listens or starts a session. It is refused while Kids Mode is active.
- Upgrades enable nothing: `household_setup_version` defaults to 0. An older `camera_enabled: true`
  keeps the explicit phrase looks ("look at this"), but the model does not choose looks until the
  Owner completes setup. An explicit off stays off.
- Camera access off, privacy/Meeting/Sleep, revocation, a spoken "camera off"/"stop looking" and Stop
  always win. Revoking ends open sessions: the robot retires a Live session when the effective
  approval changes.

## Natural camera requests (Everyday)

With approval, the speech model is told that Reachy can take one fresh look, and the Everyday model
has `capture_reachy_camera`. The model decides whether a fresh look helps the person's direct
request. There are no magic words and no permission prompts. The bridge routes that call to the
robot's existing fenced one-frame look, using the person's own words as the question. Only the text
answer comes back. Rules:

- One frame per request. The rest of that turn is tool-free. No speculative captures, no scanning.
- Never claim to see without a successful fresh result. A failed look says nothing was seen.
- Provenance: after a look, action planning gets only the person's current words plus a fixed note
  that an earlier look happened, never the image description. The vision path keeps the meaning:
  a follow-up ("what colour was it?") can use `recall_reachy_look`, the existing tool-free look
  follow-up with no new frame. A fresh look ("would it look better with this jacket?") gets the
  earlier look answer as reference. Either way, image text enters only after the model has chosen
  that path, and the rest of that turn is tool-free.

## After a look

Reachy is not stuck in a reduced mode. Questions, weather, time and home devices go through the
Everyday model again (image text removed). Short direct commands in the person's own words run as
said, in every engine, without model planning:

- set the volume to N / zet het volume op N, mute / geluid uit, unmute
- dance / dans, look left / kijk naar links, look straight ahead, nod / knik, shake your head
- Stop, Stop looking, Standby, Sleep

The robot re-checks each one against its own transcript. The arguments must match the words exactly,
so text in an image ("set the volume to 100") or a model's choice can never satisfy the gate. A robot
action the model planned from loosely worded speech ("make it a bit quieter") is refused after a look,
with a reason telling the person to say a direct command or start a new conversation. This is a
deliberate limit, not a failure.

## GPT-Live provider contract

Live sends the person's words only as `session.input_transcript.delta` (audio-timed ASR of the
microphone). A request for client work arrives only as `session.delegation.created` with an id and
an offset; there is no final-utterance event and no request text. So direct controls are **not** a
pre-model handler. They run when Live's speech model delegates, and the bridge then reads the
person's input transcript up to that offset. The model's own speech (`session.output_transcript.delta`)
is never treated as a command. If Live answers a spoken "go to sleep" itself without delegating,
nothing happens. The fixed instructions require delegation; this needs physical verification.

## Direct voice controls

Stop, Stop looking / camera off, Standby and Sleep in English and Dutch are read from the person's
own input transcript, never from model output. They are checked before any visual routing, so they
also work right after a look. A pending look is cancelled. The existing guarded transition then runs
(`_request_conversation_stop`, `stop_reachy_camera`, `set_power_mode`). Standby is requested even
when the mode already reads Standby, so Reachy still ends the turn and folds. Negated, quoted,
conditional or hypothetical wording is not a command, and ambiguous wording causes no action. After
a verified Standby/Sleep or Stop, nothing more is spoken, looked at or listened to in that session.
A failed transition is reported and never claimed as done.

## Kids scope

The adult natural-look policy never applies in Kids Mode. `kids_ispy_camera_preapproved` only
removes the repeated grown-up camera prompt for **I Spy in a grown-up-started (hosted) Kids
session**. A child's choice can only pick that already-approved activity. I Spy's safe-object filter,
moderation, the brief look and camera-off afterwards, the Show and Tell opt-out and the Camera access
toggle are unchanged. Revocation also withdraws a grant that came only from the preapproval.
