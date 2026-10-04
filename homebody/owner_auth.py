"""Local owner provisioning and remembered devices. No cloud identity or public claim-first flow.

The HTTPS reverse proxy and firewall are part of this boundary; see docs/owner-access.md.
"""

from __future__ import annotations

import argparse
import contextvars
import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

COOKIE = "__Host-homebody-owner"
SESSION_SECONDS = 30 * 24 * 60 * 60
PAIR_SECONDS = 10 * 60
owner_authenticated = contextvars.ContextVar("owner_authenticated", default=False)
# These have their OWN existing machine authentication. Never broaden by prefix.
MACHINE_ROUTES = {
    ("POST", "/api/presence/signal"),
    ("POST", "/api/initiative/offers"),
    ("POST", "/api/agent/reminder-delivery"),
    ("POST", "/api/camera/snapshot"),
    ("POST", "/api/agent-setup/pair"),
    ("POST", "/mcp"),
    ("GET", "/mcp"),
    ("DELETE", "/mcp"),
}
PUBLIC_FILES = {"/", "/manifest.webmanifest", "/service-worker.js"}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OwnerStore:
    """SQLite transactions arbitrate redemption across CLI and server processes."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pairing (hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, hash TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    csrf TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attempts (at REAL NOT NULL);
            """)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    @property
    def origin(self) -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='origin'").fetchone()
        return row[0] if row else ""

    def provision(self, origin: str) -> str:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or origin != f"https://{parsed.netloc}"
        ):
            raise ValueError("Use an exact HTTPS origin, with no path or trailing slash")
        code = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT value FROM settings WHERE key='origin'").fetchone()
            if current and current[0] != origin:
                raise ValueError("Origin is already provisioned; migrate the database explicitly while stopped")
            db.execute("INSERT OR REPLACE INTO settings VALUES ('origin', ?)", (origin,))
            db.execute("DELETE FROM pairing")
            db.execute("INSERT INTO pairing VALUES (?, ?)", (digest(code), time.time() + PAIR_SECONDS))
        return code

    def redeem(self, code: str, name: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT expires FROM pairing WHERE hash=?", (digest(code),)).fetchone()
            if not row or row[0] <= now:
                raise ValueError("Pairing code is invalid or expired")
            db.execute("DELETE FROM pairing")
            db.execute("DELETE FROM devices WHERE expires<=?", (now,))
            if db.execute("SELECT COUNT(*) FROM devices").fetchone()[0] >= 32:
                raise ValueError("Revoke an old device with the local CLI first")
            db.execute(
                "INSERT INTO devices VALUES (?, ?, ?, ?, ?, ?)",
                (
                    secrets.token_hex(16),
                    digest(token),
                    name,
                    secrets.token_urlsafe(32),
                    now,
                    now + SESSION_SECONDS,
                ),
            )
        return token

    def allow_pair_attempt(self) -> bool:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE at < ?", (now - 60,))
            if db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] >= 10:
                return False
            db.execute("INSERT INTO attempts VALUES (?)", (now,))
        return True

    def session(self, token: str | None) -> dict | None:
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT id, name, csrf, expires FROM devices WHERE hash=? AND expires>?", (digest(token), time.time())
            ).fetchone()
        return dict(row) if row else None

    def devices(self) -> list[dict]:
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id, name, created, expires FROM devices WHERE expires>? ORDER BY created", (time.time(),)
                )
            ]

    def revoke(self, device_id: str) -> None:
        with self.connect() as db:
            if device_id == "all":
                db.execute("DELETE FROM devices")
                db.execute("DELETE FROM pairing")
            else:
                db.execute("DELETE FROM devices WHERE id=?", (device_id,))


def default_owner_path() -> Path:
    # Like config, this is server-side process configuration, never request-controlled.
    from .config import default_config_path

    return Path(os.environ.get("HOMEBODY_OWNER_DB", str(default_config_path().with_name("owner.sqlite3"))))


def _effective_origin(conn: HTTPConnection) -> str:
    """Reconstruct the public origin, honouring X-Forwarded-* proxy headers."""
    host = conn.headers.get("x-forwarded-host") or conn.headers.get("host", "")
    scheme = conn.headers.get("x-forwarded-proto") or conn.url.scheme
    if "," in host:
        host = host.split(",")[0].strip()
    if "," in scheme:
        scheme = scheme.split(",")[0].strip()
    return f"{scheme}://{host}"


