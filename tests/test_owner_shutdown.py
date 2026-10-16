"""Real Owner/CSRF middleware over the phone's Tailnet HTTPS origin; no hardware."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from homebody import host_shutdown
from homebody.config import AppConfig
from homebody.main import Homebody

ORIGIN = 'https://timverhoogtnano.tail51823f.ts.net'


@pytest.fixture
def app(monkeypatch, tmp_path):
    import homebody.main as main
    monkeypatch.setattr(main, 'default_config_path', lambda: tmp_path/'settings.json')
    monkeypatch.setattr(main, 'load_config', lambda: AppConfig())
    monkeypatch.setattr(main, 'host', lambda: SimpleNamespace(shutdown_supported=True, label='NVIDIA Jetson'))
    robot = Homebody(False)
    events = []
    runtime = SimpleNamespace(control_ready=True, kids_controls_locked=False,
                              set_power_mode=lambda mode: events.append(mode))
    robot._runtime = runtime
    def ipc(op):
        events.append(op)
        return {'protocol': 1, 'state': 'ready'} if op == 'CHECK' else {
            'protocol': 1, 'state': 'scheduled', 'delay_seconds': 5}
    monkeypatch.setattr(host_shutdown, 'request', ipc)
    client = TestClient(robot.settings_app, base_url=ORIGIN)
    code = robot.settings_app.state.owner_store.provision(ORIGIN)
    return robot, client, code, events


def paired(app):
    robot, client, code, events = app
    assert client.post('/api/owner/pair', json={'code':code,'name':'Phone'},
                       headers={'Origin':ORIGIN}).status_code == 200
    csrf = client.get('/api/owner/session').json()['csrf']
    return robot, client, {'Origin':ORIGIN,'X-Homebody-CSRF':csrf}, events


def test_tailnet_owner_csrf_origin_and_readiness(app):
    _, client, _, events = app
    assert client.post('/api/shutdown', json={'confirm':'shutdown'}).status_code == 401
    assert client.get('/api/shutdown/readiness').status_code == 401
    _, client, headers, events = paired(app)
    for h in ({}, {'Origin':ORIGIN}, {**headers,'Origin':'https://evil.test'},
              {**headers,'X-Forwarded-Host':'evil.test'}):
        assert client.post('/api/shutdown', json={'confirm':'shutdown'}, headers=h).status_code == 403
    assert not events
    read = client.get('/api/shutdown/readiness')
    assert read.status_code == 200 and read.json()['state'] == 'ready'
    assert read.headers['cache-control'] == 'no-store'
    assert events == ['CHECK']
    events.clear()
    result = client.post('/api/shutdown',json={'confirm':'shutdown'},headers=headers)
    assert result.status_code == 202 and result.json() == {
        'ok':True,'protocol':1,'state':'scheduled','delay_seconds':5}
    assert events == ['CHECK','sleep','SCHEDULE']
    assert result.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('locked', [False, True])
def test_confirmation_and_kids_management_lock(app, locked):
    robot, client, headers, events = paired(app)
    robot._runtime.kids_controls_locked = locked
    result = client.post('/api/shutdown',json={'confirm':'no'},headers=headers)
    assert result.status_code == (423 if locked else 400)
    assert not events
    if locked:
        assert client.get('/api/shutdown/readiness').status_code == 423
        assert client.post('/api/shutdown',json={'confirm':'shutdown'},headers=headers).status_code == 423
        assert not events


@pytest.mark.parametrize('runtime', ['absent','starting','fold_failure','missing_helper','refused','unknown'])
def test_no_false_success_and_preflight_before_fold(app, monkeypatch, runtime):
    robot, client, headers, events = paired(app)
    if runtime == 'absent':
        robot._runtime = None
    elif runtime == 'starting':
        robot._runtime.control_ready = False
    elif runtime == 'fold_failure':
        def fail(mode):
            events.append(mode)
            raise RuntimeError('fold unconfirmed')
        robot._runtime.set_power_mode = fail
    original = host_shutdown.request
    def ipc(op):
        if runtime == 'missing_helper' or (runtime == 'refused' and op == 'SCHEDULE'):
            events.append(op)
            raise host_shutdown.ShutdownRefused('not provisioned or unsafe pose')
        if runtime == 'unknown' and op == 'SCHEDULE':
            events.append(op)
            raise host_shutdown.ShutdownUnknown('acknowledgement lost')
        return original(op)
    monkeypatch.setattr(host_shutdown,'request',ipc)
    result = client.post('/api/shutdown',json={'confirm':'shutdown'},headers=headers)
    assert result.status_code == (502 if runtime == 'unknown' else
                                  503 if runtime in ('missing_helper','refused') else 409)
    assert result.json().get('ok') is not True
    if runtime == 'missing_helper':
        assert events == ['CHECK']
    if runtime in ('absent','starting','fold_failure'):
        assert 'SCHEDULE' not in events
    if runtime in ('missing_helper','refused'):
        assert result.headers['X-Homebody-Shutdown'] == 'refused'


def test_revoked_phone_cannot_schedule(app):
    robot, client, headers, events = paired(app)
    session = client.get('/api/owner/session').json()
    assert client.post('/api/owner/revoke',json={'device_id':session['device_id']},headers=headers).status_code == 200
    assert client.post('/api/shutdown',json={'confirm':'shutdown'},headers=headers).status_code == 401
    assert not events
