# Dashboard feedback

## Audit and presentation rules

The dashboard already had bounded global notifications, but also left raw green action
results beside controls. Action outcomes now use the same titled, text-only dark card,
cream body text, small semantic icon, and explicit dismiss control. Successful feedback
expires; errors and pending work remain until dismissed or replaced. Hover/focus pauses
expiry. Owner and Kids locks, fullscreen placement, and queue limits remain unchanged.

- Power transitions and app lifecycle: titled notifications; remove duplicate inline outcomes.
- Settings and connection tests: pending/result replace the same Settings card.
- Agent, Kids, announcements, presence, initiative, presentation, controller, GPIO,
  camera tests, local vision tests, agent setup, and MCP access: shared titled feedback helper.
- Camera stream failures: same Camera notification style; stream status remains inline.
- Home-screen installation action/error: shared feedback helper. Installation readiness stays inline.
- Robot's rapid movement successes and routine telemetry: deliberately quiet.
- Vision answers, pairing instructions, permission reasons, and polled diagnostics:
  persistent inline content, not disappearing notifications.

Inline messages use rounded neutral callouts with a small semantic border rather than
all-green text. Action text is hidden only after the notification service accepts it;
when unavailable/suppressed/full, the local message remains a fallback. Persistent power
state remains in the existing state display. Notifications never grant robot authority.

## Verification

Node tests cover replacement timers, pending dismissal, confirmed/false-success power
responses, network failures, queueing, text-only rendering and privacy suppression.
Chromium tests exercise real DOM/CSS at phone, landscape and desktop sizes, title
announcements, icons, duplicate suppression, expiry, inline fallback, and fullscreen.
All robot APIs in these tests are explicit intercepted fixtures; they cause no motion.
