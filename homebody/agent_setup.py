"""Let the owner's own agent (Hermes Agent or OpenClaw) connect itself to Reachy.

The owner starts a setup in Settings and gets a short message to paste to their agent. It contains
this robot's address and a one-time setup code. The agent then:

1. reads a setup guide that this robot serves, matching its installed Homebody version;
2. downloads the companion bridge from this robot and checks every file against the SHA-256 manifest;
3. creates a dedicated, tool-restricted Reachy profile or agent on its own host and starts the bridge;
4. pairs: it sends the setup code, the bridge URL and the bridge key to ``/api/agent-setup/pair``.

Pairing saves the bridge settings only after Homebody has reached the bridge with that key and the
bridge reports a healthy, correctly restricted agent. Otherwise the agent gets the reason, so it can fix
it and try again with the same code. A code lasts 30 minutes, works once, and five wrong codes end it.
Creating one needs the bridge API key when one is set, so the code carries the owner's authority.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import __version__

BACKENDS = {"hermes": "Hermes Agent", "openclaw": "OpenClaw"}
SETUP_SECONDS = 30 * 60
CODE_ATTEMPTS = 5
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion
_GUIDE_DIR = Path(__file__).with_name("agent_guides")
_BRIDGE_FILE_RE = re.compile(r"^[a-z_]+\.py$|^hermes-reachy-bridge\.service\.example$")


class SetupError(Exception):
    """A pairing problem the agent can act on; ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def bridge_dir() -> Path:
    """The companion bridge shipped with this app (``homebody.bridge``), or ``companion/`` in a checkout."""
    try:
        packaged = Path(str(resources.files("homebody.bridge")))
        if (packaged / "hermes_reachy_bridge.py").is_file():
            return packaged
    except ModuleNotFoundError:
        pass
    return Path(__file__).resolve().parents[1] / "companion"


