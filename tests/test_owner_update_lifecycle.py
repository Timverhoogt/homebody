"""Regression gates for SDK-retained lifecycle states and final live-worker admission."""
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from test_update_daemon import deployment as deployment
from test_update_daemon import folded
from test_updates import manifest, wheel

from homebody import update_api, update_daemon, update_worker, updates
from homebody.runtime import HermesVoiceRuntime


class SDKLifecycle:
    """Hardware-free SDK 1.9.0 semantics: DONE retained, STOPPING not stoppable."""
    def __init__(self, state="done", stop_race=None):
        self.state = state
        self.stop_race = stop_race
        self.calls = []
        self.polls = 0

    def api(self, path, post=False):
        self.calls.append((path, post))
        if path.endswith("state/full"):
            return folded()
        if not post:
            self.polls += 1
            if self.state == "stopping" and self.polls >= 3:
                self.state = "done"
            return None if self.state is None else {"info": {"name": "reachy_mini_homebody"}, "state": self.state}
        if path.endswith("stop-current-app"):
            if self.stop_race:
                self.state, self.stop_race = self.stop_race, None
            if self.state in {None, "done", "stopping"}:
                httpx.Response(400, request=httpx.Request("POST", "http://localhost" + path)).raise_for_status()
            self.state = None
        else:
            if self.state not in {None, "done"}:
                httpx.Response(400, request=httpx.Request("POST", "http://localhost" + path)).raise_for_status()
            self.state = "running"
        return None


@pytest.mark.parametrize("state", ["done", "stopping", "running", "starting", "error", None])
def test_daemon_stop_handles_the_sdk_retained_status(deployment, state):
    driver = update_daemon.DaemonSlots(deployment[0])
    sdk = SDKLifecycle(state)
    driver.api = sdk.api
    driver.stop()
    assert sdk.state in {None, "done"}
    assert sum(post for _, post in sdk.calls) == (0 if state in {None, "done", "stopping"} else 1)
    driver.start()
    assert sdk.state == "running"


@pytest.mark.parametrize("state", ["done", "stopping"])
def test_clean_exit_or_existing_stop_wins_stop_post_race(deployment, state):
    driver = update_daemon.DaemonSlots(deployment[0])
    sdk = SDKLifecycle("running", stop_race=state)
    driver.api = sdk.api
    driver.stop()
    driver.start()
    assert sdk.state == "running"


def test_stopping_has_a_bounded_wait_and_no_duplicate_stop(deployment, monkeypatch):
    driver = update_daemon.DaemonSlots(deployment[0])
    sdk = SDKLifecycle("stopping")
    sdk.polls = -100
    driver.api = sdk.api
    clock = iter((0, 20, 40, 60))
    monkeypatch.setattr(update_daemon, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None))
    with pytest.raises(ValueError, match="did not stop"):
        driver.stop()
    assert not any(post for _, post in sdk.calls)


@pytest.mark.parametrize("phase,state", [("queued", "done"), ("queued", "stopping"),
                                         ("stopping", "stopping"), ("starting", "done")])
