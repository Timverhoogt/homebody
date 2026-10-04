# Notifications

Homebody uses a small, dependency-free notification service for action feedback across tabs. The detail stays next to the control too. Notifications do not start, stop, retry, or approve robot actions.

## What you see

- **Progress:** stays until the action returns a result. Dismissing it hides the card, not the action.
- **Success or information:** disappears after six seconds of visible reading time.
- **Error:** stays until dismissed or replaced by the next result from that control.
- At most three cards appear together. Other cards wait with a count beneath the stack; their reading timers start only when shown.
- Timers pause while a card is hovered, has keyboard focus, or the page is hidden.
- Phone notifications sit above navigation. Fullscreen camera views keep the stack and its live regions inside the fullscreen element.

The stack is ephemeral: no notification history, browser storage, external delivery, or extra dependency. Owner-session loss and the Kids adult lock clear private feedback and suppress late results until access is restored. The underlying inline messages and action authorization are unchanged.

## Which events belong here?

Use notifications for deliberate action results: Settings, Agent, Presence, Initiative, Presentation, announcements, camera failures, and controller/button configuration. Power, bounded Agent operations, and local vision questions can update progress in place.

Keep telemetry and routine polling quiet. Ordinary motor-command success stays inline so a joystick or a row of movement buttons cannot flood the screen. Local vision answers stay beside the camera; the notification only says an answer is ready.

Repeated keyed text does not reset the timer. Unkeyed duplicates are suppressed for five seconds. The service holds at most twelve cards, including queued cards. If full, a transient success/info card can be removed to make room; pending operations and errors are never evicted. If no transient card is available, the new card is omitted and its inline feedback remains available.

## Frontend API

Load `notifications.js` before the control scripts. For a lifecycle with multiple states, reuse one ID:

```js
const notices = window.HomebodyNotifications;
notices.show("Preparing the preview…", { id: "agent-preview", kind: "pending" });
// After the server has returned a checked result:
notices.show("Preview ready. Review it before starting.", { id: "agent-preview", kind: "ok" });
// Or, on failure:
notices.show("Could not prepare the preview.", { id: "agent-preview", kind: "error" });
```

`kind` supports `pending`, `ok`, `error`, and `info`. `show` returns the ID, or `null` when suppressed. `dismiss(id)` removes one card; `clear()` removes all cards. `setSuppressed(reason, value)` manages independent privacy locks without one lock accidentally undoing another.

Existing inline message handlers use `notifyFeedback(element, kind)` in `main.js`. This preserves the message's class and text, adds a contextual label, and uses its element ID to replace earlier feedback from the same control. Do not pass secrets, pairing codes, credentials, or full private answers into the service. Content is rendered as text, never HTML.

Screen-reader messages use persistent `status` and `alert` live regions; dismiss buttons are outside those announcements. Dismiss buttons have a 44-pixel touch target, visible keyboard focus, and focus moves to the next card or back to the originating control. Reduced-motion preferences disable entry and spinner animation.

## Checks

Dependency-free behavioral tests:

```sh
python -m pytest tests/test_notifications.py tests/test_power_feedback.py -o addopts='' -q
```

Optional real-browser checks:

```sh
python -m pip install playwright
python -m playwright install chromium
python -m pytest tests/test_notifications_browser.py -o addopts='' -q
```

Browser checks load the actual app shell and JavaScript but intercept every request with explicit fixtures. They do not contact a robot, enable a camera, or run physical movement. They cover phone/desktop layout, keyboard dismissal, queueing and timers, an actual Settings submission failure, tab changes, owner-session expiry, text-only rendering, reduced motion, and fullscreen placement. Screenshots are saved under `.pytest_cache/notification-browser/`.
