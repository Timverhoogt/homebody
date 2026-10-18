"""Chromium phone Tailnet-HTTPS fixtures: no real network, motors, media or poweroff."""
# ruff: noqa: F811
from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401

ORIGIN = 'https://timverhoogtnano.tail51823f.ts.net'
ACK = {'ok': True, 'protocol': 1, 'state': 'scheduled', 'delay_seconds': 5}


def phone(ui, width=390):
    page, state, posts, errors = ui
    page.set_viewport_size({'width':width, 'height':844})
    page.emulate_media(color_scheme='dark', reduced_motion='reduce')
    # The imported HTTP fixture mocks all network traffic. Its JSON fallback is not
    # a real HTTPS service-worker script; installation is outside this test scope.
    page.add_init_script("navigator.serviceWorker.register = async () => ({})")
    page.route('**/api/status', lambda route: route.fulfill(json={
        'config':state['config'], 'runtime':state['runtime'],
        'host':{'shutdown_supported':True, 'label':'NVIDIA Jetson', 'kind':'jetson'}}))
    page.goto(ORIGIN + '/')
    page.wait_for_function("HomebodyOwner.isOwner() && !document.querySelector('main').hidden")
    page.evaluate("document.getElementById('tab-settings').click()")  # behind More on phones
    page.wait_for_function("!document.getElementById('shutdown-button').hidden")
    page.on('dialog', lambda dialog: dialog.accept('SHUTDOWN'))
    return page, state, posts, errors


def click(page):
    page.locator('#shutdown-button').evaluate("button => button.scrollIntoView({block:'center'})")
    page.click('#shutdown-button')


@pytest.mark.parametrize('width', [320,390,1440])
def test_verified_ack_and_expected_offline_no_retry(ui, width):
    page, state, posts, errors = phone(ui, width)
    requests = []
    def schedule(route):
        requests.append(route.request)
        route.fulfill(status=202,json=ACK)
    page.route('**/api/shutdown',schedule)
    click(page)
    page.wait_for_function("document.getElementById('power-message').textContent.includes('Shutdown scheduled')")
    assert len(requests) == 1
    request = requests[0]
    assert request.url == ORIGIN+'/api/shutdown'
    assert request.headers['x-homebody-csrf'] == 'fixture'
    assert request.headers['origin'] == ORIGIN
    assert request.post_data_json == {'confirm':'shutdown'}
    page.route('**/api/status', lambda route: route.abort('failed'))
    page.evaluate('refreshStatus()')
    page.wait_for_function("document.body.dataset.runtimeState === 'disconnected'")
    assert 'go offline shortly; that is expected' in page.locator('#power-message').text_content()
    assert page.locator('#shutdown-button').is_disabled()
    page.evaluate("document.getElementById('shutdown-button').dispatchEvent(new Event('click'))")
    assert len(requests) == 1 and not posts and not errors
    if width == 390:
        target = Path('.artifacts/shutdown/phone-scheduled.png')
        target.parent.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(target),full_page=True)


@pytest.mark.parametrize('kind', ['502','503','504','disconnect','false_success','bad_delay'])
def test_proxy_disconnect_and_unverified_ack_are_unknown_never_auto_retry(ui, kind):
    page, state, posts, errors = phone(ui)
    seen = []
    def reply(route):
        seen.append(route.request)
        if kind == 'disconnect':
            route.abort('failed')
        elif kind in ('502','503','504'):
            route.fulfill(status=int(kind), content_type='text/html', body='Proxy unavailable')
        elif kind == 'bad_delay':
            route.fulfill(status=202,json={**ACK,'delay_seconds':0})
        else:
            route.fulfill(json={'ok':True,'state':'shutting_down'})
    page.route('**/api/shutdown',reply)
    click(page)
    page.wait_for_function("document.getElementById('power-message').textContent.includes('outcome unknown')")
    text = page.locator('#power-message').text_content()
    assert 'not started' not in text and 'may already be shutting down' in text
    assert page.locator('#shutdown-button').is_disabled()
    page.evaluate("document.getElementById('shutdown-button').dispatchEvent(new Event('click'))")
    assert len(seen) == 1 and not posts and not errors


@pytest.mark.parametrize('status,detail', [(503,'Host helper is not provisioned'),
                                            (403,'CSRF rejected'), (423,'Parent controls are locked')])
def test_explicit_helper_refusal_and_deliberate_retry(ui, status, detail):
    page, state, posts, errors = phone(ui)
    page.route('**/api/shutdown',lambda route: route.fulfill(status=status,
        headers={'X-Homebody-Shutdown':'refused'},json={'detail':detail}))
    click(page)
    page.wait_for_function("document.getElementById('power-message').textContent.includes('Shutdown refused')")
    assert page.locator('#shutdown-button').is_enabled()
    page.route('**/api/shutdown',lambda route: route.fulfill(status=202,json=ACK))
    page.get_by_role('button', name='Dismiss notification').click()
    click(page)
    page.wait_for_function("document.getElementById('power-message').textContent.includes('Shutdown scheduled')")
    assert not errors


def test_pending_duplicate_and_late_ack_after_real_owner_loss(ui):
    page, state, posts, errors = phone(ui)
    held = []
    def hold(route):
        held.append(route)
        page.evaluate('window.shutdownHeld = true')
    page.route('**/api/shutdown',hold)
    click(page)
    page.wait_for_function('window.shutdownHeld === true')
    assert page.locator('#shutdown-button').is_disabled()
    page.evaluate("document.getElementById('shutdown-button').dispatchEvent(new Event('click'))")
    assert len(held) == 1
    page.route('**/api/agent/workspace/native-badge',lambda route: route.fulfill(status=401,json={'detail':'revoked'}))
    page.evaluate("fetch('/api/agent/workspace/native-badge')")
    page.wait_for_function('HomebodyOwner.isOwner() === false')
    held[0].fulfill(status=202,json=ACK)
    page.wait_for_timeout(100)
    assert 'Shutdown scheduled' not in page.locator('#power-message').text_content()
    assert page.locator('main').get_attribute('hidden') is not None
    assert not posts and not errors
