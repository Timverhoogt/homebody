"""The robot and standalone host bridge use the exact same pure visual-consent classifier."""

try:
    from .bridge.look_intent import looks_like_look_request
except ModuleNotFoundError as exc:
    if exc.name not in {"homebody.bridge", "homebody.bridge.look_intent"}:
        raise
    from companion.look_intent import looks_like_look_request

__all__ = ["looks_like_look_request"]