def bridge_manifest() -> list[dict[str, str]]:
    """Every bridge file with its SHA-256, so the agent can verify what it downloaded."""
    folder = bridge_dir()
    files = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and _BRIDGE_FILE_RE.match(path.name) and path.name != "__init__.py":
            files.append({"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return files


def bridge_file(name: str) -> Path:
    if not _BRIDGE_FILE_RE.match(name) or name == "__init__.py":
        raise FileNotFoundError(name)
    path = bridge_dir() / name
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


def lan_address() -> str:
    """This computer's address on the local network, for agents on another machine."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # TEST-NET; no packet is sent for UDP connect
            return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def robot_url(host_header: str, scheme: str = "http") -> str:
    """The address an agent should use for this robot, based on how the owner reached Settings."""
    host = host_header.strip() or "127.0.0.1:8042"
    hostname = urlparse(f"//{host}").hostname or ""
    port = urlparse(f"//{host}").port
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        # Settings opened on the robot itself: an agent elsewhere needs the LAN address.
        host = f"{lan_address()}:{port or 8042}"
    return f"{scheme}://{host}"


def render_guide(backend: str, base_url: str) -> str:
    """The setup guide for one agent, filled in for this robot and this Homebody version."""
    if backend not in BACKENDS:
        raise FileNotFoundError(backend)
    template = (_GUIDE_DIR / f"{backend}.md").read_text(encoding="utf-8")
    files = "\n".join(f"| `{item['name']}` | `{item['sha256']}` |" for item in bridge_manifest())
    return (
        template.replace("{{ROBOT_HOST}}", urlparse(base_url).hostname or "")
        .replace("{{ROBOT_URL}}", base_url)
        .replace("{{VERSION}}", __version__)
        .replace("{{BRIDGE_FILES}}", "| File | SHA-256 |\n| --- | --- |\n" + files)
    )


def setup_message(backend: str, base_url: str, code: str, *, mcp: bool) -> str:
    """What the owner pastes to their agent. It names the robot, the guide and the code, nothing secret."""
    agent = BACKENDS[backend]
    extra = " Also add Reachy as an MCP tool for yourself, as the guide describes." if mcp else ""
    return (
        f"Please connect my Reachy Mini robot (Homebody app) to you, {agent}, on this computer. "
        f"Read and follow the setup guide at {base_url}/agent-setup/{backend}.md. "
        f"The one-time setup code is {code}; it is valid for 30 minutes and only for that robot.{extra} "
        "Before you install software, change your configuration or start a service, tell me what you will "
        "do and wait for my OK. Never send keys or tokens anywhere except that robot's pairing address."
    )


@dataclass
class _Session:
    backend: str
    code_digest: str
    expires_at: float
    mcp: bool
    attempts: int = 0
    state: str = "waiting"  # waiting | paired | failed | cancelled
    last_error: str = ""
    paired: dict[str, Any] = field(default_factory=dict)


def _digest(code: str) -> str:
    return hashlib.sha256(re.sub(r"[^A-Z0-9]", "", code.upper()).encode("ascii")).hexdigest()


def _check_bridge_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path not in {"", "/"}:
        raise SetupError("bridge_url must look like http://<agent-computer-address>:8643")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise SetupError("bridge_url must not contain credentials, a query or a fragment")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address.is_unspecified):
        raise SetupError(
            "bridge_url points at the robot itself. Use the agent computer's LAN address, the one the robot "
            "can reach (for example the source address of `ip route get <robot-ip>`), and start the bridge "
            "with --host 0.0.0.0."
        )
    return url.strip().rstrip("/")


def probe_bridge(
    bridge_url: str, api_key: str, backend: str, *, client: httpx.Client | None = None
) -> dict[str, Any]:
    """Reach the bridge as Reachy will: health, the agent behind it, and the key on an authenticated route."""
    own = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False)
    try:
        try:
            health_response = http.get(f"{bridge_url}/health")
            health = health_response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SetupError(
                f"Reachy could not reach the bridge at {bridge_url} ({type(exc).__name__}). Check that it runs "
                "with --host 0.0.0.0 and that the agent computer's firewall allows the port.",
                status=424,
            ) from exc
        if not isinstance(health, dict):
            raise SetupError(f"{bridge_url}/health did not return the Homebody bridge's JSON", status=424)
        backends = {
            str(item.get("name")): item for item in health.get("agent_backends", []) if isinstance(item, dict)
        }
        entry = backends.get(backend)
        if entry is None:
            raise SetupError(
                f"The bridge does not run the {BACKENDS[backend]} backend; start it with "
                f"--agent-backends {backend}",
                status=424,
            )
        if entry.get("ok") is not True:
            raise SetupError(
                f"The bridge is running, but {BACKENDS[backend]} is not ready: {entry.get('error') or 'unavailable'}",
                status=424,
            )
        try:
            models = http.get(f"{bridge_url}/v1/models", headers={"Authorization": f"Bearer {api_key}"})
        except httpx.HTTPError as exc:
            raise SetupError(f"The bridge stopped answering: {type(exc).__name__}", status=424) from exc
        if models.status_code == 401:
            raise SetupError("The bridge refused that api_key. Send the key the bridge itself uses.", status=424)
        if models.status_code != 200:
            raise SetupError(f"The bridge answered HTTP {models.status_code} on /v1/models", status=424)
        return health
    finally:
        if own:
            http.close()


class AgentSetup:
    """At most one setup at a time; starting a new one replaces the old code."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._session: _Session | None = None

    def start(self, backend: str, *, mcp: bool) -> str:
        if backend not in BACKENDS:
            raise SetupError(f"backend must be one of {', '.join(BACKENDS)}")
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(8))
        with self._lock:
            self._session = _Session(
                backend=backend, code_digest=_digest(raw), expires_at=self._clock() + SETUP_SECONDS, mcp=mcp
            )
        return f"{raw[:4]}-{raw[4:]}"

    def cancel(self) -> None:
        with self._lock:
            if self._session is not None and self._session.state == "waiting":
                self._session.state = "cancelled"

    def status(self) -> dict[str, Any]:
        with self._lock:
            session = self._session
            if session is None:
                return {"state": "idle"}
            state = session.state
            if state == "waiting" and session.expires_at <= self._clock():
                state = "expired"
            return {
                "state": state,
                "backend": session.backend,
                "agent": BACKENDS[session.backend],
                "mcp": session.mcp,
                "expires_in": max(0, int(session.expires_at - self._clock())) if state == "waiting" else 0,
                "last_error": session.last_error,
                **session.paired,
            }

    def redeem(self, code: str, backend: str) -> _Session:
        """Check the code (attempt-limited, constant time). The session stays open until ``complete``."""
        with self._lock:
            session = self._session
            if session is None or session.state != "waiting" or session.expires_at <= self._clock():
                raise SetupError("No setup is waiting. Ask the owner to create a new setup message.", status=410)
            session.attempts += 1
            if not hmac.compare_digest(_digest(code), session.code_digest):
                if session.attempts >= CODE_ATTEMPTS:
                    session.state = "failed"
                    session.last_error = "Too many wrong setup codes"
                raise SetupError("That setup code is not right.", status=403)
            session.attempts = 0  # a correct code; bridge problems may need several tries
            if backend != session.backend:
                raise SetupError(f"This setup is for {BACKENDS[session.backend]}, not {backend}.")
            return session

    def record_error(self, message: str) -> None:
        with self._lock:
            if self._session is not None and self._session.state == "waiting":
                self._session.last_error = message[:300]

    def complete(self, bridge_url: str, *, agent_name: str = "") -> None:
        with self._lock:
            if self._session is None:
                return
            self._session.state = "paired"
            self._session.last_error = ""
            self._session.paired = {
                "bridge_url": bridge_url,
                "agent_name": agent_name[:80],
                "paired_at": int(self._clock()),
            }

    @staticmethod
    def validate_pair_request(payload: dict[str, Any]) -> tuple[str, str, str, str]:
        code = str(payload.get("code") or "")
        backend = str(payload.get("backend") or "")
        api_key = str(payload.get("api_key") or "").strip()
        if backend not in BACKENDS:
            raise SetupError(f"backend must be one of {', '.join(BACKENDS)}")
        if not 32 <= len(api_key) <= 512 or any(char.isspace() for char in api_key):
            raise SetupError("api_key must be the bridge's key: 32-512 characters, no spaces")
        return code, backend, _check_bridge_url(str(payload.get("bridge_url") or "")), api_key
