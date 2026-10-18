"""Household-only application factory, runnable without a Hermes agent installation.

No lifecycle is started on import. Deployment owns binding/auth/provider settings.
Workspace routes are deliberately absent; use the existing shared Bridge for Workspace.
"""
from __future__ import annotations

import argparse
import hmac
import os

from aiohttp import ClientSession, ClientTimeout, web

try:
    from .hermes_reachy_bridge import Bridge
    from .household_mcp import household_mcp
    from .household_robot import robot_callbacks
except ImportError:
    from hermes_reachy_bridge import Bridge
    from household_mcp import household_mcp
    from household_robot import robot_callbacks


class HouseholdBridge(Bridge):
    def __init__(self, *, api_key: str, robot_api_key: str = "", llm=None):
        if not api_key:
            raise ValueError("Household transport requires an explicit bearer credential")
        super().__init__(api_key=api_key, hermes_url="", backends=(), llm=llm)
        if robot_api_key and hmac.compare_digest(api_key, robot_api_key):
            raise ValueError("Robot and MCP caller credentials must be different")
        self.robot_api_key = robot_api_key
        self.household_robots = {}
        # Subscription-backed reasoning uses the Hermes-managed subscription runtime.
        # Direct API providers and direct Realtime/Live speech do not.
        if self.llm.name == "openai-codex" or getattr(self.everyday_llm, "name", "") == "openai-codex":
            raise ValueError("Standalone household requires a direct API provider, not Hermes subscription reasoning")

    def require_robot_auth(self, request):
        if not self.robot_api_key or not hmac.compare_digest(
            request.headers.get("Authorization", ""), "Bearer " + self.robot_api_key
        ):
            raise web.HTTPUnauthorized()

    def require_auth(self, request):
        if self.robot_api_key and request.path != "/mcp":
            self.require_robot_auth(request)
        else:
            super().require_auth(request)

    def init_native_workspace(self):
        self.native_workspace = None
        self.github_repositories = None

    async def start(self, app):
        self.http = ClientSession(timeout=ClientTimeout(total=180, connect=10))

    async def session(self, request):
        # State updates must also retire Everyday on Workspace/Kids/privacy/Stop.
        # Recording an agent profile grants nothing: this app has no agent routes,
        # and household execution requires the conversation profile.
        return await self.broker_session(request)

def create_household_app(*, api_key: str, robot_api_key: str = "", llm=None) -> web.Application:
    bridge = HouseholdBridge(api_key=api_key, robot_api_key=robot_api_key, llm=llm)
    app = web.Application(client_max_size=26 * 1024 * 1024, middlewares=[bridge.kids_latch_middleware])
    app.on_startup.append(bridge.start)
    app.on_cleanup.append(bridge.stop)
    routes = {
        "/v1/agent/session": bridge.session,  # compatibility lifecycle, not an agent call
        "/v1/household/session": bridge.session,
        "/v1/everyday/turn": bridge.everyday_turn,
        "/v1/everyday/look": bridge.everyday_look,
        "/v1/everyday/look/follow-up": bridge.everyday_look_follow_up,
        "/v1/everyday/vision": bridge.everyday_vision,
    }
    for path, handler in routes.items():
        app.router.add_post(path, handler)
    async def endpoint(request):
        return await household_mcp(bridge, request)

    async def callbacks(request):
        return await robot_callbacks(bridge, request)

    app.router.add_get("/v1/household/robot", callbacks)
    app.router.add_post("/mcp", endpoint)
    app.router.add_get("/v1/realtime", bridge.realtime)
    app.router.add_get("/v1/live", bridge.live)
    app.router.add_get("/v1/everyday/session", bridge.everyday_session)
    app.router.add_get("/v1/everyday/capabilities", bridge.everyday_capabilities)
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Household-only MCP and voice service (no Hermes agent)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8644)
    args = parser.parse_args(argv)
    api_key = os.environ.get("HOMEBODY_HOUSEHOLD_TOKEN", "")
    robot_key = os.environ.get("HOMEBODY_HOUSEHOLD_ROBOT_TOKEN", "")
    if not api_key or not robot_key:
        parser.error("Set separate HOMEBODY_HOUSEHOLD_TOKEN and HOMEBODY_HOUSEHOLD_ROBOT_TOKEN")
    web.run_app(create_household_app(api_key=api_key, robot_api_key=robot_key),
                host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
