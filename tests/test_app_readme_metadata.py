"""Preserve app-validator metadata in its Space export, not the clean GitHub README."""
import shutil
from pathlib import Path

from tools.prepare_space_readme import prepare


def test_exported_app_readme_keeps_reachy_metadata_before_the_article(tmp_path):
    root = Path(__file__).resolve().parent.parent
    original = (root / "README.md").read_text()
    assert original.startswith("# Homebody")
    for name in ("README.md", "hf-space-metadata.yaml"):
        shutil.copyfile(root / name, tmp_path / name)
    prepare(tmp_path)
    text = (tmp_path / "README.md").read_text()
    assert text.startswith("---\n"), "The official app validator requires export metadata"
    metadata = text.split("---", 2)[1]
    assert "title: Homebody" in metadata
    assert {"- reachy_mini", "- reachy_mini_python_app"} <= {line.strip() for line in metadata.splitlines()}
    assert (root / "README.md").read_text() == original
