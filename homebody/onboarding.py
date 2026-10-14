"""Owner-only, resumable first run. Readiness never initiates robot media or actions."""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from .config import AppConfig, config_transaction, merge_config
from .platform_info import host

CHECK_TTL = 600
STEPS = ("welcome", "connection", "review")


class Progress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: Literal["welcome", "connection", "review"]
    entry_route: Literal["unknown", "marketplace", "manual"] | None = None


class Finish(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal["restart"]


def connection_revision(config: AppConfig) -> str:
    # A local private digest binds the proof to saved credentials and selected speech settings.
    # It is never returned to the browser or public status.
    names = (
        "bridge_url",
        "api_key",
        "model",
        "language",
        "conversation_mode",
        "stt_provider",
        "stt_model",
        "stt_robot_fallback",
        "tts_provider",
        "tts_model",
        "tts_voice",
        "realtime_model",
        "realtime_voice",
        "live_model",
        "live_voice",
    )
    payload = {name: getattr(config, name) for name in names}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def checked(config: AppConfig) -> bool:
    return (
        config.configured
        and config.setup_connection_sha256 == connection_revision(config)
        and 0 <= time.time() - config.setup_connection_checked_at <= CHECK_TTL
    )


def snapshot(config: AppConfig) -> dict:
    capabilities = host()
    try:
        sdk = version("reachy-mini")
    except PackageNotFoundError:
        sdk = "unknown"
    dedicated = capabilities.kind in {"raspberry_pi", "jetson"}
    return {
        "required": config.setup_pending,
        "step": config.setup_step if config.setup_step in STEPS else "welcome",
        "entry_route": config.setup_entry_route
        if config.setup_entry_route in {"marketplace", "manual"}
        else "unknown",
        "connection_configured": config.configured,
        "connection_checked": checked(config),
        "speech_tested": False,
        "language": config.language,
        "host": {
            "kind": capabilities.kind,
            "label": capabilities.label,
            "system": capabilities.system,
            "machine": capabilities.machine,
        },
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "sdk": sdk,
        "recommendation": (
            "Keep the robot runtime light; use a separate or cloud AI backend first."
            if capabilities.kind == "raspberry_pi"
            else "Get the base app working first; GPU models are optional and share system RAM."
            if capabilities.kind == "jetson"
            else "Use the existing Reachy setup; local models and acceleration are optional."
        ),
        "update_path": "managed_or_control"
        if dedicated and sys.version_info[:2] in {(3, 11), (3, 12)}
        else "control_or_manual",
        "capability_note": (
            "Host detection is not a hardware acceptance test. "
            "Camera, motion and local models need separate setup."
        ),
    }


def register(app, *, load, save, bridge_factory):
    @app.get("/api/onboarding")
    def status():
        return snapshot(load())

    @app.post("/api/onboarding/progress")
    def progress(request: Progress):
        with config_transaction():
            changes: dict[str, object] = {"setup_step": request.step}
            if request.entry_route is not None:
                changes["setup_entry_route"] = request.entry_route
            config = merge_config(load(), changes)
            save(config)
        return snapshot(config)

    @app.post("/api/onboarding/check")
    def check():
        with config_transaction():
            config = load()
            if not config.configured:
                raise HTTPException(409, "Configure a voice backend in Settings before checking the connection.")
            revision = connection_revision(config)
            check_id = secrets.token_hex(16)
            cleared = merge_config(
                config,
                {
                    "setup_connection_sha256": "",
                    "setup_connection_checked_at": 0.0,
                    "setup_check_id": check_id,
                },
            )
            save(cleared)
        try:
            with httpx.Client(timeout=5.0, follow_redirects=False) as transport:
                bridge = bridge_factory(config, client=transport)
                try:
                    bridge.health()
                    bridge.models()  # authenticated endpoint: an unauthenticated health reply is not enough
                finally:
                    bridge.close()
        except Exception as exc:
            raise HTTPException(
                502, "Connection check failed. Check the backend address, credentials and service, then retry."
            ) from exc
        with config_transaction():
            current = load()
            if connection_revision(current) != revision or current.setup_check_id != check_id:
                raise HTTPException(409, "Settings changed during the check. Check the connection again.")
            verified = merge_config(
                current, {"setup_connection_sha256": revision, "setup_connection_checked_at": time.time()}
            )
            save(verified)
        return snapshot(verified)

    @app.post("/api/onboarding/finish")
    def finish(request: Finish):
        with config_transaction():
            config = load()
            if not checked(config):
                raise HTTPException(409, "Check the saved connection successfully before finishing setup.")
            ready = merge_config(config, {"setup_pending": False, "setup_step": "review"})
            save(ready)
        return {
            **snapshot(ready),
            "ok": True,
            "restart_required": config.setup_pending,
            "note": (
                "Setup saved. In Reachy Mini Control, Stop then Start Homebody to enable wake-word listening. "
                "Camera and motion stay unchanged; speech has not been tested."
            ),
        }
