# Robot controls

The Robot tab gives a trusted local operator safe, bounded manual control over Reachy's head and base.

## Layout

**Wake & enable**, **Fold & disable**, and cooperative **Stop action** sit together at the top of the tab. Below them:

- **Nine-way bounded head pad** for diagonal looks.
- **Precision controls** for X/Y/Z in 1, 2.5, 5, or 10 mm steps and roll/pitch/yaw by the same number of degrees.
- **Rotating-base steps** in 5°, 15°, 30°, and 60° increments. Head yaw couples to base rotation to preserve the SDK head/body relationship. Clear-space confirmation is required at 30° and 60°. Rotation clamps at ±120° inside the SDK's ±160° safety range.
- **Measured pose readback** stays visible beside the controls.
- **Independent centering** for head, base, or both.
- **Expression presets** that describe their motion character.
- **Dance presets** labeled compact, medium, and wide movement. The wide Energetic move has an extra clear-space confirmation.

## Safety model

All controls use the same serialized action worker as Realtime. The browser cannot submit raw joints, arbitrary move names, shell commands, or motor calibration.

A manual movement from Standby first completes Reachy's native wake motion and leaves it Awake. Power transitions pause all presets. Additional taps are rejected while an action is active. Privacy is rechecked immediately before execution. Meeting and Sleep block movement.

**Stop action** remains available during semantic movement. It cancels active and queued work without changing power mode or initiating a new pose, and preserves the voice playback pipeline. Safe folding is never interrupted.

The UI is for a trusted LAN/VPN only.
