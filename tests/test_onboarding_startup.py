"""First-run defaults and quiet admission; no hardware or provider access."""

import builtins
import json
import threading
from unittest.mock import Mock

from homebody.config import load_config, save_config
from homebody.runtime import HermesVoiceRuntime


def test_fresh_install_requires_explicit_setup(tmp_path):
    config = load_config(tmp_path / "new.json")
    assert getattr(config, "setup_pending", False) is True
    assert config.motion_enabled is False
    assert config.robot_tools_enabled is False
    assert config.camera_enabled is False
    assert config.camera_feed_enabled is False
    assert not (tmp_path / "new.json").exists()


def test_existing_install_does_not_gain_setup_gate(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api_key": "saved", "motion_enabled": True}))
    config = load_config(path)
    assert getattr(config, "setup_pending", False) is False
    assert config.motion_enabled is True
    assert config.api_key == "saved"


def test_quiet_run_never_admits_media_models_or_actions(monkeypatch, tmp_path):
    import homebody.runtime as runtime_module

    config = load_config(tmp_path / "config.json")
    stop = threading.Event()
    robot = Mock()
    forbidden = Mock(side_effect=AssertionError("Setup cannot load a wake model"))
    monkeypatch.setattr(runtime_module, "ensure_kws_model", forbidden)
    runtime = HermesVoiceRuntime(robot, stop, config_loader=lambda: config)
    outcomes = []

    def run():
        try:
            runtime.run()
        except BaseException as exc:
            outcomes.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        # Wait for the actual run() boundary, not a fixed sleep.
        for _ in range(100):
            if runtime.status()["state"] == "setup_required" or outcomes:
                break
            stop.wait(0.01)
        assert runtime.status()["state"] == "setup_required"
        assert outcomes == []
        forbidden.assert_not_called()
        assert robot.mock_calls == []
        assert runtime.control_ready is False
    finally:
        stop.set()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_pending_state_survives_settings_save(tmp_path):
    path = tmp_path / "config.json"
    config = load_config(path)
    save_config(config, path)
    assert getattr(load_config(path), "setup_pending", False) is True


def test_windows_import_can_reach_app_entry_without_unix_locking(monkeypatch):
    import importlib

    import homebody.update_worker as worker
    import homebody.updates as updates

    original = builtins.__import__

    def no_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("Windows has no fcntl")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(builtins, "__import__", no_fcntl)
        importlib.reload(updates)
        importlib.reload(worker)
        import homebody.main as main

        assert callable(main.run_cli)
    importlib.reload(updates)
    importlib.reload(worker)


def test_unavailable_managed_locking_fails_before_creating_files(monkeypatch, tmp_path):
    import pytest

    import homebody.updates as updates

    monkeypatch.setattr(updates, "fcntl", None)
    root = tmp_path / "updates"
    with pytest.raises(OSError, match="locking"):
        with updates.Store(root).transaction():
            pass
    assert not root.exists()
