from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from companion.reachy_agent_broker import (
    BrokerConfig,
    BrokerUnavailableError,
    BrokerValidationError,
    ReachyAgentBroker,
)
from companion.reachy_projects import ProjectCatalog


def context(**changes):
    return {
        "capability_profile": "agent",
        "adult_ui_unlocked": True,
        "kids_mode_active": False,
        "power_mode": "awake",
        "privacy_enabled": True,
        "emergency_stop_active": False,
        "robot_available": True,
        "session_generation": 7,
        "requested_session_generation": 7,
        "explicit_private_intent": True,
        **changes,
    }


def catalog(directory: Path, **changes):
    return ProjectCatalog.from_json(
        json.dumps(
            {
                "photo": {
                    "title": "Photo agent",
                    "root": str(directory),
                    "roadmap": "roadmap.md",
                    **changes,
                }
            }
        )
    )


async def execute(broker, capability, arguments, **changes):
    state = context(**changes)
    await broker.establish_session("reachy-a", state)
    return await broker.execute(
        {
            "request_id": "project-read-test",
            "capability_id": capability,
            "arguments": arguments,
            "context": state,
        },
        None,
        device_id="reachy-a",
    )


def test_catalog_disabled_by_default_and_host_owned(tmp_path, monkeypatch):
    monkeypatch.delenv("REACHY_AGENT_PROJECTS_JSON", raising=False)
    broker = ReachyAgentBroker(BrokerConfig())
    assert "read_project_roadmap" not in {item["id"] for item in broker.manifest()}
    with pytest.raises(BrokerUnavailableError):
        asyncio.run(execute(broker, "read_project_roadmap", {"project_id": "photo"}))
    monkeypatch.setenv(
        "REACHY_AGENT_PROJECTS_JSON",
        json.dumps(
            {
                "photo": {
                    "title": "Photo agent",
                    "root": str(tmp_path),
                    "roadmap": "roadmap.md",
                }
            }
        ),
    )
    assert BrokerConfig.from_env().projects.public_catalog() == [
        {"project_id": "photo", "title": "Photo agent"},
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"root": "relative"},
        {"roadmap": "/etc/passwd"},
        {"roadmap": "../outside.md"},
        {"roadmap": "x/../roadmap.md"},
        {"roadmap": "secret.env"},
        {"title": ""},
        {"title": True},
        {"root": []},
        {"unexpected": "field"},
    ],
)
def test_catalog_rejects_invalid_configuration(tmp_path, changes):
    with pytest.raises(ValueError):
        catalog(tmp_path, **changes)


@pytest.mark.parametrize("identifier", ["Photo", "../photo", "photo space", "", "x" * 33])
def test_catalog_preserves_and_validates_ids(tmp_path, identifier):
    with pytest.raises(ValueError):
        ProjectCatalog.from_json(
            json.dumps(
                {
                    identifier: {
                        "title": "Photo",
                        "root": str(tmp_path),
                        "roadmap": "roadmap.md",
                    }
                }
            )
        )


def test_catalog_limits_count(tmp_path):
    with pytest.raises(ValueError):
        ProjectCatalog.from_json(
            json.dumps(
                {
                    f"p{i}": {
                        "title": "Photo",
                        "root": str(tmp_path),
                        "roadmap": "roadmap.md",
                    }
                    for i in range(33)
                }
            )
        )


