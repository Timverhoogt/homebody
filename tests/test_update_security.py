"""Adversarial update discovery, exact health and crash recovery contracts."""
import json

import httpx
import pytest
from test_updates import manifest

from homebody import updates
from homebody.update_worker import SystemdSlots, run_job


def release(v, rid, **extra):
    artifact = f"reachy_mini_homebody-{v}-py3-none-any.whl"
    return {"id": rid, "tag_name": "v" + v, "draft": False, "prerelease": False,
            "body": "Notes " + v, "assets": [{"name": name, "browser_download_url":
             f"https://github.com/Timverhoogt/homebody/releases/download/v{v}/{name}"}
             for name in [artifact, "homebody-update.json"]], **extra}


def test_discovery_accumulates_notes_and_skips_prereleases(monkeypatch):
    m = manifest()
    def fetch(url, limit):
        if "per_page" in url:
            return json.dumps([release("0.5.0", 42), release("0.4.1", 41),
                               release("0.6.0", 43, prerelease=True)]).encode()
        if "/commits/" in url:
            return json.dumps({"sha": m["commit"]}).encode()
        return json.dumps(m).encode()
    monkeypatch.setattr(updates, "get", fetch)
    result = updates.discover("0.4.0")
    assert result["available"] == m
    assert [n["version"] for n in result["notes"]] == ["0.4.1", "0.5.0"]


@pytest.mark.parametrize("field,value", [("release_id", 43), ("commit", "c" * 40)])
def test_discovery_rejects_identity_mismatch(monkeypatch, field, value):
    m = {**manifest(), field: value}
    def fetch(url, limit):
        if "per_page" in url:
            return json.dumps([release("0.5.0", 42)]).encode()
        if "/commits/" in url:
            return json.dumps({"sha": "a" * 40}).encode()
        return json.dumps(m).encode()
    monkeypatch.setattr(updates, "get", fetch)
    with pytest.raises(ValueError, match="identity"):
        updates.discover("0.4.0")


@pytest.mark.parametrize("change", [{"release_id": 43}, {"version": "0.4.0"}, {"commit": "c" * 40}])
def test_health_rejects_wrong_installed_identity(monkeypatch, change):
    m = manifest()
    payload = {"app": "homebody", "release": {**m, **change}, "healthy": True}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs))
    monkeypatch.setattr("homebody.update_worker.time.sleep", lambda _: None)
    driver = SystemdSlots({"current": "/unused", "slots": "/unused", "python": "/unused",
                          "status_url": "http://127.0.0.1:1111", "health_token": "a" * 64})
    assert driver.healthy(m, deadline=0.01) is False


def test_crash_recovery_rolls_back_not_success(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    current = tmp_path / "current"
    current.symlink_to(new)
    store = updates.Store(tmp_path / "state")
    store.enqueue(manifest(), "0.4.0", [])
    prior_identity = {"version": "0.4.0", "release_id": None, "commit": None}
    store.change("verifying", prior=str(old), prior_identity=prior_identity)
    class Driver(SystemdSlots):
        def preflight(self): pass
        def stop(self): pass
        def start(self): pass
        def healthy(self, identity): return identity == prior_identity
    driver = Driver({"current": str(current), "slots": str(tmp_path), "python": "unused"})
    run_job(store, driver)
    assert current.resolve() == old
    assert store.read()["job"]["state"] == "rolled_back"


def test_download_failure_restarts_quiesced_prior_app(tmp_path):
    current = tmp_path / "current"
    old = tmp_path / "slots/old"
    old.mkdir(parents=True)
    current.symlink_to(old)
    store = updates.Store(tmp_path / "state")
    store.enqueue(manifest(), "0.4.0", [])
    class Driver(SystemdSlots):
        started = False
        def preflight(self): pass
        def stop(self): pass
        def start(self): self.started = True
        def healthy(self, identity): return True
    driver = Driver({"current": str(current), "slots": str(old.parent), "python": "unused"})
    def failure(dest): raise ValueError("download failed")
    run_job(store, driver, download=failure)
    assert driver.started is True
    assert current.resolve() == old
    assert store.read()["job"]["state"] == "failed"


def test_preflight_failure_is_terminal_not_stuck(tmp_path):
    store = updates.Store(tmp_path)
    store.enqueue(manifest(), "0.4.0", [])
    class Driver:
        current = tmp_path
        def preflight(self): raise ValueError("changed service topology")
        def stop(self): pass
        def start(self): pass
        def healthy(self, identity): return True
    run_job(store, Driver())
    assert store.read()["job"]["state"] == "failed"
