"""Same-origin, owner-authenticated WebRTC signaling relay.

This protects signaling, not direct daemon ports or already-negotiated peer media.
The deployment firewall MUST prevent clients reaching the upstream directly.
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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
            except RuntimeError:
                pass
