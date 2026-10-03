"""The internet-facing listener for hosted agents: ``/mcp`` with OAuth only, plus the OAuth endpoints.

Hosted agents reach Homebody through an HTTPS tunnel. Earlier, those routes shared the dashboard
port and a ``Host`` header check kept the dashboard hidden; a proxy that rewrites ``Host`` (nginx's
default ``proxy_pass``, ``ngrok --host-header=rewrite``, ``cloudflared httpHostHeader``) silently
turned that check off. Here the public routes get their own small app on their own port, so a
tunnel pointed at it can reach nothing else, whatever headers the proxy sends, and the never-expiring
static MCP token is not accepted at all.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import AppConfig
from .mcp_oauth import CONSENT_HEADERS, OAuthError, OAuthServer, consent_page

_LOGGER = logging.getLogger(__name__)

McpAnswer = Callable[[Request, AppConfig], Awaitable[Response]]


def public_listener_wanted(config: AppConfig) -> tuple[str, int] | None:
    """The address to listen on, or None when hosted-agent sign-in is off."""
    if config.mcp_enabled and config.mcp_oauth_enabled and config.mcp_public_url:
        return config.mcp_public_bind, int(config.mcp_public_port)
    return None


def build_public_app(
    *,
    oauth: OAuthServer,
    config_loader: Callable[[], AppConfig],
    answer_mcp: McpAnswer,
    same_origin: Callable[[str, Request], bool],
) -> FastAPI:
    """Only the hosted-agent routes; every other path is FastAPI's plain 404."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def oauth_config() -> AppConfig:
        config = config_loader()
        if public_listener_wanted(config) is None:
            raise HTTPException(status_code=404, detail="Not found")
        return config

    def oauth_error(exc: OAuthError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=exc.body(), headers={"Cache-Control": "no-store"})

    async def read_form(request: Request, limit: int) -> dict[str, str]:
        body = await request.body()
        return {key: values[0] for key, values in parse_qs(body.decode("utf-8", "replace")[:limit]).items()}

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> Response:
        config = oauth_config()
        origin = request.headers.get("origin", "")
        if origin and not same_origin(origin, request):
            return JSONResponse(status_code=403, content={"detail": "Cross-origin MCP requests are not allowed"})
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        resource = OAuthServer.resource_url(config.mcp_public_url)
        # OAuth access tokens only: the static token is for the home network and never works here.
        if scheme.lower() != "bearer" or not oauth.validate_access_token(token.strip(), resource=resource):
            metadata = f"{config.mcp_public_url}/.well-known/oauth-protected-resource/mcp"
            return JSONResponse(
                status_code=401,
                content={"detail": "Sign in with OAuth to use Homebody"},
                headers={"WWW-Authenticate": f'Bearer realm="homebody", resource_metadata="{metadata}"'},
            )
        return await answer_mcp(request, config)

    @app.get("/mcp")
    @app.delete("/mcp")
    def mcp_no_stream() -> Response:
        oauth_config()
        # Stateless server: no server-initiated SSE stream and no sessions to delete.
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    def resource_metadata() -> dict[str, object]:
        return OAuthServer.protected_resource_metadata(oauth_config().mcp_public_url)

    @app.get("/.well-known/oauth-authorization-server")
    def server_metadata() -> dict[str, object]:
        return OAuthServer.authorization_server_metadata(oauth_config().mcp_public_url)

    @app.post("/oauth/register")
    async def register(request: Request) -> JSONResponse:
        oauth_config()
        body = await request.body()
        if len(body) > 16 * 1024:
            return JSONResponse(status_code=413, content={"error": "invalid_client_metadata"})
        try:
            payload = json.loads(body or b"{}")
            return JSONResponse(status_code=201, content=oauth.register(payload))
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "invalid_client_metadata"})
        except OAuthError as exc:
            return oauth_error(exc)

    @app.get("/oauth/authorize")
    def authorize(request: Request) -> HTMLResponse:
        config = oauth_config()
        params = dict(request.query_params.items())
        try:
            pending = oauth.start_authorization(params, resource=OAuthServer.resource_url(config.mcp_public_url))
        except OAuthError as exc:
            # Never redirect to an unverified URI; show the problem instead.
            return HTMLResponse(consent_page(None, error=exc.description), status_code=400, headers=CONSENT_HEADERS)
        return HTMLResponse(consent_page(pending, vision=config.mcp_vision_enabled), headers=CONSENT_HEADERS)

    @app.post("/oauth/authorize")
    async def authorize_decision(request: Request) -> Response:
        config = oauth_config()
        form = await read_form(request, 4096)
        pending_id = form.get("pending_id", "")
        if form.get("action") == "deny":
            target = oauth.deny(pending_id)
            if target is None:
                return HTMLResponse(consent_page(None), status_code=400, headers=CONSENT_HEADERS)
            return RedirectResponse(target, status_code=303, headers=CONSENT_HEADERS)
        try:
            target = oauth.approve(pending_id, form.get("approval_code", ""))
        except OAuthError as exc:
            pending = oauth.pending(pending_id)
            html = consent_page(pending, error=exc.description, vision=config.mcp_vision_enabled)
            return HTMLResponse(html, status_code=400, headers=CONSENT_HEADERS)
        return RedirectResponse(target, status_code=303, headers=CONSENT_HEADERS)

    @app.post("/oauth/token")
    async def token(request: Request) -> JSONResponse:
        config = oauth_config()
        form = await read_form(request, 8192)
        try:
            tokens = oauth.exchange(form, resource=OAuthServer.resource_url(config.mcp_public_url))
        except OAuthError as exc:
            return oauth_error(exc)
        return JSONResponse(tokens, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.post("/oauth/revoke")
    async def revoke(request: Request) -> Response:
        oauth_config()
        form = await read_form(request, 4096)
        oauth.revoke_token(form.get("token", ""))
        return Response(status_code=200)

    return app


class PublicAgentListener:
    """Run the public app on its own address while hosted-agent sign-in is on."""

    def __init__(self, app_factory: Callable[[], FastAPI]) -> None:
        self._app_factory = app_factory
        self._lock = threading.Lock()
        self._server: object | None = None
        self._thread: threading.Thread | None = None
        self._address: tuple[str, int] | None = None
        self._error = ""

    def sync(self, config: AppConfig) -> None:
        """Start, stop or move the listener to match the settings. Never raises into the app."""
        wanted = public_listener_wanted(config)
        with self._lock:
            if wanted is None:
                self._error = ""
            running = self._thread is not None and self._thread.is_alive()
            if running and wanted == self._address:
                return
            self._stop_unlocked()
            if wanted is not None:
                self._start_unlocked(*wanted)

    def stop(self) -> None:
        with self._lock:
            self._stop_unlocked()

    close = stop

    def status(self) -> dict[str, object]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            host, port = self._address or ("", 0)
            return {"running": running, "address": f"{host}:{port}" if running else "", "error": self._error}

    def _start_unlocked(self, host: str, port: int) -> None:
        try:
            import uvicorn  # noqa: PLC0415 - shipped with reachy-mini; only needed when sign-in is on
        except ImportError:
            self._error = "uvicorn is not installed, so hosted-agent sign-in cannot start"
            _LOGGER.warning(self._error)
            return
        config = uvicorn.Config(
            self._app_factory(),
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
            server_header=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, name="homebody-agent-listener", daemon=True)
        thread.start()
        # Wait for the socket, so a busy port is reported instead of failing silently.
        for _ in range(100):
            if getattr(server, "started", False) or not thread.is_alive():
                break
            thread.join(timeout=0.05)
        if not getattr(server, "started", False):
            server.should_exit = True
            thread.join(timeout=2.0)
            self._error = f"Could not listen on {host}:{port} for hosted agents; is the port in use?"
            _LOGGER.warning(self._error)
            return
        self._server, self._thread, self._address, self._error = server, thread, (host, port), ""
        _LOGGER.info("Hosted-agent sign-in listening on %s:%s", host, port)

    def _stop_unlocked(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = self._address = None
        if server is not None:
            server.should_exit = True  # type: ignore[attr-defined]
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
