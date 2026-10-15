"""Pinned face-model cache readiness and truthful face-tracking status.

No camera, no model load, no SDK client and no network: downloads are fixtures and sockets are
blocked where the real ``huggingface_hub`` cache lookup is exercised.
"""

from __future__ import annotations

import hashlib
import socket
import threading
from pathlib import Path

import pytest
from test_runtime_power import FakeRobot

from homebody import face_model
from homebody.runtime import HermesVoiceRuntime

FIXTURE = b"fixture onnx bytes"


@pytest.fixture
def pinned_fixture(monkeypatch):
    """Use fixture bytes as the 'pinned' digest; the real constants are asserted separately."""
    monkeypatch.setattr(face_model, "SHA256", hashlib.sha256(FIXTURE).hexdigest())
    monkeypatch.setattr(face_model, "SIZE", len(FIXTURE))


def place(cache: Path, data: bytes = FIXTURE) -> Path:
    path = face_model.cached_path(cache)
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    return path


def test_pinned_identity_matches_sdk_constants_and_public_digest():
    assert face_model.REPO == "pollen-robotics/face_detection_yunet_2026may"
    assert face_model.FILENAME == "face_detection_yunet_2026may.onnx"
    assert face_model.REVISION == "2b8e922362946a0db67e861bae0f77826980effd"
    assert face_model.SHA256 == "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0"
    assert face_model.SIZE == 229_738
    sdk = Path(__file__).resolve().parents[1] / ".artifacts" / "sdk-reference" / "face_detector.py"
    if sdk.exists():  # local read-only SDK copy; never shipped
        source = sdk.read_text()
        for value in (face_model.REPO, face_model.FILENAME, face_model.REVISION):
            assert f'"{value}"' in source


def test_cache_location_follows_hugging_face_environment(tmp_path):
    assert face_model.default_cache_dir({"HF_HUB_CACHE": str(tmp_path / "hub")}) == tmp_path / "hub"
    assert face_model.default_cache_dir({"HF_HOME": str(tmp_path / "hf")}) == tmp_path / "hf" / "hub"
    assert face_model.default_cache_dir({"XDG_CACHE_HOME": str(tmp_path)}) == tmp_path / "huggingface" / "hub"
    assert face_model.cached_path(tmp_path).relative_to(tmp_path).parts == (
        "models--pollen-robotics--face_detection_yunet_2026may", "snapshots",
        "2b8e922362946a0db67e861bae0f77826980effd", "face_detection_yunet_2026may.onnx")


def test_offline_check_states(tmp_path, pinned_fixture):
    assert face_model.check(tmp_path) == "missing"
    path = place(tmp_path)
    assert face_model.check(tmp_path) == "verified"
    path.write_bytes(b"x" * len(FIXTURE))
    assert face_model.check(tmp_path) == "mismatch"
    path.write_bytes(b"short")
    assert face_model.check(tmp_path) == "mismatch"


def test_provision_is_anonymous_pinned_and_digest_verified(tmp_path, pinned_fixture):
    calls = []

    def download(repo, filename, **kwargs):
        calls.append((repo, filename, kwargs))
        return str(place(Path(kwargs["cache_dir"])))

    assert face_model.provision(tmp_path, download=download) == "verified"
    [(repo, filename, kwargs)] = calls
    assert (repo, filename) == (face_model.REPO, face_model.FILENAME)
    assert kwargs["revision"] == face_model.REVISION and kwargs["token"] is False
    staging = Path(kwargs["cache_dir"])
    assert staging.parent == tmp_path and not staging.exists()  # private staging, removed afterwards
    installed = face_model.cached_path(tmp_path)
    assert installed.is_symlink() and installed.resolve().name == face_model.SHA256  # hub layout
    assert face_model.provision(tmp_path, download=download) == "verified" and len(calls) == 1  # no refetch


