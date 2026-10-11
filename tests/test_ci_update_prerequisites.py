"""Keep actual pip installation and non-isolated wheel-build tests enabled in fresh uv environments."""
import tomllib
from pathlib import Path


def test_ci_and_dev_environments_provision_real_installer_and_build_backend():
    root=Path(__file__).resolve().parents[1]
    workflow=(root/'.github/workflows/ci.yml').read_text()
    install=workflow.split('      - name: Install test environment',1)[1].split('      - name: Lint',1)[0]
    for tool in ('pip','setuptools'):
        assert f'"{tool}>=' in install, f'The uv CI interpreter needs explicit {tool} for real lifecycle tests'
    assert ' wheel' in install
    project=tomllib.loads((root/'pyproject.toml').read_text())
    development=project['dependency-groups']['dev']
    for tool in ('pip','setuptools','wheel'):
        assert any(item==tool or item.startswith(tool+'>=') for item in development), tool
