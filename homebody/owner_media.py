"""Same-origin, owner-authenticated WebRTC signaling relay.

This protects signaling, not direct daemon ports or already-negotiated peer media.
The deployment firewall MUST prevent clients reaching the upstream directly.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .config import load_config
from .owner_auth import COOKIE


def install_owner_media(app: FastAPI, runtime_getter) -> None:
    def policy_allows() -> bool:
        runtime = runtime_getter()
        if runtime is None or not runtime.control_ready or runtime.kids_controls_locked:
            return False
        status = runtime.status()
        kids = status.get("kids_mode", {})
        return (
            load_config().camera_feed_enabled
            and status.get("power_mode") == "awake"
            and not kids.get("active")
            and not kids.get("locked")
        )

    frame_lock = threading.Lock()

    def upstream_url() -> str:
        upstream = os.environ.get("HOMEBODY_SIGNALING_UPSTREAM", "ws://127.0.0.1:8443")
        parsed = urlsplit(upstream)
        if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Camera signaling must use the local daemon")
        return upstream

    @app.get("/api/camera/connectivity")
    async def connectivity():
        # Pairing verifies transport readiness, never starts a media session.
        reachable = False
        try:
            async with connect(upstream_url(), max_size=65536, open_timeout=3) as remote:
                welcome = json.loads(await asyncio.wait_for(remote.recv(), timeout=3))
                reachable = welcome.get("type") == "welcome"
        except (OSError, ConnectionClosed, TimeoutError, ValueError):
            pass
        return {
            "signaling_ready": reachable,
            "media_verified": False,
            "https_fallback": True,
            "camera_started": False,
            "detail": (
                "Camera transport ready. Press Start to verify live video; pairing does not activate the camera."
                if reachable
                else "Camera signaling unavailable. HTTPS viewing remains available while Awake and opted in."
            ),
        }

    @app.post("/api/camera/frame")
    def live_frame(request: Request):
        # OwnerBoundary enforces origin, owner session and CSRF. This is live-view
        # consent, not AI/snapshot consent, and never calls a vision model.
        if not policy_allows():
            raise HTTPException(403, "Live camera requires opt-in, Awake and unlocked adult controls")
        if not frame_lock.acquire(blocking=False):
            raise HTTPException(429, "Camera frame request already in progress")
        try:
            runtime = runtime_getter()
            capture = getattr(getattr(getattr(runtime, "robot", None), "media", None), "get_frame_jpeg", None)
            if not callable(capture):
                raise HTTPException(503, "Local camera capture unavailable")
            jpeg = capture()
            if not app.state.owner_store.session(request.cookies.get(COOKIE)):
                raise HTTPException(401, "Owner session ended")
            if not policy_allows():
                raise HTTPException(403, "Live camera permission ended")
            if not jpeg:
                raise HTTPException(503, "Camera frame not ready")
            if not isinstance(jpeg, (bytes, bytearray, memoryview)) or len(jpeg) > 1_000_000:
                raise HTTPException(503, "Camera frame exceeds the live-view safety limit")
            return Response(bytes(jpeg), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
        finally:
            frame_lock.release()

    @app.websocket("/api/camera/signaling")
    async def signaling(socket: WebSocket):
        store = app.state.owner_store
        token = socket.cookies.get(COOKIE)
        if not store.session(token) or not policy_allows():
            await socket.close(code=4403)
            return
        # Never proxy caller-supplied URLs or forward cookies/authorization upstream.
        upstream = os.environ.get("HOMEBODY_SIGNALING_UPSTREAM", "ws://127.0.0.1:8443")
        parsed = urlsplit(upstream)
        if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            await socket.close(code=1011)
            return
        await socket.accept()
        tasks = []
        try:
            async with connect(upstream, max_size=1024 * 1024, open_timeout=5) as remote:

                async def to_remote():
                    while True:
                        message = await socket.receive_text()
                        if len(message) > 1024 * 1024:
                            return
                        await remote.send(message)

                async def to_browser():
                    async for message in remote:
                        if isinstance(message, str):
                            await socket.send_text(message)

                async def validity():
                    while True:
                        await asyncio.sleep(1)
                        if not store.session(token) or not policy_allows():
                            return

                tasks = [asyncio.create_task(coro()) for coro in (to_remote, to_browser, validity)]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, ConnectionClosed, WebSocketDisconnect, TimeoutError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await socket.close(code=1000)
            except (RuntimeError, WebSocketDisconnect):
                pass
