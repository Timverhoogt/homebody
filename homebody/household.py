"""One-time Owner household setup, applied to the existing settings (no second permission store).

The Owner boundary (pairing + same-origin + CSRF) protects the routes, but pairing alone grants
nothing: only an explicit POST that acknowledges the current disclosure revision applies the
chosen settings, once. Applying never captures, listens or starts a session. Purchases, messages,
locks and private account integrations are never part of the preset.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict

from .config import HOUSEHOLD_SETUP_VERSION, AppConfig

DISCLOSURE: dict[str, object] = {
    "version": HOUSEHOLD_SETUP_VERSION,
    "items": [
        {
            "id": "voice",
            "text": "After the wake word, speech goes to the configured voice provider (a cloud service for "
            "GPT-Live and Realtime) to understand and answer. Sleep turns the wake word off.",
        },
        {
            "id": "following",
            "text": "Face following and gestures run on Reachy itself. Faces are never identified or stored.",
        },
        {
            "id": "camera",
            "text": "When someone asks something visual (\"does this shirt look good on me?\"), Reachy may take "
            "one still frame for that request. It is analysed by the configured vision model (a cloud service "
            "unless local vision is on) and then discarded: no video, no monitoring, no face identification.",
        },
        {
            "id": "kids",
            "text": "Optional: I Spy in a Kids session that a grown-up started may use the camera without asking "
            "again. Safe-object checks and moderation stay on; other Kids camera rules do not change.",
        },
    ],
    "excluded": ["purchases", "messages", "locks", "private account integrations"],
    "revocation": "Revoke any time here, switch Camera access off, say \"camera off\" or \"stop looking\", "
    "or use Stop/Sleep.",
}


class HouseholdSetupRequest(BaseModel):
    """Explicit Owner choices; every capability is a separate boolean, nothing is implied."""

    model_config = ConfigDict(extra="forbid")

    disclosure_version: int
    acknowledged: bool
    camera_on_request: bool
    following_and_movement: bool
    voice_power_controls: bool
    kids_ispy_camera: bool = False


def household_status(config: AppConfig) -> dict[str, object]:
    current = config.household_setup_version >= HOUSEHOLD_SETUP_VERSION
    return {
        "completed": current,
        # The general Camera access setting; off overrides every approval below.
        "camera_enabled": bool(config.camera_enabled),
        "version": config.household_setup_version,
        "required_version": HOUSEHOLD_SETUP_VERSION,
        "completed_at": config.household_setup_completed_at if current else 0.0,
        "choices": {
            "camera_on_request": bool(current and config.camera_enabled and config.camera_natural_requests_enabled),
            "following_and_movement": bool(config.face_tracking_enabled and config.robot_tools_enabled),
            "voice_power_controls": bool(config.power_tools_enabled),
            "kids_ispy_camera": bool(current and config.camera_enabled and config.kids_ispy_camera_preapproved),
        },
        "disclosure": DISCLOSURE,
    }


def household_updates(request: HouseholdSetupRequest, *, now: float | None = None) -> dict[str, object]:
    """Settings the Owner explicitly chose; raises ``ValueError`` without a current acknowledgement."""
    if request.acknowledged is not True or request.disclosure_version != HOUSEHOLD_SETUP_VERSION:
        raise ValueError("Read and acknowledge the current household disclosure first")
    camera = request.camera_on_request is True
    return {
        "household_setup_version": HOUSEHOLD_SETUP_VERSION,
        "household_setup_completed_at": float(now if now is not None else time.time()),
        "camera_enabled": camera,
        "camera_natural_requests_enabled": camera,
        "face_tracking_enabled": request.following_and_movement is True,
        "robot_tools_enabled": request.following_and_movement is True,
        "power_tools_enabled": request.voice_power_controls is True,
        # Kids I Spy preapproval is only meaningful inside the adult camera approval.
        "kids_ispy_camera_preapproved": camera and request.kids_ispy_camera is True,
    }


def household_revocation() -> dict[str, object]:
    """Withdraw the household approvals; the general Camera access toggle keeps its own value."""
    return {
        "household_setup_version": 0,
        "household_setup_completed_at": 0.0,
        "camera_natural_requests_enabled": False,
        "kids_ispy_camera_preapproved": False,
    }
