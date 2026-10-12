"""Regression gates for recovery, exact topology, and long-running worker admission."""
import json
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_updates import manifest

from homebody import update_worker, updates
from homebody.update_api import UpdateService
from homebody.update_worker import SystemdSlots, run_job


@pytest.mark.parametrize("state,bad_preflight", [("installing", False), ("queued", True)])
def test_admitted_app_recovers_even_without_prior(tmp_path, state, bad_preflight):
    old = tmp_path / "slots/old"
    old.mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to(old)
    store = updates.Store(tmp_path / "state")
    store.enqueue(manifest(), "0.4.0", [])
    store.change(state)
    driver = Mock(current=current)
    if bad_preflight:
        driver.preflight.side_effect = ValueError("changed topology")
    driver.healthy.return_value = True
    run_job(store, driver)
    driver.stop.assert_called_once()
    driver.start.assert_called_once()
    driver.healthy.assert_called_once()
    assert store.read()["job"]["state"] == "failed"


def test_later_survives_checks_and_check_failure(tmp_path, monkeypatch):
    service = UpdateService(tmp_path)
    updates.atomic_json(tmp_path / "catalog.json", {"deferred_release_id": 42})
    monkeypatch.setattr("homebody.update_api.discover", lambda _: {"available": manifest(), "notes": []})
    assert service.check()["deferred_release_id"] == 42
    monkeypatch.setattr("homebody.update_api.discover", Mock(side_effect=ValueError("offline")))
    assert service.check()["deferred_release_id"] == 42
    monkeypatch.setattr("homebody.update_api.discover", lambda _: {"available": manifest(), "notes": []})
    assert UpdateService(tmp_path).check()["deferred_release_id"] == 42


@pytest.mark.parametrize("mutation", ["valid", "argv", "env", "restart", "extra", "envfile"])
def test_preflight_rejects_substring_topology(tmp_path, monkeypatch, mutation):
    python = str(tmp_path / "deps/bin/python")
    current = str(tmp_path / "current")
    properties = {
        "ExecStart": f"{{ path={python} ; argv[]={python} -m homebody.main ; ignore_errors=no ; }}",
        "Environment": f"PYTHONPATH={current} PYTHONNOUSERSITE=1",
        "Restart": "no", "EnvironmentFiles": "", "WorkingDirectory": str(tmp_path),
        "FragmentPath": str(tmp_path / "homebody-app.service"),
        "ExecStartPre": "", "ExecStartPost": "", "ExecStop": "", "PassEnvironment": "", "UnsetEnvironment": "",
    }
    if mutation == "argv":
        properties["ExecStart"] = properties["ExecStart"].replace("-m homebody.main", "-m homebody.main.evil")
    elif mutation == "env":
        properties["Environment"] = f"PYTHONPATH={current}:evil PYTHONNOUSERSITE=1"
    elif mutation == "restart":
        properties["Restart"] = "nope"
    elif mutation == "extra":
        properties["ExecStart"] += properties["ExecStart"]
    elif mutation == "envfile":
        properties["EnvironmentFiles"] = "/unreviewed (ignore_errors=no)"
    driver = SystemdSlots({"current": current, "slots": str(tmp_path / "slots"), "python": python})
    monkeypatch.setattr(driver, "command", lambda *args: "123" if "--value" in args else
                        "\n".join(f"{k}={v}" for k, v in properties.items()))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, '1.9.2\n'))
    if mutation == "valid":
        driver.preflight()
    else:
        with pytest.raises(ValueError, match="topology"):
            driver.preflight()


def test_heartbeat_continues_during_job(tmp_path, monkeypatch):
    value = {"state": str(tmp_path)}
    monkeypatch.setattr(update_worker, "config", lambda _: value)
    monkeypatch.setattr(update_worker, "SystemdSlots", lambda _: Mock())
    observed = []
    def job(store, driver):
        first = json.loads((tmp_path / "worker.json").read_text())["heartbeat"]
        time.sleep(0.15)
        observed.append(json.loads((tmp_path / "worker.json").read_text())["heartbeat"] > first)
    monkeypatch.setattr(update_worker, "run_job", job)
    monkeypatch.setattr(update_worker, "HEARTBEAT_INTERVAL", 0.02, raising=False)
    update_worker.serve(Path("unused"), once=True)
    assert observed == [True]


def test_unconfigured_folded_idle_is_safe_but_faults_are_not():
    safe = {"state": "waiting_for_configuration", "power_mode": "standby", "motors_enabled": False,
            "head_safely_folded": True, "robot_action_busy": False, "camera_control_active": False,
            "announcement_busy": False, "announcement_queue_depth": 0,
            "kids_mode": {"active": False, "locked": False}, "last_error": ""}
    updates.require_safe(safe)
    with pytest.raises(ValueError):
        updates.require_safe({**safe, "last_error": "hardware fault"})
    with pytest.raises(ValueError):
        updates.require_safe({**safe, "state": "error"})


def test_dependency_probe_does_not_inherit_slot_pythonpath(tmp_path, monkeypatch):
    from test_updates import wheel
    artifact = wheel(tmp_path, "0.5.0")
    driver = SystemdSlots({"current": str(tmp_path / "current"), "slots": str(tmp_path),
                          "python": "/deps/bin/python"})
    monkeypatch.setenv("PYTHONPATH", "/current")
    monkeypatch.setenv("PYTHONHOME", "/bad")
    calls = []
    def run(args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, 0, json.dumps({"installed": {}, "markers": {}}))
    monkeypatch.setattr(subprocess, "run", run)
    driver.install(artifact, tmp_path / "new")
    for call in calls:
        assert "PYTHONPATH" not in call["env"]
        assert "PYTHONHOME" not in call["env"]
        assert call["env"]["PYTHONNOUSERSITE"] == "1"
