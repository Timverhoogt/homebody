"""Preserve the app README metadata required by Reachy SDK 1.9's official validator."""
from pathlib import Path


def test_app_readme_keeps_reachy_metadata_before_the_article():
    text = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert text.startswith("---\n"), "The official app validator requires README metadata"
    metadata = text.split("---", 2)[1]
    assert "title: Homebody" in metadata
    assert {"- reachy_mini", "- reachy_mini_python_app"} <= {line.strip() for line in metadata.splitlines()}
