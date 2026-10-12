"""Daemon-owned lifecycle and atomically switched app-only package links."""
import json
import math
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from test_updates import manifest, wheel

from homebody import update_worker, updates


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    from homebody.update_daemon import DaemonSlots
    monkeypatch.setattr(DaemonSlots, "sleep_distance", lambda _: {"safe": True, "distance": 0., "threshold": 10.},
                        raising=False)
    base = tmp_path / "managed"
    slot = base / "slots/initial"
    (slot / "homebody").mkdir(parents=True)
    meta = slot / "reachy_mini_homebody-0.4.0.dist-info"
    meta.mkdir()
    (slot / ".homebody-dist-info").symlink_to(meta.name)
    current = base / "current"
    current.symlink_to(slot)
    venv = tmp_path / "deps"
    python = venv / "bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    (venv / "pyvenv.cfg").touch()
    site = venv / "lib/python3.12/site-packages"
    site.mkdir(parents=True)
    (site / "homebody").symlink_to(current / "homebody")
    metadata = site / meta.name
    metadata.symlink_to(current / ".homebody-dist-info")
    value = {"topology": "reachy-daemon-slots-v1", "state": str(base / "state"),
             "slots": str(base / "slots"), "current": str(current), "python": str(python),
             "site_packages": str(site), "metadata_link": str(metadata),
             "status_url": "http://127.0.0.1:8042/api/updates/health", "health_token": "a" * 64}
    path = tmp_path / "updater.json"
    path.write_text(json.dumps(value))
    return value, path


def test_daemon_configuration_preserves_existing_app_environment(deployment):
    value, path = deployment
    assert update_worker.config(path) == value
    assert type(update_worker.driver_for(value)).__name__ == "DaemonSlots"


@pytest.mark.parametrize("mutation", ["package", "metadata", "extra_distribution", "outside_site"])
def test_daemon_config_rejects_drifted_import_links(deployment, mutation):
    value, path = deployment
    site = Path(value["site_packages"])
    if mutation == "package":
        (site / "homebody").unlink()
        (site / "homebody").mkdir()
    elif mutation == "metadata":
        Path(value["metadata_link"]).unlink()
        Path(value["metadata_link"]).symlink_to(site / "unmanaged")
    elif mutation == "extra_distribution":
        (site / "reachy_mini_homebody-9.9.9.dist-info").mkdir()
    else:
        value["site_packages"] = str(site.parent / "other")
        path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        update_worker.config(path)


def folded():
    return {"control_mode": "disabled", "head_pose": {
        "x": -.02, "y": 0., "z": -.04, "roll": 0., "pitch": .48, "yaw": 0.}}


@pytest.mark.parametrize("key,bad", [("z", math.nan), ("pitch", math.inf), ("x", None),
                                      ("z", 0.), ("pitch", 0.), ("z", True)])
def test_daemon_will_not_stop_an_unverified_fold(deployment, key, bad):
    value, _ = deployment
    driver = update_worker.driver_for(value)
    state = folded()
    state["head_pose"][key] = bad
    calls = []
    def api(path, post=False):
        calls.append((path, post))
        return state
    driver.api = api
    with pytest.raises(ValueError, match="fold|torque"):
        driver.stop()
    assert not any(post for _, post in calls)


def test_daemon_rejects_enabled_torque(deployment):
    value, _ = deployment
    driver = update_worker.driver_for(value)
    driver.api = lambda *args, **kwargs: {**folded(), "control_mode": "enabled"}
    with pytest.raises(ValueError, match="fold|torque"):
        driver.start()


def test_daemon_stop_never_stops_another_app(deployment):
    value, _ = deployment
    driver = update_worker.driver_for(value)
    calls = []
    def api(path, post=False):
        calls.append((path, post))
        return folded() if path.endswith("state/full") else {"info": {"name": "other"}, "state": "running"}
    driver.api = api
    with pytest.raises(ValueError, match="Another"):
        driver.stop()
    assert not any(post for _, post in calls)


def test_daemon_stop_and_start_only_use_homebody_routes(deployment):
    value, _ = deployment
    driver = update_worker.driver_for(value)
    calls = []
    app = [{"info": {"name": "reachy_mini_homebody"}, "state": "running"}]
    def api(path, post=False):
        calls.append((path, post))
        if path.endswith("state/full"):
            return folded()
        if post and path.endswith("stop-current-app"):
            app[0] = None
        elif post:
            app[0] = {"info": {"name": "reachy_mini_homebody"}, "state": "starting"}
        return app[0]
    driver.api = api
    driver.stop()
    driver.start()
    assert [path for path, post in calls if post] == [
        "/api/apps/stop-current-app", "/api/apps/start-app/reachy_mini_homebody"]


