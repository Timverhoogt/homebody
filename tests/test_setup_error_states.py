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


def test_public_motion_download_does_not_use_saved_credentials(monkeypatch):
    import huggingface_hub
    import reachy_mini.motion.recorded_move as recorded

    from homebody.robot_tools import _default_library_factory

    download = Mock(return_value='/cache/public')
    library = Mock()
    monkeypatch.setattr(huggingface_hub, 'snapshot_download', download)
    monkeypatch.setattr(recorded, 'RecordedMoves', library)
    _default_library_factory()
    assert download.call_args.kwargs['token'] is False
    assert download.call_args.kwargs['repo_type'] == 'dataset'
    library.assert_called_once_with('pollen-robotics/reachy-mini-emotions-library')