def test_real_file_read_is_fresh_bounded_redacted_and_evidenced(tmp_path):
    roadmap = tmp_path / "roadmap.md"
    raw = b"# Roadmap\nDone: importer\nNext: colour pipeline\npassword=private-value\n"
    roadmap.write_bytes(raw)
    broker = ReachyAgentBroker(BrokerConfig(projects=catalog(tmp_path)))
    result = asyncio.run(execute(broker, "read_project_roadmap", {"project_id": "photo"}))
    assert result["read_only"] is True and result["side_effect"] is False
    assert result["data"]["execution_available"] is False
    assert "2|Done: importer" in result["data"]["text"]
    assert "private-value" not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)
    assert result["data"]["source_ranges"] == [[1, 4]]
    assert result["data"]["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["evidence"][0]["source"] == "project_roadmap"
    assert result["evidence"][0]["source_sha256"] == result["data"]["source_sha256"]
    roadmap.write_text("Next: regression tests\n")
    refreshed = asyncio.run(execute(broker, "read_project_roadmap", {"project_id": "photo"}))
    assert refreshed["data"]["text"] == "1|Next: regression tests"
    assert refreshed["data"]["source_sha256"] != result["data"]["source_sha256"]


def test_catalog_does_not_disclose_roots_or_status(tmp_path):
    broker = ReachyAgentBroker(BrokerConfig(projects=catalog(tmp_path)))
    result = asyncio.run(execute(broker, "list_projects", {}))
    assert result["data"] == {"projects": [{"project_id": "photo", "title": "Photo agent"}]}
    assert str(tmp_path) not in json.dumps(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"kids_mode_active": True},
        {"adult_ui_unlocked": False},
        {"privacy_enabled": False},
        {"emergency_stop_active": True},
        {"power_mode": "meeting"},
        {"power_mode": "sleep"},
        {"capability_profile": "conversation"},
        {"requested_session_generation": 6},
        {"explicit_private_intent": False},
    ],
)
def test_roadmap_read_requires_current_owner_intent(tmp_path, changes):
    broker = ReachyAgentBroker(BrokerConfig(projects=catalog(tmp_path)))
    with pytest.raises(BrokerValidationError):
        asyncio.run(execute(broker, "read_project_roadmap", {"project_id": "photo"}, **changes))


@pytest.mark.parametrize(
    "arguments",
    [
        {"project_id": "unknown"},
        {"project_id": "photo", "path": "/etc/passwd"},
        {"project_id": "../photo"},
        {"project_id": "Photo"},
        {"project_id": True},
    ],
)
def test_caller_cannot_select_unregistered_project_or_path(tmp_path, arguments):
    broker = ReachyAgentBroker(BrokerConfig(projects=catalog(tmp_path)))
    with pytest.raises(BrokerValidationError):
        asyncio.run(execute(broker, "read_project_roadmap", arguments))


@pytest.mark.parametrize("kind", ["file_symlink", "directory_symlink", "root_symlink", "fifo", "large", "binary"])
def test_unsafe_roadmaps_fail_closed(tmp_path, kind):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("private material")
    roadmap = root / "roadmap.md"
    relative = "roadmap.md"
    if kind == "file_symlink":
        roadmap.symlink_to(outside)
    elif kind == "directory_symlink":
        (root / "docs").symlink_to(tmp_path, target_is_directory=True)
        relative = "docs/outside.md"
    elif kind == "root_symlink":
        root.rmdir()
        root.symlink_to(tmp_path, target_is_directory=True)
        relative = "outside.md"
    elif kind == "fifo":
        os.mkfifo(roadmap)
    elif kind == "large":
        roadmap.write_bytes(b"x" * 512_001)
    elif kind == "binary":
        roadmap.write_bytes(b"\xff\xfe")
    broker = ReachyAgentBroker(BrokerConfig(projects=catalog(root, roadmap=relative)))
    with pytest.raises(BrokerValidationError):
        asyncio.run(execute(broker, "read_project_roadmap", {"project_id": "photo"}))


def test_large_roadmap_has_truthful_line_ranges_and_omission(tmp_path):
    (tmp_path / "roadmap.md").write_text("\n".join(f"line {i}: " + "x" * 80 for i in range(500)))
    broker = ReachyAgentBroker(BrokerConfig(projects=catalog(tmp_path)))
    result = asyncio.run(execute(broker, "read_project_roadmap", {"project_id": "photo"}))
    data = result["data"]
    assert data["truncated"] is True and data["line_count"] == 500
    assert len(data["text"]) <= 18_000
    assert data["source_ranges"][0][0] == 1 and data["source_ranges"][-1][-1] == 500
    assert "1|line 0:" in data["text"] and "500|line 499:" in data["text"]
    assert "omitted lines" in data["text"]
