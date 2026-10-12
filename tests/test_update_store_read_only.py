"""Status/maintenance reads must not rewrite private update state or change its ownership."""

import fcntl
import json
import threading

import pytest

from homebody import updates
from homebody.update_api import UpdateService


def metadata(path):
    value = path.stat()
    return (value.st_ino, value.st_uid, value.st_gid, value.st_mode, value.st_mtime_ns, value.st_ctime_ns)


def test_read_uninitialized_store_does_not_create_state_or_lock(tmp_path):
    root = tmp_path / "not-created"
    assert updates.Store(root).read() == {"job": None, "history": []}
    assert not root.exists()


@pytest.mark.parametrize("existing_lock", [True, False])
def test_read_preserves_existing_journal_bytes_inode_mode_owner_and_timestamps(tmp_path, existing_lock):
    store = updates.Store(tmp_path)
    content = b'{ "history": [], "job": null }\n'
    store.path.write_bytes(content)
    store.path.chmod(0o600)
    if existing_lock:
        (tmp_path / "journal.lock").write_bytes(b"")
    before = metadata(store.path)
    lock_before = metadata(tmp_path / "journal.lock") if existing_lock else None
    for _ in range(3):
        state = store.read()
        state["history"].append({"local": "not persisted"})
    assert store.path.read_bytes() == content
    assert metadata(store.path) == before
    if existing_lock:
        assert metadata(tmp_path / "journal.lock") == lock_before
    else:
        assert not (tmp_path / "journal.lock").exists()


def test_read_never_calls_atomic_writer_and_works_with_read_only_files(tmp_path, monkeypatch):
    store = updates.Store(tmp_path)
    store.path.write_text('{"job":null,"history":[]}')
    lock = tmp_path / "journal.lock"
    lock.write_text("")
    store.path.chmod(0o400)
    lock.chmod(0o400)
    tmp_path.chmod(0o500)

    def unexpected_write(*args):
        raise AssertionError("A read must not replace the journal as the inspecting user")

    monkeypatch.setattr(updates, "atomic_json", unexpected_write)
    try:
        assert store.read() == {"job": None, "history": []}
    finally:
        tmp_path.chmod(0o700)
        store.path.chmod(0o600)
        lock.chmod(0o600)


def test_read_waits_for_writer_commit_using_existing_shared_lock(tmp_path):
    store = updates.Store(tmp_path)
    updates.atomic_json(store.path, {"job": None, "history": []})
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    result = []

    def writer():
        with store.transaction() as data:
            data["history"].append({"committed": True})
            entered.set()
            assert release.wait(2)

    def reader():
        result.append(store.read())
        finished.set()

    write = threading.Thread(target=writer)
    read = threading.Thread(target=reader)
    write.start()
    assert entered.wait(2)
    read.start()
    try:
        assert not finished.wait(0.05), "Reader skipped an existing exclusive journal lock"
    finally:
        release.set()
        write.join(2)
        read.join(2)
    assert not write.is_alive() and not read.is_alive()
    assert result == [{"job": None, "history": [{"committed": True}]}]


def test_update_status_is_observation_not_a_journal_write(tmp_path, monkeypatch):
    store = updates.Store(tmp_path)
    updates.atomic_json(store.path, {"job": None, "history": []})
    (tmp_path / "journal.lock").write_text("")
    before = metadata(store.path)
    service = UpdateService(tmp_path)
    monkeypatch.setattr(service, "store", lambda: store)
    monkeypatch.setattr(service, "supported", lambda: (True, ""))
    for _ in range(3):
        assert service.status()["error"] == ""
    assert metadata(store.path) == before


def test_read_invalid_journal_fails_without_replacing_it(tmp_path):
    store = updates.Store(tmp_path)
    store.path.write_text("not valid JSON")
    before = metadata(store.path)
    with pytest.raises(json.JSONDecodeError):
        store.read()
    assert metadata(store.path) == before


def test_read_releases_shared_lock(tmp_path):
    store = updates.Store(tmp_path)
    updates.atomic_json(store.path, {"job": None, "history": []})
    lock = tmp_path / "journal.lock"
    lock.write_text("")
    store.read()
    with lock.open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