def test_real_job_recovers_with_sdk_done_and_stopping(deployment, tmp_path, phase, state):
    value, _ = deployment
    driver = update_daemon.DaemonSlots(value)
    driver.python = sys.executable
    driver.preflight = lambda: None
    prior = driver.current.resolve()
    (prior / "homebody/__init__.py").write_text('__version__ = "0.4.0"\n')
    sdk = SDKLifecycle(state)
    driver.api = sdk.api
    def healthy(identity: dict, deadline: float = 90) -> bool:
        path = driver.current / "homebody/release.json"
        version = json.loads(path.read_text())["version"] if path.exists() else "0.4.0"
        return sdk.state == "running" and version == identity["version"]
    driver.healthy = healthy
    artifact = wheel(tmp_path, "0.5.0")
    m = {**manifest(), "python": "3.11,3.12", "size": artifact.stat().st_size,
         "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}
    store = updates.Store(Path(value["state"]))
    store.enqueue(m, "0.4.0", [])
    if phase != "queued":
        store.change(phase, prior=str(prior), prior_identity={"version": "0.4.0", "release_id": None, "commit": None})
        if phase == "starting":
            new = driver.slots / "interrupted"
            driver.install(artifact, new)
            driver.switch(new)
    update_worker.run_job(store, driver, download=lambda destination: shutil.copyfile(artifact, destination))
    assert store.read()["job"]["state"] == ("succeeded" if phase == "queued" else "rolled_back")
    assert sdk.state == "running"
    assert (driver.current.resolve() == prior) is (phase != "queued")


@pytest.fixture
def admission(tmp_path, monkeypatch):
    root = tmp_path / "state"
    service = update_api.UpdateService(tmp_path / "catalog")
    value = {"state": str(root), "current": str(Path(update_api.__file__).resolve().parent.parent)}
    monkeypatch.setattr(update_api, "configured_path", lambda: tmp_path / "updater.json")
    monkeypatch.setattr(update_api, "config", lambda _: value)
    updates.atomic_json(root / "worker.json", {"pid": os.getpid(), "heartbeat": time.time()})
    updates.atomic_json(service.root / "catalog.json", {"available": manifest(), "notes": []})
    monkeypatch.setattr(update_api, "installed_identity", lambda: {"version": "0.4.0"})
    runtime = object.__new__(HermesVoiceRuntime)
    runtime._motor_transition_lock = threading.RLock()
    runtime._voice_activity_lock = threading.Lock()
    runtime._kids_lock = threading.RLock()
    runtime.stop_event = threading.Event()
    runtime._control_ready = threading.Event()
    runtime._control_ready.set()
    runtime.status = lambda: {"state": "waiting_for_wake_word", "power_mode": "standby", "motors_enabled": False,
        "head_safely_folded": True, "robot_action_busy": False, "camera_control_active": False,
        "announcement_busy": False, "announcement_queue_depth": 0,
        "kids_mode": {"active": False, "locked": False}, "last_error": ""}
    return root, service, runtime


@pytest.mark.parametrize("phase", ["discover", "preflight", "manifest", "reservation"])
def test_worker_dying_during_install_checks_never_quiesces_runtime(admission, monkeypatch, phase):
    root, service, runtime = admission
    def stale():
        updates.atomic_json(root / "worker.json", {"pid": os.getpid(), "heartbeat": time.time()-60})
    def discover(_):
        if phase == "discover":
            stale()
        return {"available": manifest(), "notes": []}
    driver = Mock()
    if phase == "preflight":
        driver.preflight.side_effect = stale
    if phase == "manifest":
        driver.check_manifest.side_effect = lambda _: stale()
    monkeypatch.setattr(update_api, "discover", discover)
    monkeypatch.setattr(update_api, "driver_for", lambda _: driver)
    if phase == "reservation":
        reserve = runtime.reserve_update
        def deferred(enqueue):
            stale()
            return reserve(enqueue)
        runtime.reserve_update = deferred
    with pytest.raises(ValueError, match="updater|unavailable"):
        service.install(update_api.InstallRequest(confirm="INSTALL", release_id=42), runtime)
    assert not runtime.stop_event.is_set() and runtime.control_ready
    assert service.store().read()["job"] is None


def test_fresh_heartbeat_with_a_dead_pid_is_not_available(admission):
    root, service, _ = admission
    updates.atomic_json(root / "worker.json", {"pid": 99999999, "heartbeat": time.time()})
    assert service.supported()[0] is False


@pytest.mark.parametrize("status", [400, 401, 500])
def test_unexpected_stop_error_is_not_a_successful_race(deployment, status):
    driver = update_daemon.DaemonSlots(deployment[0])
    sdk = SDKLifecycle("running")
    def api(path, post=False):
        if post:
            httpx.Response(status, request=httpx.Request("POST", "http://localhost"+path)).raise_for_status()
        return sdk.api(path, post)
    driver.api = api
    with pytest.raises(httpx.HTTPStatusError):
        driver.stop()
    assert sdk.state == "running"


def test_start_waits_for_in_progress_stop_without_duplicating_it(deployment):
    driver = update_daemon.DaemonSlots(deployment[0])
    sdk = SDKLifecycle("stopping")
    driver.api = sdk.api
    driver.start()
    assert sdk.state == "running"
    assert [path for path, post in sdk.calls if post] == ["/api/apps/start-app/reachy_mini_homebody"]


@pytest.mark.parametrize("heartbeat", [None, [], "bad", {},
    {"pid": True, "heartbeat": 0}, {"pid": -1, "heartbeat": 0},
    {"pid": 1, "heartbeat": "bad"}])
def test_malformed_worker_availability_fails_closed(admission, heartbeat):
    root, service, _ = admission
    updates.atomic_json(root / "worker.json", heartbeat)
    assert service.supported()[0] is False
