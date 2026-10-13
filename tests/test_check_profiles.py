"""Conservative selection must not confuse a focused developer check with a release gate."""

import pytest

from tools.checks import select_changed


@pytest.fixture
def repository(tmp_path):
    names = [
        "test_runtime.py",
        "test_voice_live.py",
        "test_kids_mode.py",
        "test_owner_boundary.py",
        "test_github_repositories.py",
        "test_workspace.py",
        "test_docs.py",
        "test_other.py",
        "test_ui_motion_browser.py",
        "test_workspace_browser.py",
        "test_kids_status_browser.py",
    ]
    directory = tmp_path / "tests"
    directory.mkdir()
    for name in names:
        (directory / name).write_text("# selection fixture\n")
    return tmp_path


def test_voice_change_keeps_cross_mode_and_owner_coverage(repository):
    units, browsers, reason = select_changed(["homebody/voice_live.py"], repository)
    assert {
        "tests/test_voice_live.py",
        "tests/test_kids_mode.py",
        "tests/test_runtime.py",
        "tests/test_owner_boundary.py",
    } <= set(units)
    assert "tests/test_other.py" not in units
    assert "tests/test_kids_status_browser.py" in browsers


@pytest.mark.parametrize(
    "path",
    ["homebody/config.py", "tests/conftest.py", "pyproject.toml", "homebody/new_shared_helper.py", "tools/checks.py"],
)
def test_shared_and_unknown_changes_fall_back_to_all_units(repository, path):
    units, browsers, reason = select_changed([path], repository)
    assert "tests/test_other.py" in units
    assert all(not p.endswith("_browser.py") for p in units)
    assert "tests/test_workspace_browser.py" in browsers


def test_explicit_new_test_is_included(repository):
    assert select_changed(["tests/test_other.py"], repository)[0] == ["tests/test_other.py"]


def test_docs_only_selection_is_small(repository):
    assert select_changed(["docs/setup.md"], repository)[0] == ["tests/test_docs.py"]


def test_no_changes_is_not_an_empty_green_gate(repository):
    assert "tests/test_other.py" in select_changed([], repository)[0]


def test_unknown_plus_known_is_not_partial(repository):
    units, _, _ = select_changed(["homebody/voice_live.py", "homebody/unknown.py"], repository)
    assert "tests/test_other.py" in units
