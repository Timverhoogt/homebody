"""OAuth 2.1 for Homebody's MCP endpoint, so hosted agents can connect with the owner's consent.

Hosted agents such as ChatGPT dots or Grok Bot run in their vendor's cloud. The MCP authorization
spec asks them to discover an authorization server, register themselves, and send the owner
through a consent screen. Homebody is both the resource server (``/mcp``) and a minimal
authorization server:

* discovery: Protected Resource Metadata (RFC 9728) and Authorization Server Metadata (RFC 8414);
* registration: Dynamic Client Registration (RFC 7591) for public clients only;
* authorization code flow with mandatory PKCE S256, exact redirect-URI matching, and a ``resource``
  indicator (RFC 8707) that binds every token to this ``/mcp`` endpoint;
* consent: Homebody has no user accounts. The owner approves each specific request in Settings on
  the home network (which needs the bridge API key), where the agent's name, return address and
  a short match code are shown by Homebody itself rather than by the agent. The public consent
  page only shows the same match code and waits; nothing typed there grants access;
* tokens: one-hour access tokens and 30-day rotating refresh tokens. Reusing an old refresh token
  revokes the whole grant. Only SHA-256 hashes of codes and tokens are kept.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_LOGGER = logging.getLogger(__name__)

SCOPE = "homebody"
ACCESS_TOKEN_SECONDS = 3600
REFRESH_TOKEN_SECONDS = 30 * 86400
AUTH_CODE_SECONDS = 300
PENDING_SECONDS = 600
MAX_CLIENTS = 20
MAX_REDIRECT_URIS = 5
# Registration and /authorize are open to the internet, so everything they store is bounded.
MAX_PENDING = 50
MAX_PENDING_PER_CLIENT = 5
UNAPPROVED_CLIENT_SECONDS = 3600
_MATCH_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion
_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class OAuthError(Exception):
    """An OAuth protocol error with its RFC error code."""

    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status

    def body(self) -> dict[str, str]:
        return {"error": self.error, "error_description": self.description}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def valid_public_url(url: str) -> bool:
    """OAuth issuers must be HTTPS; plain HTTP is allowed only for localhost testing."""
    parsed = urlparse(url)
    if not parsed.netloc or parsed.query or parsed.fragment:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}


def _valid_redirect_uri(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.fragment or not parsed.netloc:
        return False
    if parsed.scheme == "https":
        return True
    # Native and CLI clients use loopback redirects (RFC 8252).
    return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _valid_client(record: object) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("name"), str)
        and _is_number(record.get("created_at"))
        and isinstance(record.get("redirect_uris"), list)
        and 1 <= len(record["redirect_uris"]) <= MAX_REDIRECT_URIS
        and all(isinstance(uri, str) and _valid_redirect_uri(uri) for uri in record["redirect_uris"])
        and (record.get("approved_at") is None or _is_number(record.get("approved_at")))
    )


def _valid_grant(record: object) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("client_id"), str)
        and isinstance(record.get("resource"), str)
        and isinstance(record.get("refresh"), str)
        and _is_number(record.get("refresh_expires_at", 0))
        and isinstance(record.get("used", []), list)
        and all(isinstance(item, str) for item in record.get("used", []))
    )


def _valid_access(record: object) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("grant_id"), str)
        and isinstance(record.get("resource"), str)
        and _is_number(record.get("expires_at"))
    )


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    pending_id: str
    client_id: str
    client_name: str
    redirect_uri: str
    state: str
    code_challenge: str
    resource: str
    expires_at: float
    match_code: str = ""
    created_at: float = 0.0


class OAuthServer:
    """Thread-safe OAuth state with JSON persistence for clients and grants."""

    def __init__(
        self,
        path: Path | None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._clock = clock
        self._lock = threading.RLock()
        self._clients: dict[str, dict[str, Any]] = {}
        self._grants: dict[str, dict[str, Any]] = {}
        self._access: dict[str, dict[str, Any]] = {}
        self._codes: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, PendingAuthorization] = {}
        # Owner decisions made in Settings, keyed by pending request id: "approved" or "denied".
        self._decisions: dict[str, str] = {}
        self._load()

    # -- discovery ------------------------------------------------------------------------------

    @staticmethod
    def resource_url(base: str) -> str:
        return f"{base.rstrip('/')}/mcp"

    @classmethod
    def protected_resource_metadata(cls, base: str) -> dict[str, Any]:
        base = base.rstrip("/")
        return {
            "resource": cls.resource_url(base),
            "authorization_servers": [base],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": "Homebody (Reachy Mini)",
        }

    @staticmethod
    def authorization_server_metadata(base: str) -> dict[str, Any]:
        base = base.rstrip("/")
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "revocation_endpoint": f"{base}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE],
        }

    # -- registration ---------------------------------------------------------------------------

    def register(self, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise OAuthError("invalid_client_metadata", "Registration must be a JSON object")
        uris = payload.get("redirect_uris")
        if (
            not isinstance(uris, list)
            or not 1 <= len(uris) <= MAX_REDIRECT_URIS
            or not all(isinstance(uri, str) and len(uri) <= 512 and _valid_redirect_uri(uri) for uri in uris)
        ):
            raise OAuthError("invalid_redirect_uri", "redirect_uris must be 1-5 https (or loopback http) URLs")
        method = payload.get("token_endpoint_auth_method", "none")
        if method != "none":
            raise OAuthError("invalid_client_metadata", "Only public clients (token_endpoint_auth_method none)")
        grant_types = payload.get("grant_types", ["authorization_code", "refresh_token"])
        if not isinstance(grant_types, list) or not set(grant_types) <= {"authorization_code", "refresh_token"}:
            raise OAuthError("invalid_client_metadata", "Unsupported grant_types")
        name = " ".join(str(payload.get("client_name") or "Unnamed agent").split())[:80] or "Unnamed agent"
        with self._lock:
            self._make_room_for_client_unlocked()
            client_id = "hbc_" + secrets.token_urlsafe(16)
            issued = int(self._clock())
            self._clients[client_id] = {"name": name, "redirect_uris": list(uris), "created_at": issued}
            self._save_unlocked()
        _LOGGER.info("MCP OAuth client registered: %s", name)
        return {
            "client_id": client_id,
            "client_id_issued_at": issued,
            "client_name": name,
            "redirect_uris": list(uris),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        }

    # -- authorization --------------------------------------------------------------------------

    def start_authorization(self, params: dict[str, str], *, resource: str) -> PendingAuthorization:
        """Validate an /authorize request. Raises OAuthError; never redirects to an unknown URI."""
        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                raise OAuthError("invalid_client", "Unknown client_id; register first", 400)
            if redirect_uri not in client["redirect_uris"]:
                raise OAuthError("invalid_request", "redirect_uri does not match the registration", 400)
            name = str(client["name"])
        if params.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "Only response_type=code is supported")
        challenge_ok = _CHALLENGE_RE.fullmatch(params.get("code_challenge", ""))
        if params.get("code_challenge_method") != "S256" or not challenge_ok:
            raise OAuthError("invalid_request", "PKCE with code_challenge_method=S256 is required")
        requested = params.get("resource", "")
        if requested and requested.rstrip("/") != resource:
            raise OAuthError("invalid_target", "This server only issues tokens for its own MCP endpoint")
        scope = params.get("scope", SCOPE)
        if scope and set(scope.split()) - {SCOPE}:
            raise OAuthError("invalid_scope", f"Only the '{SCOPE}' scope is available")
        pending = PendingAuthorization(
            pending_id=secrets.token_urlsafe(24),
            client_id=client_id,
            client_name=name,
            redirect_uri=redirect_uri,
            state=params.get("state", "")[:512],
            code_challenge=params["code_challenge"],
            resource=resource,
            expires_at=self._clock() + PENDING_SECONDS,
            match_code="".join(secrets.choice(_MATCH_ALPHABET) for _ in range(4)),
            created_at=self._clock(),
        )
        with self._lock:
            self._prune_unlocked()
            if client_id not in self._clients:
                raise OAuthError("invalid_client", "Unknown client_id; register first", 400)
            # A client may hold only a few open requests, and the whole store is capped; the oldest
            # request goes first, so a flood can delay a real connection but never fill memory.
            own = sorted(
                (item for item in self._pending.values() if item.client_id == client_id),
                key=lambda item: item.expires_at,
            )
            for stale in own[: max(0, len(own) - MAX_PENDING_PER_CLIENT + 1)]:
                self._pending.pop(stale.pending_id, None)
            while len(self._pending) >= MAX_PENDING:
                oldest = min(self._pending.values(), key=lambda item: item.expires_at)
                self._pending.pop(oldest.pending_id, None)
            self._pending[pending.pending_id] = pending
        return pending

    def pending(self, pending_id: str) -> PendingAuthorization | None:
        with self._lock:
            self._prune_unlocked()
            return self._pending.get(pending_id)

    def pending_for_owner(self) -> list[dict[str, Any]]:
        """Open requests for the Settings page; Homebody, not the agent, vouches for these details."""
        with self._lock:
            self._prune_unlocked()
            now = self._clock()
            items = []
            for pending in sorted(self._pending.values(), key=lambda item: item.created_at):
                client = self._clients.get(pending.client_id, {})
                items.append(
                    {
                        "id": pending.pending_id,
                        "agent": pending.client_name,
                        "returns_to": urlparse(pending.redirect_uri).netloc,
                        "match_code": pending.match_code,
                        "age_seconds": int(now - pending.created_at),
                        "registered_seconds_ago": int(now - float(client.get("created_at", now))),
                        "approved": self._decisions.get(pending.pending_id) == "approved",
                    }
                )
            return items

    def owner_decide(self, pending_id: str, *, approve: bool) -> None:
        """Owner-side, from Settings: approve or deny one specific request."""
        with self._lock:
            self._prune_unlocked()
            pending = self._pending.get(pending_id)
            if pending is None or pending.client_id not in self._clients:
                raise OAuthError("access_denied", "That request expired or was withdrawn")
            self._decisions[pending_id] = "approved" if approve else "denied"
        _LOGGER.info("MCP OAuth request from %s %s", pending.client_name, "approved" if approve else "denied")

    def complete(self, pending_id: str) -> str:
        """Agent-side "Continue": the redirect URL once the owner has decided. Raises while waiting."""
        with self._lock:
            self._prune_unlocked()
            pending = self._pending.get(pending_id)
            if pending is None:
                raise OAuthError("access_denied", "This request expired. Start connecting again from your agent.")
            client = self._clients.get(pending.client_id)
            if client is None:
                self._pending.pop(pending_id, None)
                raise OAuthError("access_denied", "This agent's registration expired. Connect again from your agent.")
            decision = self._decisions.get(pending_id)
            if decision is None:
                raise OAuthError(
                    "authorization_pending",
                    "Not approved yet. Approve this request in Homebody Settings, then press Continue.",
                )
            self._pending.pop(pending_id, None)
            self._decisions.pop(pending_id, None)
            if decision != "approved":
                return self._redirect(
                    pending.redirect_uri,
                    {"error": "access_denied", "error_description": "The owner declined", "state": pending.state},
                )
            # Approved agents are never evicted to make room for new registrations.
            client["approved_at"] = int(self._clock())
            self._save_unlocked()
            code = secrets.token_urlsafe(32)
            self._codes[_digest(code)] = {
                "client_id": pending.client_id,
                "redirect_uri": pending.redirect_uri,
                "code_challenge": pending.code_challenge,
                "resource": pending.resource,
                "expires_at": self._clock() + AUTH_CODE_SECONDS,
            }
        _LOGGER.info("MCP OAuth access granted to %s", pending.client_name)
        return self._redirect(pending.redirect_uri, {"code": code, "state": pending.state})

    def deny(self, pending_id: str) -> str | None:
        with self._lock:
            pending = self._pending.pop(pending_id, None)
            self._decisions.pop(pending_id, None)
        if pending is None:
            return None
        return self._redirect(
            pending.redirect_uri,
            {"error": "access_denied", "error_description": "The owner declined", "state": pending.state},
        )

    @staticmethod
    def _redirect(uri: str, params: dict[str, str]) -> str:
        from urllib.parse import urlencode  # noqa: PLC0415

        query = urlencode({key: value for key, value in params.items() if value})
        return f"{uri}{'&' if '?' in uri else '?'}{query}"

    # -- tokens ---------------------------------------------------------------------------------

    def exchange(self, form: dict[str, str], *, resource: str) -> dict[str, Any]:
        grant_type = form.get("grant_type", "")
        if grant_type == "authorization_code":
            return self._exchange_code(form, resource=resource)
        if grant_type == "refresh_token":
            return self._refresh(form, resource=resource)
        raise OAuthError("unsupported_grant_type", "Use authorization_code or refresh_token")

    def _exchange_code(self, form: dict[str, str], *, resource: str) -> dict[str, Any]:
        verifier = form.get("code_verifier", "")
        if not _VERIFIER_RE.fullmatch(verifier):
            raise OAuthError("invalid_request", "A valid PKCE code_verifier is required")
        with self._lock:
            self._prune_unlocked()
            record = self._codes.pop(_digest(form.get("code", "")), None)  # single use, even on failure
            if record is None:
                raise OAuthError("invalid_grant", "The authorization code is invalid or expired")
            if record["client_id"] != form.get("client_id") or record["redirect_uri"] != form.get("redirect_uri"):
                raise OAuthError("invalid_grant", "client_id or redirect_uri does not match the authorization")
            if not hmac.compare_digest(pkce_challenge(verifier), record["code_challenge"]):
                raise OAuthError("invalid_grant", "PKCE verification failed")
            requested = form.get("resource", "")
            if requested and requested.rstrip("/") != record["resource"]:
                raise OAuthError("invalid_target", "Token requested for a different resource")
            if record["resource"] != resource:
                raise OAuthError("invalid_target", "The server address changed; connect again")
            grant_id = secrets.token_urlsafe(16)
            self._grants[grant_id] = {
                "client_id": record["client_id"],
                "resource": resource,
                "refresh": "",
                "used": [],
            }
            return self._issue_unlocked(grant_id)

    def _refresh(self, form: dict[str, str], *, resource: str) -> dict[str, Any]:
        digest = _digest(form.get("refresh_token", ""))
        with self._lock:
            for grant_id, grant in list(self._grants.items()):
                if digest in grant.get("used", []):
                    # A rotated-out refresh token came back: assume theft and end the grant.
                    self._revoke_grant_unlocked(grant_id)
                    self._save_unlocked()
                    raise OAuthError("invalid_grant", "Refresh token reuse detected; connect again")
                if grant.get("refresh") == digest:
                    if grant["client_id"] != form.get("client_id"):
                        raise OAuthError("invalid_grant", "client_id does not match the refresh token")
                    if grant.get("refresh_expires_at", 0) <= self._clock():
                        self._revoke_grant_unlocked(grant_id)
                        self._save_unlocked()
                        raise OAuthError("invalid_grant", "The refresh token expired; connect again")
                    if grant["resource"] != resource:
                        raise OAuthError("invalid_target", "The server address changed; connect again")
                    grant["used"] = (grant.get("used", []) + [digest])[-20:]
                    return self._issue_unlocked(grant_id)
        raise OAuthError("invalid_grant", "The refresh token is invalid")

    def _issue_unlocked(self, grant_id: str) -> dict[str, Any]:
        grant = self._grants[grant_id]
        for token_digest, info in list(self._access.items()):
            if info["grant_id"] == grant_id:
                del self._access[token_digest]  # one live access token per grant
        access = "hba_" + secrets.token_urlsafe(32)
        refresh = "hbr_" + secrets.token_urlsafe(32)
        now = self._clock()
        self._access[_digest(access)] = {
            "grant_id": grant_id,
            "resource": grant["resource"],
            "expires_at": now + ACCESS_TOKEN_SECONDS,
        }
        grant["refresh"] = _digest(refresh)
        grant["refresh_expires_at"] = now + REFRESH_TOKEN_SECONDS
        self._save_unlocked()
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_SECONDS,
            "refresh_token": refresh,
            "scope": SCOPE,
        }

    def validate_access_token(self, token: str, *, resource: str) -> bool:
        with self._lock:
            info = self._access.get(_digest(token))
            if info is None or info["expires_at"] <= self._clock():
                return False
            # Audience check: a token for another server (or an old address) is refused.
            return info["resource"] == resource and info["grant_id"] in self._grants

    def revoke_token(self, token: str) -> None:
        """RFC 7009: revoking either token ends the whole grant. Unknown tokens are not an error."""
        digest = _digest(token)
        with self._lock:
            grant_id = None
            if digest in self._access:
                grant_id = self._access[digest]["grant_id"]
            else:
                matches = (gid for gid, grant in self._grants.items() if grant.get("refresh") == digest)
                grant_id = next(matches, None)
            if grant_id:
                self._revoke_grant_unlocked(grant_id)
                self._save_unlocked()

    def disconnect_all(self) -> None:
        with self._lock:
            self._clients.clear()
            self._grants.clear()
            self._access.clear()
            self._codes.clear()
            self._pending.clear()
            self._decisions.clear()
            self._save_unlocked()
        _LOGGER.info("All MCP OAuth agents disconnected")

    def public_status(self) -> dict[str, Any]:
        with self._lock:
            connected = {grant["client_id"] for grant in self._grants.values()}
            agents = [
                {"name": client["name"], "connected": client_id in connected}
                for client_id, client in sorted(self._clients.items(), key=lambda item: item[1]["created_at"])
            ]
        return {"agents": agents, "pending": self.pending_for_owner()}

    # -- housekeeping ---------------------------------------------------------------------------

    def _make_room_for_client_unlocked(self) -> None:
        """Drop never-approved registrations so strangers cannot use up every client slot."""
        now = self._clock()
        granted = {grant["client_id"] for grant in self._grants.values()}
        # A stable sort on age alone keeps registration order for agents created in the same second.
        unapproved = sorted(
            (
                (client["created_at"], client_id)
                for client_id, client in self._clients.items()
                if not client.get("approved_at") and client_id not in granted
            ),
            key=lambda item: item[0],
        )
        for created_at, client_id in unapproved:
            if now - created_at >= UNAPPROVED_CLIENT_SECONDS:
                self._drop_client_unlocked(client_id)
        unapproved = [item for item in unapproved if item[1] in self._clients]
        while len(self._clients) >= MAX_CLIENTS and unapproved:
            self._drop_client_unlocked(unapproved.pop(0)[1])
        if len(self._clients) >= MAX_CLIENTS:
            raise OAuthError("invalid_client_metadata", "Too many connected agents; disconnect some first")

    def _drop_client_unlocked(self, client_id: str) -> None:
        self._clients.pop(client_id, None)
        self._pending = {key: value for key, value in self._pending.items() if value.client_id != client_id}

    def _revoke_grant_unlocked(self, grant_id: str) -> None:
        self._grants.pop(grant_id, None)
        for token_digest, info in list(self._access.items()):
            if info["grant_id"] == grant_id:
                del self._access[token_digest]

    def _prune_unlocked(self) -> None:
        now = self._clock()
        self._codes = {key: value for key, value in self._codes.items() if value["expires_at"] > now}
        self._pending = {key: value for key, value in self._pending.items() if value.expires_at > now}
        self._decisions = {key: value for key, value in self._decisions.items() if key in self._pending}
        self._access = {key: value for key, value in self._access.items() if value["expires_at"] > now}

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            clients = payload.get("clients", {})
            grants = payload.get("grants", {})
            access = payload.get("access", {})
            if not all(isinstance(item, dict) for item in (clients, grants, access)):
                raise ValueError("unexpected structure")
            # Keep only well-formed records that still link up, so a damaged file drops agents
            # (they connect again) instead of raising errors later on every request.
            self._clients = {key: value for key, value in clients.items() if _valid_client(value)}
            self._grants = {
                key: value
                for key, value in grants.items()
                if _valid_grant(value) and value["client_id"] in self._clients
            }
            self._access = {
                key: value
                for key, value in access.items()
                if _valid_access(value) and value["grant_id"] in self._grants
            }
            dropped = len(clients) + len(grants) + len(access) - (
                len(self._clients) + len(self._grants) + len(self._access)
            )
            if dropped:
                _LOGGER.warning("Dropped %s malformed record(s) from the MCP OAuth store", dropped)
        except (OSError, ValueError, AttributeError) as exc:
            # Fail closed: an unreadable store means no agent is connected.
            _LOGGER.warning("Ignoring unreadable MCP OAuth store %s: %s", self._path, exc)
            self._clients, self._grants, self._access = {}, {}, {}

    def _save_unlocked(self) -> None:
        if self._path is None:
            return
        payload = {"clients": self._clients, "grants": self._grants, "access": self._access}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(self._path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self._path)
        except OSError as exc:
            _LOGGER.warning("Could not persist MCP OAuth state: %s", exc)


CONSENT_HEADERS = {
    "Cache-Control": "no-store",
    # The consent page must never be framed (clickjacking) or load anything external. No
    # form-action: browsers apply it to the 303 redirect back to the agent and would block it.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def consent_page(pending: PendingAuthorization | None, *, error: str = "", vision: bool = False) -> str:
    """The owner-facing approval screen; every dynamic value is HTML-escaped."""
    from html import escape  # noqa: PLC0415

    style = (
        "body{font-family:system-ui,sans-serif;background:#0b0d12;color:#f7f8fb;margin:0;padding:24px}"
        "main{max-width:460px;margin:0 auto;background:#13161e;border:1px solid #ffffff1a;"
        "border-radius:16px;padding:22px}"
        "h1{font-size:1.3rem;margin:0 0 12px}p,li{color:#c7ccd8;line-height:1.5}"
        "input{width:100%;box-sizing:border-box;font-size:1.3rem;letter-spacing:.12em;padding:12px;border-radius:10px;"
        "border:1px solid #ffffff33;background:#080a0f;color:#f7f8fb;text-transform:uppercase}"
        "button{font-size:1rem;padding:12px 16px;border-radius:10px;border:0;margin-top:12px;"
        "width:100%;cursor:pointer}"
        ".ok{background:#ff7a45;color:#fff}.no{background:#2a2f3b;color:#f7f8fb}.err{color:#ff5d6c}"
        ".code{font-size:2rem;letter-spacing:.3em;text-align:center;font-weight:700;color:#f7f8fb;margin:8px 0}"
    )
    if pending is None:
        body = (
            "<h1>Connection request expired</h1><p>Start connecting again from your agent.</p>"
            + (f'<p class="err">{escape(error)}</p>' if error else "")
        )
    else:
        redirect_host = urlparse(pending.redirect_uri).netloc
        abilities = [
            "check whether Reachy is awake, resting or private",
            "say short messages aloud (for example reminders)",
            "show an emotion while Reachy is already awake",
        ]
        if vision:
            abilities.append("ask what Reachy sees (answered locally; it receives text, never images)")
        items = "".join(f"<li>{escape(item)}</li>" for item in abilities)
        body = (
            f"<h1>Let “{escape(pending.client_name)}” use Reachy?</h1>"
            f"<p>The agent at <strong>{escape(redirect_host)}</strong> asks to:</p><ul>{items}</ul>"
            "<p>Meeting, Sleep, privacy and Kids Mode always win. You can disconnect it any time in "
            "Homebody Settings → Agent access.</p>"
            "<p><strong>To allow it,</strong> open Homebody Settings → Agent access on your home network. "
            "Find this request, check that it shows the same code and return address, and press "
            "<em>Approve</em>. Then come back and press <em>Continue</em>.</p>"
            f'<p class="code" aria-label="Match code">{escape(pending.match_code)}</p>'
            "<p>Did you not start this? Press Deny, and deny it in Settings too.</p>"
            + (f'<p class="err" role="alert">{escape(error)}</p>' if error else "")
            + '<form method="post" action="/oauth/authorize">'
            f'<input type="hidden" name="pending_id" value="{escape(pending.pending_id)}">'
            '<button class="ok" name="action" value="continue">Continue</button>'
            "</form>"
            '<form method="post" action="/oauth/authorize">'
            f'<input type="hidden" name="pending_id" value="{escape(pending.pending_id)}">'
            '<button class="no" name="action" value="deny" formnovalidate>Deny</button></form>'
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Homebody: connect an agent</title><style>{style}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )
