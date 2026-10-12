"""Release artifact generation must bind package/changelog/version to the published release."""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def test_release_generator_builds_verifiable_reproducible_metadata(tmp_path):
    root = tmp_path / "source"
    package = root / "homebody"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="reachy-mini-homebody"\nversion="0.5.0"\n')
    (package / "__init__.py").write_text('__version__ = "0.5.0"\n')
    (root / "CHANGELOG.md").write_text('## [Unreleased]\n\n## [0.5.0] - 2026-10-04\n\n- Good changes.\n')
    script = Path(__file__).resolve().parents[1] / "tools/release_manifest.py"
    args = [sys.executable, str(script), "--root", str(root), "--release-id", "42", "--commit", "a" * 40]
    result = subprocess.run([*args, "--prepare"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((package / "release.json").read_text())["release_id"] == 42
    from test_updates import wheel
    artifact = wheel(tmp_path, "0.5.0")
    output = tmp_path / "manifest.json"
    result = subprocess.run([*args, "--wheel", str(artifact), "--output", str(output)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    first = output.read_bytes()
    result = subprocess.run([*args, "--wheel", str(artifact), "--output", str(output)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_bytes() == first
    data = json.loads(first)
    assert data["notes"].strip() == "- Good changes."
    assert data["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()


def test_real_project_build_contains_update_ui_and_bound_identity(tmp_path):
    """Build the actual project in an isolated source copy; no release ID is published."""
    repo = Path(__file__).resolve().parents[1]
    root = tmp_path / "source"
    root.mkdir()
    for name in ("homebody", "companion"):
        shutil.copytree(repo / name, root / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("pyproject.toml", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(repo / name, root / name)
    import tomllib

    current_version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    (root / "CHANGELOG.md").write_text(
        f"## [{current_version}]\n\n- Isolated build fixture; not a published release.\n"
    )
    script = repo / "tools/release_manifest.py"
    args = [sys.executable, str(script), "--root", str(root), "--release-id", "42", "--commit", "a" * 40]
    subprocess.run([*args, "--prepare"], check=True, capture_output=True)
    subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation", str(root)],
                   check=True, capture_output=True, cwd=root)
    artifact, = (root / "dist").glob("*.whl")
    output = root / "dist/homebody-update.json"
    subprocess.run([*args, "--wheel", str(artifact), "--output", str(output)], check=True, capture_output=True)
    from zipfile import ZipFile

    from homebody.updates import verify_wheel
    verify_wheel(artifact, json.loads(output.read_text()))
    with ZipFile(artifact) as archive:
        assert {"homebody/update_worker.py", "homebody/update_api.py", "homebody/static/updates.js",
                "homebody/release.json"} <= set(archive.namelist())
