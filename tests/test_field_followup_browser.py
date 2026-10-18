# ruff: noqa: F811 -- imported Playwright fixture
"""Integrated Chromium routes; all HTTP fulfilled locally, no robot/provider calls."""
import pytest
from test_assistant_modes_browser import ui  # noqa: F401
from test_kids_game_master_browser import hosted


@pytest.mark.parametrize('width', [390, 1280])
def test_host_end_chat_start_has_explicit_single_route_and_no_stale_consent(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({'width': width, 'height': 900})
    page.emulate_media(reduced_motion='reduce')
    page.click('#tab-kids')
    starts = []

    def start(route):
        profile = route.request.post_data_json
        starts.append(profile)
        state['runtime'] = hosted('lobby' if profile['host'] else 'playing', activity=profile['activity'])
        state['runtime']['kids_mode']['profile'] = profile
        state['runtime']['kids_mode']['host']['enabled'] = profile['host']
        route.fulfill(json={'ok': True, 'kids_mode': state['runtime']['kids_mode']})

    def stop(route):
        state['runtime'] = {'state': 'waiting_for_wake_word', 'power_mode': 'standby',
                            'kids_mode': {'active': False, 'locked': False, 'last_fold_succeeded': True}}
        route.fulfill(json={'ok': True, 'kids_mode': state['runtime']['kids_mode']})

    page.route('**/api/kids/start', start)
    page.route('**/api/kids/stop', stop)
    page.check('#kids-host-camera-consent')
    page.click('#kids-host-start-button')
    page.wait_for_function(
        "!kidsRequestPending && document.getElementById('panel-kids').dataset.kidsSession==='active'")
    assert starts[-1]['host'] is True
    page.click('#kids-stop-button')
    page.wait_for_function("!kidsRequestPending && document.getElementById('panel-kids').dataset.kidsSession==='idle'")
    assert not page.is_checked('#kids-host-camera-consent')
    page.click('[data-kids-activity="buddy"]')
    assert not page.locator('#kids-host-start-button').is_visible()
    assert not page.locator('#kids-host-camera-row').is_visible()
    assert page.locator('#kids-start-button').inner_text() == 'Start Buddy chat only'
    page.evaluate('refreshStatus()')
    assert 'Single activity' in page.locator('#kids-setup-label').text_content()
    assert page.locator('#kids-stage .hb-scene').get_attribute('data-scene') == 'buddy'
    page.click('#kids-start-button')
    page.wait_for_function(
        "!kidsRequestPending && document.getElementById('panel-kids').dataset.kidsSession==='active'")
    assert starts[-1]['host'] is False
    assert starts[-1]['activity'] == 'buddy'
    assert starts[-1]['camera_consent'] is False
    assert not page.locator('#kids-host-controls').is_visible()
    assert not any(path == '/api/kids/host/select' for path, _ in posts)
    page.click('#kids-stop-button')
    page.wait_for_function('!kidsRequestPending')
    page.click('#kids-host-setup-link')
    assert page.locator('#kids-host-start-button').is_visible()
    assert page.locator('#kids-stage .hb-scene').get_attribute('data-scene') == 'host'
    assert not page.is_checked('#kids-host-camera-consent')
    assert not errors


def test_end_is_single_flight_even_before_next_status(ui):
    page, state, posts, errors = ui
    state['runtime'] = hosted('playing', activity='story')
    page.evaluate('refreshStatus()')
    held = []
    page.route('**/api/kids/stop', lambda route: held.append(route))
    page.evaluate("document.getElementById('kids-stop-button').click(); "
                  "document.getElementById('kids-stop-button').click()")
    page.wait_for_timeout(150)
    assert len(held) == 1
    state['runtime']['kids_mode'] = {'active': False, 'locked': False}
    held[0].fulfill(json={'ok': True, 'kids_mode': state['runtime']['kids_mode']})
    page.wait_for_function('!kidsRequestPending')
    assert not errors


def test_unused_workspace_badge_stops_polling_but_real_work_and_workspace_still_poll(ui):
    page, state, posts, errors = ui
    reads = []
    page.wait_for_timeout(200)  # finish the fixture's initial discovery before installing our route
    def badge(route):
        reads.append(route.request.url)
        route.fulfill(status=423, json={'detail': 'Kids latch still active'})
    page.route('**/api/agent/workspace/native-badge', badge)
    # Enter/exit the blocked state: one discovery read, then preserve uncertainty.
    state['runtime']['kids_mode'] = {'active': True, 'locked': True}
    page.evaluate('refreshStatus()')
    state['runtime']['kids_mode'] = {}
    page.evaluate('refreshStatus()')
    page.wait_for_timeout(100)
    assert len(reads) == 1
    page.evaluate('window.fieldNow = Date.now; Date.now = () => window.fieldNow() + 60000')
    for _ in range(3):
        page.evaluate('refreshStatus()')
    assert len(reads) == 1
    assert 'unconfirmed' in page.locator('#native-work-badge').inner_text()
    assert not page.locator('#agent-stop-button').get_attribute('hidden')
    state['runtime']['agent']['profile'] = 'agent'
    page.evaluate('refreshStatus()')
    page.wait_for_timeout(100)
    assert len(reads) == 2
    state['runtime']['kids_mode'] = {'active': True, 'locked': True}
    page.evaluate('refreshStatus()')
    page.evaluate('Date.now = () => window.fieldNow() + 120000; refreshStatus()')
    assert len(reads) == 2
    assert not errors


def test_known_work_keeps_polling_after_transient_failure_until_confirmed_idle(ui):
    page, state, posts, errors = ui
    page.wait_for_timeout(200)
    reads = []
    response = {'running': True, 'status': 'running'}

    def badge(route):
        reads.append(route.request.url)
        route.fulfill(status=503 if response.get('fail') else 200, json=response)

    page.route('**/api/agent/workspace/native-badge', badge)
    state['runtime']['kids_mode'] = {'active': True, 'locked': True}
    page.evaluate('refreshStatus()')
    state['runtime']['kids_mode'] = {}
    page.evaluate('refreshStatus()')
    page.wait_for_function("document.getElementById('native-work-badge').textContent === 'Project work running'")
    assert len(reads) == 1
    page.evaluate('window.fieldNow = Date.now; window.fieldOffset = 0; '
                  'Date.now = () => window.fieldNow() + window.fieldOffset')
    response.clear()
    response['fail'] = True
    page.evaluate('window.fieldOffset += 60000; refreshStatus()')
    page.wait_for_function("document.getElementById('native-work-badge').textContent.includes('unconfirmed')")
    assert len(reads) == 2
    response.clear()
    response.update(running=False, status='idle')
    page.evaluate('window.fieldOffset += 60000; refreshStatus()')
    page.wait_for_function("document.getElementById('native-work-badge').hidden")
    assert len(reads) == 3
    page.evaluate('window.fieldOffset += 60000; refreshStatus()')
    assert len(reads) == 3
    assert not errors
