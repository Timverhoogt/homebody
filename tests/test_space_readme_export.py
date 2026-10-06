from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_github_readme_is_clean_and_space_export_preserves_metadata(tmp_path: Path) -> None:
    body = (ROOT / "README.md").read_text(encoding="utf-8")
    assert body.startswith("# Homebody")
    assert "sdk: static" not in body
    assert "docs/assets/homebody-banner.webp" in body
    for name in ("README.md", "hf-space-metadata.yaml"):
        shutil.copy2(ROOT / name, tmp_path / name)
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools/prepare_space_readme.py"), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not result.stderr
    metadata = (ROOT / "hf-space-metadata.yaml").read_text(encoding="utf-8").strip()
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == f"---\n{metadata}\n---\n\n{body}"
    assert (ROOT / "README.md").read_text(encoding="utf-8") == body
    workflow = (ROOT / ".github/workflows/sync-hf-space.yml").read_text(encoding="utf-8")
    assert 'python3 tools/prepare_space_readme.py "$RUNNER_TEMP/space"' in workflow


def test_export_rejects_double_front_matter(tmp_path: Path) -> None:
    from tools.prepare_space_readme import prepare

    (tmp_path / "README.md").write_text("---\nsdk: static\n---\n", encoding="utf-8")
    (tmp_path / "hf-space-metadata.yaml").write_text("sdk: static\n", encoding="utf-8")
    with pytest.raises(ValueError, match="already has front matter"):
        prepare(tmp_path)
