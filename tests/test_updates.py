"""Hardware-free update contract and real independent-worker sandbox tests."""
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


def module():
    from homebody import updates
    return updates


def manifest(version="0.5.0"):
    return {"schema": 1, "repository": "Timverhoogt/homebody", "version": version,
            "tag": "v" + version, "release_id": 42, "commit": "a" * 40,
            "python": "3.11,3.12", "platform": "linux", "sdk": "1.9",
            "artifact": f"reachy_mini_homebody-{version}-py3-none-any.whl",
            "sha256": "b" * 64, "size": 100, "notes": "Changes\n<script>not HTML</script>"}


def test_strict_manifest():
    u = module()
    assert u.validate_manifest(manifest())["version"] == "0.5.0"
    for key, value in [("repository", "attacker/homebody"), ("version", "0.5.0rc1"),
                       ("artifact", "../../evil.whl"), ("sha256", "not-a-hash"),
                       ("sdk", "2.0"), ("release_id", True), ("commit", "main")]:
        bad = manifest()
        bad[key] = value
        with pytest.raises(ValueError):
            u.validate_manifest(bad)


def test_durable_single_job_and_acknowledgement(tmp_path):
    u = module()
    store = u.Store(tmp_path)
    job = store.enqueue(manifest(), "0.4.0", [{"version": "0.5.0", "notes": "hello"}])
    assert u.Store(tmp_path).read()["job"]["id"] == job["id"]
    with pytest.raises(ValueError, match="already"):
        store.enqueue(manifest(), "0.4.0", [])
    store.finish("succeeded")
    assert store.read()["history"][0]["acknowledged"] is False
    store.acknowledge(job["id"])
    assert u.Store(tmp_path).read()["history"][0]["acknowledged"] is True


def test_safe_standby_is_fail_closed():
    u = module()
    safe = {"state": "waiting_for_wake_word", "power_mode": "standby", "motors_enabled": False,
            "head_safely_folded": True, "robot_action_busy": False, "camera_control_active": False,
            "announcement_busy": False, "announcement_queue_depth": 0,
            "kids_mode": {"active": False, "locked": False}, "last_error": ""}
    u.require_safe(safe)
    for key, value in [("motors_enabled", True), ("head_safely_folded", False),
                       ("state", "listening"), ("robot_action_busy", True), ("power_mode", "sleep")]:
        with pytest.raises(ValueError):
            u.require_safe({**safe, key: value})
    with pytest.raises(ValueError):
        u.require_safe({**safe, "kids_mode": {"active": True, "locked": False}})
    with pytest.raises(ValueError):
        u.require_safe({})


def wheel(path, version, fail=False):
    """A genuine wheel installed by pip into a slot, not a fabricated API response."""
    name = f"reachy_mini_homebody-{version}"
    target = path / (name + "-py3-none-any.whl")
    files = {"homebody/__init__.py": f'__version__ = "{version}"\n',
             "homebody/release.json": json.dumps({"version": version, "release_id": 42, "commit": "a" * 40}),
             "homebody/main.py": "raise RuntimeError('bad startup')\n" if fail else "print('healthy')\n",
             f"{name}.dist-info/METADATA": f"Metadata-Version: 2.1\nName: reachy-mini-homebody\nVersion: {version}\n",
             f"{name}.dist-info/WHEEL": ("Wheel-Version: 1.0\nGenerator: test\n"
                                       "Root-Is-Purelib: true\nTag: py3-none-any\n"),
             f"{name}.dist-info/RECORD": ""}
    with zipfile.ZipFile(target, "w") as archive:
        for key, value in files.items():
            archive.writestr(key, value)
    return target


@pytest.mark.parametrize("fail", [False, True])
def test_independent_worker_installs_and_rolls_back(tmp_path, fail):
    u = module()
    slots = tmp_path / "slots"
    old = slots / "old"
    old.mkdir(parents=True)
    (old / "retained.txt").write_text("previous artifact")
    current = tmp_path / "current"
    current.symlink_to(old)
    settings = tmp_path / "settings.json"
    settings.write_text('{"secret":"preserve me"}')
    artifact = wheel(tmp_path, "0.5.0", fail)
    m = manifest()
    m.update(size=artifact.stat().st_size, sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())
    state = tmp_path / "state"
    u.Store(state).enqueue(m, "0.4.0", [{"version": "0.5.0", "notes": "new"}])
    # The sandbox adapter is a separate test program; production never accepts arbitrary commands.
    driver = Path(__file__).with_name("fixtures") / "update_worker_sandbox.py"
    result = subprocess.run([sys.executable, str(driver), str(state), str(current), str(slots),
                             str(artifact)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    job = u.Store(state).read()["job"]
    assert job["state"] == ("rolled_back" if fail else "succeeded")
    assert current.resolve() == (old if fail else slots / job["id"])
    assert settings.read_text() == '{"secret":"preserve me"}'
    assert (old / "retained.txt").exists()


def test_digest_failure_does_not_replace_prior_slot(tmp_path):
    u = module()
    artifact = wheel(tmp_path, "0.5.0")
    with pytest.raises(ValueError, match="digest|size"):
        u.verify_wheel(artifact, manifest())
