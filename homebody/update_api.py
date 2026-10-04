"""Owner-only update APIs. The only machine exception is token-authenticated health, not control."""
from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .update_worker import SystemdSlots, config
from .updates import (
    Store,
    atomic_json,
    compatible_host,
    configured_path,
    discover,
    installed_identity,
    require_safe,
)


class InstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str = Field(max_length=16)
    release_id: StrictInt


class LaterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release_id: StrictInt


class AcknowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class UpdateService:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()

    def store(self) -> Store:
        path = configured_path()
        return Store(Path(config(path)["state"])) if path else Store(self.root)

    def supported(self) -> tuple[bool, str]:
        try:
            path = configured_path()
            if path is None:
                raise ValueError("One-time external updater setup is required (unsupported install topology)")
            value = config(path)
            if not compatible_host():
                raise ValueError("Supported topology requires Linux and Python 3.11")
            if Path(__file__).resolve() != (Path(value["current"]).resolve() / "homebody/update_api.py"):
                raise ValueError("Running app is not the configured managed slot")
            heartbeat = json.loads((Path(value["state"]) / "worker.json").read_text())
            if not 0 <= time.time() - heartbeat["heartbeat"] < 15:
                raise ValueError("External updater is unavailable; check its service")
            return True, ""
        except (ValueError, OSError, KeyError) as exc:
            return False, str(exc)

    def status(self) -> dict:
        supported, reason = self.supported()
        try:
            state = self.store().read()
            cache = self.root / "catalog.json"
            catalog = json.loads(cache.read_text()) if cache.exists() else {
                "available": None, "notes": [], "checked_at": None, "error": ""}
        except (OSError, ValueError) as exc:
            state = {"job": None, "history": []}
            catalog = {"available": None, "notes": [], "error": str(exc)}
        return {"current": installed_identity(), "supported": supported, "reason": reason, **catalog, **state}

    def check(self) -> dict:
        with self.lock:
            path = self.root / "catalog.json"
            previous = json.loads(path.read_text()) if path.exists() else {}
            catalog = {"available": None, "notes": [], "checked_at": time.time(), "error": "",
                       "deferred_release_id": previous.get("deferred_release_id")}
            try:
                catalog.update(discover(installed_identity()["version"]))
            except Exception as exc:
                # Clear stale offers on every failure; never quietly install an old unverified preview.
                catalog["error"] = f"Release check failed: {type(exc).__name__}: {str(exc)[:200]}"
            atomic_json(self.root / "catalog.json", catalog)
            return self.status()

    def install(self, request: InstallRequest, runtime) -> dict:
        with self.lock:
            supported, reason = self.supported()
            if not supported:
                raise ValueError(reason)
            if request.confirm != "INSTALL" or runtime is None:
                raise ValueError("Explicit INSTALL confirmation and a ready runtime are required")
            preview = self.status().get("available")
            if not preview or preview["release_id"] != request.release_id:
                raise ValueError("Preview this exact release before confirming install")
            fresh = discover(installed_identity()["version"])
            if fresh["available"] != preview:
                raise ValueError("Release changed; check and preview again")
            value = config(configured_path())
            SystemdSlots(value).preflight()
            return runtime.reserve_update(lambda: self.store().enqueue(
                preview, installed_identity()["version"], fresh["notes"]))


def register(app, runtime, root: Path) -> None:
    service = UpdateService(root)
    app.state.updates = service

    @app.get("/api/updates")
    def status():
        return service.status()

    @app.post("/api/updates/check")
    def check():
        return service.check()

    @app.post("/api/updates/install")
    def install(body: InstallRequest):
        try:
            return service.install(body, runtime())
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/updates/later")
    def later(body: LaterRequest):
        with service.lock:
            path = service.root / "catalog.json"
            catalog = json.loads(path.read_text()) if path.exists() else {}
            if not catalog.get("available") or catalog["available"]["release_id"] != body.release_id:
                raise HTTPException(409, "Release is no longer available")
            catalog["deferred_release_id"] = body.release_id
            atomic_json(path, catalog)
            return service.status()

    @app.post("/api/updates/acknowledge")
    def acknowledge(body: AcknowledgeRequest):
        service.store().acknowledge(body.job_id)
        return service.status()

    @app.get("/api/updates/health")
    def health(request: Request):
        path = configured_path()
        try:
            value = config(path) if path else {}
            token = value.get("health_token", "")
        except (OSError, ValueError):
            token = ""
        if not token or not secrets.compare_digest(request.headers.get("authorization", ""), f"Bearer {token}"):
            raise HTTPException(401, "Unauthorized")
        healthy = False
        active = runtime()
        if active is not None:
            try:
                require_safe(active.status())
                healthy = active.control_ready and not active.stop_event.is_set()
            except (ValueError, AttributeError):
                pass
        return {"app": "homebody", "release": installed_identity(), "healthy": healthy}
