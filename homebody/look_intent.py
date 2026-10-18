"""The robot and standalone host bridge use the exact same pure visual-consent classifier."""

try:
    from .bridge.look_intent import (
        direct_action_intent,
        direct_control_intent,
        looks_like_look_request,
        natural_visual_words_ok,
        requests_camera_session,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"homebody.bridge", "homebody.bridge.look_intent"}:
        raise
    from companion.look_intent import (
        direct_action_intent,
        direct_control_intent,
        looks_like_look_request,
        natural_visual_words_ok,
        requests_camera_session,
    )

__all__ = [
    "direct_action_intent",
    "direct_control_intent",
    "looks_like_look_request",
    "natural_visual_words_ok",
    "requests_camera_session",
]
