from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import homebody.main as main_module
from homebody.config import AppConfig
from homebody.main import Homebody


@pytest.mark.parametrize('path', ['/api/models', '/api/voice-options'])
def test_unconfigured_catalog_is_setup_state_not_upstream_failure(monkeypatch, path):
    monkeypatch.setattr(main_module, 'load_config', lambda: AppConfig())
    bridge = Mock(side_effect=AssertionError('must not contact unconfigured bridge'))
    monkeypatch.setattr(main_module, 'HermesBridgeClient', bridge)
    response = TestClient(Homebody(False).settings_app).get(path)
    assert response.status_code == 200
    assert response.json()['configured'] is False
    assert 'Connect your agent' in response.json()['detail']
    bridge.assert_not_called()


def test_unconfigured_test_connection_reports_setup_required(monkeypatch):
    monkeypatch.setattr(main_module, 'load_config', lambda: AppConfig())
    response = TestClient(Homebody(False).settings_app).post('/api/test-connection')
    assert response.status_code == 409
    assert 'Connect your agent' in response.json()['detail']


@pytest.mark.parametrize('cached', [True, False])
def test_public_motion_download_does_not_use_saved_credentials(monkeypatch, cached):
    import sys
    from types import ModuleType

    import reachy_mini.motion.recorded_move as recorded

    from homebody.robot_tools import _default_library_factory

    # CI intentionally omits native SDK/transitive packages. Mock this boundary
    # without requiring or contacting the real Hugging Face client.
    hub = ModuleType('huggingface_hub')
    errors = ModuleType('huggingface_hub.errors')

    class LocalEntryNotFoundError(Exception):
        pass

    errors.LocalEntryNotFoundError = LocalEntryNotFoundError
    download = Mock(return_value='/cache/public')
    if not cached:
        download.side_effect = [LocalEntryNotFoundError(), '/cache/public']
    hub.snapshot_download = download
    hub.errors = errors
    monkeypatch.setitem(sys.modules, 'huggingface_hub', hub)
    monkeypatch.setitem(sys.modules, 'huggingface_hub.errors', errors)
    library = Mock()
    monkeypatch.setattr(recorded, 'RecordedMoves', library)
    _default_library_factory()
    dataset = 'pollen-robotics/reachy-mini-emotions-library'
    download.assert_any_call(dataset, repo_type='dataset', token=False, local_files_only=True)
    assert download.call_count == (1 if cached else 2)
    if not cached:
        download.assert_called_with(dataset, repo_type='dataset', token=False)
    library.assert_called_once_with(dataset)
