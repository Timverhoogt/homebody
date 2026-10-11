"""Bounded, read-only agent checks, independent of robot power and voice state."""

import threading
import time
from collections.abc import Callable

from homebody.config import AppConfig
from homebody.hermes_client import HermesBridgeClient


def probe_connection(config: AppConfig) -> dict[str, str]:
    client = HermesBridgeClient(config)
    try:
        health = client.health()
        backend = "openclaw" if config.model.startswith("openclaw/") else "hermes"
        backends = health.get("agent_backends", [])
        ready = any(
            isinstance(item, dict) and item.get("name") == backend and item.get("ok") is True for item in backends
        )
        if not ready:
            return {"state": "unavailable", "detail": "The selected agent is not ready on the bridge."}
        # /health is public. Require authenticated discovery before showing green.
        models = client.models(timeout=5.0)
        if not any(item.get("id") == config.model for item in models):
            return {"state": "unavailable", "detail": "The selected model route is unavailable. Check Agent settings."}
        return {
            "state": "connected",
            "detail": (
                "Authenticated bridge and selected agent are ready. "
                "This does not activate microphones, cameras or motors."
            ),
        }
    except Exception:
        # Bridge exceptions may contain private URLs or provider error bodies.
        return {
            "state": "unavailable",
            "detail": "Cannot verify the agent connection. Check the bridge, credentials and Agent settings.",
        }
    finally:
        client.close()


class AgentConnectionMonitor:
    """One in-flight probe; brief cache; discard results after config changes."""

    def __init__(self, *, probe: Callable = probe_connection, clock: Callable = time.monotonic):
        self._probe = probe
        self._clock = clock
        self._lock = threading.Lock()
        self._signature = None
        self._generation = 0
        self._busy = False
        self._result = None
        self._checked = 0.0

    def invalidate(self):
        with self._lock:
            self._generation += 1
            self._result = None

    def snapshot(self, config: AppConfig) -> dict[str, object]:
        signature = (config.bridge_url, config.api_key, config.model)
        label = "OpenClaw" if config.model.startswith("openclaw/") else "Hermes"
        with self._lock:
            if signature != self._signature:
                self._signature = signature
                self._generation += 1
                self._result = None
            if not config.configured:
                return {
                    "state": "unconfigured",
                    "label": "Agent",
                    "detail": "Connect your agent in Settings.",
                    "fresh_for_ms": 0,
                }
            age = max(0.0, self._clock() - self._checked)
            if (self._result is None or age >= 15) and not self._busy:
                self._busy = True
                threading.Thread(
                    target=self._run, args=(config, self._generation), daemon=True, name="agent-connection"
                ).start()
            if self._result is None or age >= 25:
                return {
                    "state": "checking",
                    "label": label,
                    "detail": "Checking the current agent connection…",
                    "fresh_for_ms": 0,
                }
            return {**self._result, "label": label, "fresh_for_ms": int((25 - age) * 1000)}

    def _run(self, config, generation):
        try:
            result = self._probe(config)
        except Exception:
            result = {"state": "unavailable", "detail": "Agent connection check failed."}
        with self._lock:
            if generation == self._generation:
                self._result = result
                self._checked = self._clock()
            self._busy = False
