"""Owner-confirmed host shutdown, distinct from wake-time recovery.

Keep the original Stop receipt across lock waits and torque-off reconstruction.
Never recover into motion: only a healthy backend may run normal native Sleep.
The independent root helper still revalidates physical safety twice.
"""
from __future__ import annotations

from collections.abc import Callable

import httpx

from .motor_recovery import MotorRecoveryError, backend_admission_rejected, recover_motor_backend
from .shutdown_helper import safe_state


def _read_state() -> dict:
    response = httpx.get("http://127.0.0.1:8000/api/state/full", timeout=2.0)
    response.raise_for_status()
    state = response.json()
    if not isinstance(state, dict):
        raise MotorRecoveryError("Shutdown measurements are invalid")
    return state


def request_host_shutdown(runtime, schedule: Callable[[], dict]) -> dict:
    """Quiesce, verify/recover once, then atomically admit guarded OS scheduling.

    This is only called by the authenticated, confirmed shutdown route. Automatic
    Sleep/Standby and wake retain their existing authority and recovery policy.
    """
    if not runtime.control_ready:
        raise MotorRecoveryError("Runtime not ready; shutdown was not scheduled")
    with runtime._power_lock:
        runtime._pending_privacy_transitions += 1
        runtime._privacy_requested.set()
    # The returned receipt is immutable even if another Stop races with teardown.
    generation = runtime._request_conversation_stop()

    def current() -> bool:
        with runtime._conversation_stop_generation_lock:
            original = generation == runtime._conversation_stop_generation
        with runtime._power_lock:
            only_this_transition = runtime._pending_privacy_transitions == 1
        return original and only_this_transition and not runtime.stop_event.is_set()

    def check() -> None:
        if not current():
            raise MotorRecoveryError("The original shutdown was cancelled by Stop or a newer transition")

    try:
        with runtime._motor_transition_lock:
            check()
            if runtime._native_sleep_uncertain:
                raise MotorRecoveryError("Native sleep outcome is unknown; shutdown recovery refused")
            actions = runtime._actions
            if actions is not None and actions.fence_reason:
                raise MotorRecoveryError("Another movement owner blocks shutdown")
            with runtime._power_lock:
                runtime._power_mode = "sleep"
                runtime._meeting_until = 0.0
            runtime.stop_presentation_window("power_sleep")
            runtime.revoke_camera_control()
            runtime._voice_workspace.clear()
            # The Stop above already revoked voice. Do not rebase its receipt by
            # asking cancel_agent_work to publish another Stop on our behalf.
            runtime.cancel_agent_work("session_changed")
            with runtime._agent_lock:
                runtime._agent_native_preserved_generation = None
            runtime._cancel_announcements(clear_queue=True)
            runtime._presence.clear("power_sleep")
            runtime._end_face_follow()
            if actions is not None:
                actions.fence("Owner-confirmed host shutdown")
            try:
                if runtime._motion is not None:
                    runtime._motion.fence_motor_recovery()
                runtime._playback.suspend()
                runtime.robot.media.stop_playing()
                runtime._playback.confirm_stopped()
                runtime._playback_stopped_for_privacy = True
                runtime._clear_streamed_audio()
                if runtime._recording:
                    runtime.robot.media.stop_recording()
                    runtime._recording = False
                # Retire producers without sending a packet to an absent backend.
                if actions is not None:
                    actions.cancel(backend_available=False, stop_media=False)
                    if not actions.wait_idle(timeout=5.0):
                        raise MotorRecoveryError("Old movement work did not drain; shutdown refused")
                check()
                recovered = False
                try:
                    state = _read_state()
                except httpx.HTTPStatusError as exc:
                    if not backend_admission_rejected(exc, method="GET", path="/api/state/full"):
                        raise
                    runtime._motors_enabled = None
                    runtime._head_safely_folded = False
                    runtime._torque_epoch += 1
                    runtime._set_status(
                        "recovering_motors", "Reconnecting motor readings for shutdown; torque stays off",
                        last_error="",
                    )
                    recover_motor_backend(current=current, wait=runtime.stop_event.wait)
                    check()
                    # No client replacement is needed: shutdown uses fresh HTTP
                    # measurements and the helper, never the old control socket.
                    state = _read_state()
                    recovered = True
                check()
                if not safe_state(state):
                    if recovered:
                        raise MotorRecoveryError("Reconnected robot is not verified folded with torque disabled")
                    # A pre-existing healthy backend retains ordinary Shutdown's
                    # native folding behavior. Reconstruction never enters it.
                    runtime._apply_power_mode()
                    check()
                    with runtime._status_lock:
                        error = runtime._status.last_error
                    if error:
                        raise MotorRecoveryError(error)
                    state = _read_state()
                    if not safe_state(state):
                        raise MotorRecoveryError("Shutdown requires a verified folded pose and disabled torque")
                check()
                runtime._motors_enabled = False
                runtime._head_safely_folded = True
                runtime._released_torque_epoch = runtime._torque_epoch
                runtime._remember_fold_outcome(True, "")
                runtime._set_status(
                    "sleep", "Voice is disabled; Reachy is folded safely into Sleep",
                    power_mode="sleep", meeting_seconds_remaining=0, last_error="",
                    announcement_current_preview="", announcement_last_text="",
                )
                # Linearize scheduling against Stop/power admission. Once the OS
                # accepts its timer, later Stop is not a poweroff cancellation API.
                with runtime._power_lock, runtime._conversation_stop_generation_lock:
                    if (generation != runtime._conversation_stop_generation
                            or runtime._pending_privacy_transitions != 1 or runtime.stop_event.is_set()):
                        raise MotorRecoveryError("The original shutdown was cancelled before scheduling")
                    return schedule()
            except Exception:
                # Mark privacy as applied even on refusal: the wake loop must
                # NOT turn a failed reconstruction into an automatic native fold.
                runtime._motors_enabled = None
                runtime._head_safely_folded = False
                runtime._set_status(
                    "sleep", "Voice is disabled; shutdown safety was not confirmed",
                    power_mode="sleep", meeting_seconds_remaining=0,
                    last_error="Shutdown safety was not confirmed; inspect the robot before retrying",
                )
                raise
            finally:
                if actions is not None:
                    actions.unfence()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        runtime._motors_enabled = None
        runtime._head_safely_folded = False
        raise MotorRecoveryError("Shutdown could not verify motor safety; no poweroff was scheduled") from exc
    finally:
        with runtime._power_lock:
            runtime._pending_privacy_transitions -= 1
            if not runtime._pending_privacy_transitions and runtime._power_mode not in {"sleep", "meeting"}:
                runtime._privacy_requested.clear()
