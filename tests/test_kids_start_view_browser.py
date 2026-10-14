# ruff: noqa: F811
"""Start viewport behavior with API fixtures only; never activates devices."""
import pytest
from test_assistant_modes_browser import ui  # noqa: F401
from test_kids_game_master_browser import hosted


@pytest.mark.parametrize('kind',['host','ispy'])
@pytest.mark.parametrize('reduced',[False,True])
def test_confirmed_start_reveals_animation_once_on_phone(ui,kind,reduced):
    page,state,posts,errors=ui
    page.set_viewport_size({'width':390,'height':844})
    page.emulate_media(reduced_motion='reduce' if reduced else 'no-preference')
    page.click('#tab-kids')
    if kind=='ispy':
        page.click('[data-kids-activity="ispy"]')
        page.check('#kids-ispy-camera-consent')
    runtime=hosted('lobby') if kind=='host' else hosted('playing',activity='ispy')
    if kind=='ispy':
        runtime['kids_mode']['host']={'enabled':False}
        runtime['kids_mode']['profile']['host']=False
    def start(route):
        state['runtime']=runtime
        route.fulfill(json={'ok':True,'kids_mode':runtime['kids_mode']})
    page.route('**/api/kids/start',start)
    button='#kids-host-start-button' if kind=='host' else '#kids-start-button'
    page.locator(button).scroll_into_view_if_needed()
    assert page.evaluate('window.scrollY')>100
    page.click(button)
    page.wait_for_function(
        "!kidsRequestPending && document.getElementById('panel-kids').dataset.kidsSession==='active'")
    page.wait_for_function('window.scrollY<=1')
    assert page.locator('#kids-stage').evaluate(
        'e=>{const r=e.getBoundingClientRect();return r.top<innerHeight && r.bottom>0}')
    page.evaluate('window.scrollTo(0,350)')
    page.evaluate('refreshStatus()')
    page.wait_for_timeout(250)
    assert page.evaluate('window.scrollY')>100, 'ordinary active polls must not drag the user back to the top'
    assert not errors


def test_failed_start_does_not_drag_phone_to_animation(ui):
    page,state,posts,errors=ui
    page.emulate_media(reduced_motion='reduce')
    page.click('#tab-kids')
    page.route('**/api/kids/start',lambda r:r.fulfill(status=502,json={'detail':'fixture start failed'}))
    page.locator('#kids-host-start-button').scroll_into_view_if_needed()
    page.click('#kids-host-start-button')
    page.wait_for_function('!kidsRequestPending')
    assert page.evaluate('window.scrollY')>100
    assert 'failed' in page.locator('#kids-message').inner_text()
    assert not errors


def test_offline_disables_shared_start_too(ui):
    page,state,posts,errors=ui
    page.click('#tab-kids')
    page.route('**/api/status',lambda r:r.fulfill(status=503,json={}))
    page.evaluate('refreshStatus()')
    page.wait_for_function("document.getElementById('kids-status-badge').textContent==='Offline'")
    assert page.locator('#kids-host-start-button').is_disabled()
    assert not errors