def test_provision_never_installs_unverified_bytes_or_overwrites(tmp_path, pinned_fixture):
    def tampered(repo, filename, **kwargs):
        return str(place(Path(kwargs["cache_dir"]), b"X" * len(FIXTURE)))

    def elsewhere(repo, filename, **kwargs):
        return str(place(Path(kwargs["cache_dir"]) / "other"))

    for name, fetch in (("tampered", tampered), ("elsewhere", elsewhere)):
        target = tmp_path / name
        assert face_model.provision(target, download=fetch) == "mismatch"
        assert face_model.check(target) == "missing"  # nothing reached the cache
        assert not any(p.name.startswith(".homebody-face-model-") for p in target.iterdir())

    existing = tmp_path / "existing"
    place(existing, b"Y" * len(FIXTURE))

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("an existing entry must not be refetched or overwritten")

    assert face_model.provision(existing, download=must_not_run) == "mismatch"
    assert face_model.cached_path(existing).read_bytes() == b"Y" * len(FIXTURE)
    dangling = tmp_path / "dangling"
    link = face_model.cached_path(dangling)
    link.parent.mkdir(parents=True)
    link.symlink_to("missing-blob")
    assert face_model.provision(dangling, download=must_not_run) == "unreadable"


def test_provision_failure_is_categorical_in_cli(tmp_path, capsys, monkeypatch):
    def unauthorized(*_args, **_kwargs):
        raise PermissionError("401 Client Error: Unauthorized for url secret-ish detail")

    monkeypatch.setattr(face_model, "provision", lambda cache: unauthorized())
    assert face_model.main(["provision", "--cache-dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "failed (PermissionError)" in err and "secret-ish" not in err


def test_cli_check_exit_codes(tmp_path, capsys, pinned_fixture):
    assert face_model.main(["check", "--cache-dir", str(tmp_path)]) == 1
    assert "missing" in capsys.readouterr().out
    place(tmp_path)
    assert face_model.main(["check", "--cache-dir", str(tmp_path)]) == 0
    assert "verified" in capsys.readouterr().out


def test_real_hub_download_serves_pinned_commit_from_cache_without_network(tmp_path, monkeypatch, pinned_fixture):
    """The daemon's exact SDK call shape (no token argument) is answered by a preprovisioned cache."""
    hub = pytest.importorskip("huggingface_hub")
    # Layout written by huggingface_hub: blob + snapshot entry for the exact commit.
    blob = tmp_path / f"models--{face_model.REPO.replace('/', '--')}" / "blobs" / face_model.SHA256
    blob.parent.mkdir(parents=True)
    blob.write_bytes(FIXTURE)
    snapshot = face_model.cached_path(tmp_path)
    snapshot.parent.mkdir(parents=True)
    snapshot.symlink_to(blob)

    def refuse(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setenv("HF_TOKEN", "hf_stale_fixture_token")  # a stale implicit token must not matter
    path = hub.hf_hub_download(face_model.REPO, face_model.FILENAME, revision=face_model.REVISION,
                               cache_dir=str(tmp_path))
    assert Path(path).read_bytes() == FIXTURE
    assert face_model.check(tmp_path) == "verified"


class FailingRobot(FakeRobot):
    def start_head_tracking(self, weight: float = 1.0) -> None:
        raise ConnectionError("daemon unreachable")


def test_tracking_status_is_requested_not_running_and_failures_are_explicit(monkeypatch):
    monkeypatch.setattr(face_model, "check", lambda cache_dir=None: "missing")
    robot = FakeRobot()
    runtime = HermesVoiceRuntime(robot, threading.Event())
    status = runtime.status()
    assert status["face_tracking_state"] == "off" and status["face_model_app_cache"] == "unchecked"
    runtime._set_face_tracking(True, weight=0.5)
    status = runtime.status()
    assert robot.tracking_started == [0.5]
    # Command sent, daemon health unknown: requested, never "running"; safety still treats it as active.
    assert status["face_tracking_active"] is True and status["face_tracking_state"] == "requested"
    assert status["face_model_app_cache"] == "missing"
    runtime._set_face_tracking(False)
    assert runtime.status()["face_tracking_state"] == "off" and robot.tracking_stops == 1

    failing = HermesVoiceRuntime(FailingRobot(), threading.Event())
    failing._set_face_tracking(True, weight=0.5)
    status = failing.status()
    assert status["face_tracking_active"] is False and status["face_tracking_state"] == "command_failed"