def test_atomic_pointer_switch_preserves_package_and_metadata_links(deployment, monkeypatch, tmp_path):
    value, _ = deployment
    driver = update_worker.driver_for(value)
    artifact = wheel(tmp_path, "0.5.0")
    # Real pip into the inactive slot, with no dependencies or shared-venv writes.
    driver.python = sys.executable
    slot = driver.slots / "new"
    driver.install(artifact, slot)
    driver.switch(slot)
    site = Path(value["site_packages"])
    assert (site / "homebody/__init__.py").read_text() == '__version__ = "0.5.0"\n'
    assert "Version: 0.5.0" in (Path(value["metadata_link"]) / "METADATA").read_text()
    assert (site / "homebody").is_symlink()
    assert Path(value["metadata_link"]).is_symlink()


def test_daemon_preflight_verifies_target_interpreter_and_entrypoint(deployment, monkeypatch):
    value, _ = deployment
    driver = update_worker.driver_for(value)
    result = {"python": "3.12", "sdk": "1.9.0", "module": "homebody.main",
              "site": value["site_packages"]}
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, json.dumps(result)))
    driver.api = lambda path, **kwargs: folded() if path.endswith("state/full") else {
        "info": {"name": "reachy_mini_homebody"}, "state": "running"}
    driver.preflight()
    result["module"] = "untrusted.main"
    with pytest.raises(ValueError, match="entry|environment"):
        driver.preflight()


def test_python312_is_explicitly_supported(monkeypatch):
    monkeypatch.setattr(updates.sys, "version_info", (3, 12, 3))
    assert updates.compatible_host()
    assert updates.validate_manifest({**manifest(), "python": "3.11,3.12"})["python"] == "3.11,3.12"
    for unsupported in ["3.13", "3.11,3.12,3.13", ">=3.11"]:
        with pytest.raises(ValueError):
            updates.validate_manifest({**manifest(), "python": unsupported})


def test_released_python_contract_is_checked_against_target(deployment, monkeypatch):
    value, _ = deployment
    driver = update_worker.driver_for(value)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "3.12\n"))
    with pytest.raises(ValueError, match="Python"):
        driver.check_manifest({**manifest(), "python": "3.11"})
    driver.check_manifest({**manifest(), "python": "3.11,3.12"})


def test_existing_runtime_config_can_locate_updater_without_supervisor_env(tmp_path, monkeypatch):
    from homebody import config
    existing = tmp_path / "config.json"
    existing.write_text("{}")
    updater = tmp_path / "updater.json"
    updater.write_text("{}")
    monkeypatch.delenv("HOMEBODY_UPDATER_CONFIG", raising=False)
    monkeypatch.setattr(config, "default_config_path", lambda: existing)
    assert updates.configured_path() == updater


def test_shutdown_wins_a_queued_motor_enable(monkeypatch):
    from unittest.mock import Mock

    from homebody.runtime import HermesVoiceRuntime
    runtime = object.__new__(HermesVoiceRuntime)
    runtime._motor_transition_lock = threading.RLock()
    runtime.stop_event = threading.Event()
    post = Mock()
    monkeypatch.setattr("homebody.power.httpx.post", post)
    with runtime._motor_transition_lock:
        runtime.stop_event.set()
    with pytest.raises(RuntimeError, match="stopping"):
        runtime._set_motor_mode(True, wake=True)
    post.assert_not_called()


@pytest.mark.parametrize("distance,threshold,safe", [(11., 10., True), (math.nan, 10., True),
                                                    (0., 10., False), (0., math.inf, True)])
def test_sdk_stop_distance_is_required_not_just_a_fold_heuristic(deployment, distance, threshold, safe):
    value, _ = deployment
    driver = update_worker.driver_for(value)
    driver.api = lambda *args, **kwargs: folded()
    driver.sleep_distance = lambda: {"safe": safe, "distance": distance, "threshold": threshold}
    with pytest.raises(ValueError, match="SDK|fold|torque"):
        driver.physical_safe()
