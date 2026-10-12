"""Native Hermes session API, with Workspace subscription/model/reasoning locked.

Uses the real Hermes session DB, runs, tools, approvals and event stream. Separate
from the primary gateway so its model/fallback settings and ongoing work stay intact.
"""

import asyncio
import inspect
import os
import signal
import sys
from pathlib import Path

source = Path(os.environ["HOMEBODY_HERMES_SOURCE"]).resolve()
sys.path.insert(0, str(source))
import hermes_bootstrap  # noqa: E402, F401

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.api_server import APIServerAdapter  # noqa: E402
from native_subscription_policy import locked_workspace_options  # noqa: E402

SESSION = os.environ["HOMEBODY_WORKSPACE_SESSION"]
MODEL = os.environ["HOMEBODY_WORKSPACE_MODEL"]
ROOT = Path(os.environ["HOMEBODY_WORKSPACE_ROOT"]).resolve()
if not (ROOT / ".git").exists():
    raise RuntimeError("The registered Workspace repository is unavailable")
os.chdir(ROOT)
os.environ["TERMINAL_CWD"] = str(ROOT)


class SubscriptionWorkspaceAPI(APIServerAdapter):
    def _create_agent(self, *args, **kwargs):
        from agent.transports.codex import _codex_efforts_for_route

        if "none" not in _codex_efforts_for_route(
            MODEL, "https://chatgpt.com/backend-api/codex", is_codex_backend=True
        ):
            raise RuntimeError("Workspace model does not support reasoning off")
        # The native runs route does not itself confirm Browser runtime locks.
        # Pin this listener before the real Hermes agent is created: the official
        # confirmed-lock contract disables the entire fallback chain.
        options = inspect.signature(super()._create_agent).bind(*args, **kwargs).arguments
        return super()._create_agent(**locked_workspace_options(options, session_id=SESSION, model=MODEL))


async def serve():
    api = SubscriptionWorkspaceAPI(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": int(os.environ["HOMEBODY_WORKSPACE_PORT"]),
                "key": os.environ["HOMEBODY_NATIVE_API_KEY"],
                "model_routes": {MODEL: {"model": MODEL, "provider": "openai-codex"}},
                "direct_model_requests": True,
            },
        )
    )
    if not await api.connect():
        raise RuntimeError("Native Workspace listener did not start")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await stopped.wait()
    finally:
        await api.disconnect()


if __name__ == "__main__":
    asyncio.run(serve())
