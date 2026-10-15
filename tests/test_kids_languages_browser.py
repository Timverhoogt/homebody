# ruff: noqa: F811
"""Spanish/German/French/Italian/Portuguese Kids selection with API fixtures only; never activates devices."""
import json

import pytest
from test_assistant_modes_browser import ui  # noqa: F401
from test_kids_game_master_browser import hosted, show
from test_onboarding_browser import setup_ui  # noqa: F401

KIDS_OPTIONS = [['en', 'English'], ['nl', 'Nederlands'], ['es', 'Español'], ['de', 'Deutsch'],
                ['fr', 'Français'], ['it', 'Italiano'], ['pt', 'Português']]
NAMES = {'es': 'Spanish', 'de': 'German', 'fr': 'French', 'it': 'Italian', 'pt': 'Portuguese'}


@pytest.mark.parametrize('language', ['es', 'de', 'fr', 'it', 'pt'])
def test_new_language_selection_is_sent_saved_and_locked(ui, language):
    page, state, posts, errors = ui
    page.click('#tab-kids')
    labels = page.eval_on_selector_all('#kids-language option', 'o=>o.map(x=>[x.value,x.textContent])')
    assert labels == KIDS_OPTIONS
    page.select_option('#kids-language', language)
    sent = []
    runtime = hosted('lobby')
    runtime['kids_mode']['profile']['language'] = language

    def start(route):
        sent.append(json.loads(route.request.post_data or '{}'))
        state['runtime'] = runtime
        route.fulfill(json={'ok': True, 'kids_mode': runtime['kids_mode']})

    page.route('**/api/kids/start', start)
    page.click('#kids-start-button')  # the single-activity Start is the path that saves the profile
    page.wait_for_function(
        "!kidsRequestPending && document.getElementById('panel-kids').dataset.kidsSession==='active'")
    assert [body['language'] for body in sent] == [language]
    assert json.loads(page.evaluate("localStorage.getItem('homebody-kids-profile')"))['language'] == language
    assert page.locator('#kids-language').is_disabled()  # immutable while the session is active
    page.evaluate("refreshStatus()")
    page.wait_for_timeout(200)
    assert page.locator('#kids-language').is_disabled()
    assert page.locator('#kids-language').input_value() == language
    assert [body['language'] for body in sent] == [language]
    assert not errors


@pytest.mark.parametrize('language', ['es', 'de', 'fr', 'it', 'pt'])
def test_picker_availability_is_not_readiness_unconfigured_voice_refusal_starts_nothing(ui, language):
    page, state, posts, errors = ui
    page.click('#tab-kids')
    page.select_option('#kids-language', language)
    detail = f'Kids Mode {NAMES[language]} voice is not configured on the Hermes host'
    page.route('**/api/kids/start', lambda r: r.fulfill(status=409, json={'detail': detail}))
    page.click('#kids-host-start-button')
    page.wait_for_function('!kidsRequestPending')
    assert f'{NAMES[language]} voice is not configured' in page.locator('#kids-message').inner_text()
    assert page.locator('#panel-kids').get_attribute('data-kids-session') != 'active'
    assert not page.locator('#kids-language').is_disabled()
    assert not errors


def test_saved_profiles_restore_supported_languages_only(ui):
    page, state, posts, errors = ui
    for language in ('de', 'fr', 'it', 'pt', 'es'):
        page.evaluate(f"localStorage.setItem('homebody-kids-profile', JSON.stringify({{language:'{language}'}}))")
        page.reload()
        page.click('#tab-kids')
        assert page.locator('#kids-language').input_value() == language
    page.evaluate("localStorage.setItem('homebody-kids-profile', JSON.stringify({language:'sv'}))")
    page.reload()
    page.click('#tab-kids')
    assert page.locator('#kids-language').input_value() == 'en'
    options = page.eval_on_selector_all('#setup-language option', 'o=>o.map(x=>[x.value,x.textContent])')
    assert options == [['en', 'English'], ['nl', 'Dutch'], ['es', 'Español'], ['de', 'Deutsch'],
                       ['fr', 'Français'], ['it', 'Italiano'], ['pt', 'Português']]
    assert not errors


@pytest.mark.parametrize('language,expected',
                         [('es', 'es'), ('de', 'de'), ('fr', 'fr'), ('it', 'it'), ('pt', 'pt'), ('sv', 'en')])
def test_onboarding_restores_supported_conversation_language_only(setup_ui, language, expected):
    page, state, posts, errors = setup_ui
    state['language'] = language
    page.reload()
    page.wait_for_function("!document.querySelector('main').hidden")
    page.wait_for_function(f"document.getElementById('setup-language').value === '{expected}'")
    assert not errors


def test_french_session_real_401_owner_loss_revokes_host_controls(ui):
    page, state, posts, errors = ui
    page.click('#tab-kids')
    runtime = hosted('lobby')
    runtime['kids_mode']['profile']['language'] = 'fr'
    show(page, state, runtime)
    assert page.locator('#kids-host-controls').is_visible()
    # Revoke the mocked owner on the server side (HTTP 401), not just a local view flag.
    page.route('**/api/status', lambda route: route.fulfill(
        status=401, json={'detail': 'Pair this device as Owner first'}))
    page.evaluate('refreshStatus()')
    page.wait_for_function("document.getElementById('kids-host-controls').hidden")
    assert page.evaluate("[...document.querySelectorAll('[data-kids-host-activity]')].every(b => b.disabled)")
    assert page.evaluate("document.querySelector('#kids-stage .hb-scene').dataset.phase") == 'offline'
    assert not [path for path, _ in posts if path in {'/api/kids/host/select', '/api/kids/start'}]
    assert not errors