class OwnerBoundary:
    def __init__(self, app, store: OwnerStore):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        conn = HTTPConnection(scope)
        path = scope["path"]
        method = scope.get("method", "WEBSOCKET")
        # Static shell, non-secret agent sources, the pairing/session endpoints, and
        # the status endpoint (needed for the dashboard to load) do not grant authority.
        public = method in {"GET", "HEAD"} and (
            path in PUBLIC_FILES or path.startswith("/static/") or path.startswith("/agent-setup/")
        )
        machine = (method, path) in MACHINE_ROUTES
        owner_public = (
            (method == "POST" and path == "/api/owner/pair")
            or (method == "GET" and path in {"/api/owner/session", "/api/status", "/api/agent-setup/status"})
            or (method == "OPTIONS" and path == "/api/owner/pair")
        )
        if public or machine:
            return await self.app(scope, receive, send)
        origin = self.store.origin
        status, detail = 0, ""
        session = None
        if not origin:
            status, detail = 503, "Owner access needs local provisioning; see docs/owner-access.md"
        elif owner_public:
            # Public endpoints skip the origin gate but still honour an existing session cookie.
            session = self.store.session(conn.cookies.get(COOKIE))
        elif _effective_origin(conn) != origin:
            status, detail = 403, "Use the configured private HTTPS address"
        elif conn.headers.get("origin") not in {None, origin}:
            status, detail = 403, "Foreign browser origin rejected"
        else:
            session = self.store.session(conn.cookies.get(COOKIE))
            if not session and not owner_public:
                status, detail = 401, "Pair this device as Owner first"
            elif method not in {"GET", "HEAD", "OPTIONS"}:
                if conn.headers.get("origin") != origin:
                    status, detail = 403, "Same-origin request required"
                elif (
                    not owner_public
                    and method != "WEBSOCKET"
                    and not secrets.compare_digest(
                        conn.headers.get("x-homebody-csrf", ""), session["csrf"] if session else ""
                    )
                ):
                    status, detail = 403, "Refresh the page before trying again (CSRF check)"
        if status:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
            else:
                await JSONResponse({"detail": detail}, status_code=status, headers={"Cache-Control": "no-store"})(
                    scope, receive, send
                )
            return
        scope.setdefault("state", {})["owner"] = session
        token = owner_authenticated.set(bool(session))

        async def private_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [(k, v) for k, v in message["headers"] if k.lower() != b"cache-control"]
                message["headers"].append((b"cache-control", b"no-store"))
            await send(message)

        try:
            await self.app(scope, receive, private_send)
        finally:
            owner_authenticated.reset(token)


class PairRequest(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    name: str = Field(default="Owner device", min_length=1, max_length=64)


class RevokeRequest(BaseModel):
    device_id: str = Field(pattern=r"^(all|[0-9a-f]{32})$")


def install_owner_auth(app: FastAPI, store: OwnerStore) -> None:
    app.state.owner_store = store
    app.add_middleware(OwnerBoundary, store=store)

    @app.get("/api/owner/session")
    def session(request: Request):
        owner = getattr(request.state, "owner", None)
        return {
            "owner": bool(owner),
            **(
                {"device_id": owner["id"], "name": owner["name"], "csrf": owner["csrf"], "expires": owner["expires"]}
                if owner
                else {}
            ),
        }

    @app.post("/api/owner/pair")
    def pair(payload: PairRequest):
        if not store.allow_pair_attempt():
            return JSONResponse(
                {"detail": "Too many pairing attempts; wait one minute"},
                status_code=429,
                headers={"Retry-After": "60"},
            )
        try:
            token = store.redeem(payload.code, payload.name)
        except ValueError:
            return JSONResponse({"detail": "Pairing code is invalid or expired"}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite="strict")
        return response

    @app.get("/api/owner/devices")
    def devices():
        return {"devices": store.devices()}

    @app.post("/api/owner/revoke")
    def revoke(payload: RevokeRequest):
        store.revoke(payload.device_id)
        return {"ok": True}

    @app.post("/api/owner/logout")
    def logout(request: Request):
        store.revoke(request.state.owner["id"])
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
        return response


def main() -> None:
    parser = argparse.ArgumentParser(description="Homebody local owner-device provisioning (run as the app user)")
    parser.add_argument("--db", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    pairing = sub.add_parser("pair", help="Print a single-use code valid for ten minutes; does not start hardware")
    pairing.add_argument("--origin", required=True)
    sub.add_parser("list", help="List device IDs; no credentials")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("device_id", help="Device ID or 'all' (also invalidates pending pairing)")
    args = parser.parse_args()
    store = OwnerStore(args.db or default_owner_path())
    if args.command == "pair":
        print(store.provision(args.origin))
    elif args.command == "list":
        for device in store.devices():
            print(device["id"], repr(device["name"]), int(device["expires"]))
    else:
        store.revoke(args.device_id)


if __name__ == "__main__":
    main()
