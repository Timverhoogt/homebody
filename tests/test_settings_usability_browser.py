"""Visible settings navigation; no robot, media or permission changes."""
# ruff: noqa: F811
from pathlib import Path

import pytest
from settings_ui_helpers import choose
from test_assistant_modes_browser import ui  # noqa: F401


@pytest.mark.parametrize('width,theme', [(320,'light'), (390,'light'), (1440,'dark')])
def test_task_grouped_settings_and_nested_links(ui, width, theme):
    page, _, posts, errors = ui
    page.set_viewport_size({'width': width, 'height': 844})
    page.emulate_media(color_scheme=theme)
    page.click('#tab-settings')
    assert page.locator('#settings-everyday').is_visible()
    assert page.locator('#presence-enabled').is_visible()
    assert page.locator('#everyday-permissions').is_hidden()
    assert page.locator('#companion-presence').is_hidden()
    assert page.locator('#everyday-model').is_hidden()
    assert page.locator('#settings-form').is_hidden()
    assert page.locator('#speaker-volume').is_hidden()
    assert page.locator('#shutdown-button').is_hidden()
    assert page.locator('[data-settings-nav]').count() == 5
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    ids = page.evaluate("[...document.querySelectorAll('[id]')].map(e => e.id)")
    assert len(ids) == len(set(ids))
    shots = Path('.artifacts/screenshots/settings-usability')
    shots.mkdir(parents=True, exist_ok=True)
    page.evaluate('scrollTo(0,0)')
    page.screenshot(path=str(shots / f'settings-{width}-{theme}.png'))

    choose(page,'voice')
    assert page.locator('#speaker-volume').is_visible()
    assert page.locator('#settings-form').is_visible()
    choose(page,'privacy')
    page.locator('#everyday-permissions > summary').click()
    # A real permissions link must open all containing disclosures, not only the nearest.
    page.locator('[data-everyday-setting="camera_enabled"]').click()
    assert page.locator('#camera_enabled').is_visible()
    assert page.evaluate('document.activeElement.id') == 'camera_enabled'
    assert not posts
    assert not errors


def test_settings_layout_is_cached_and_geared():
    html = Path('homebody/static/index.html').read_text()
    sw = Path('homebody/static/service-worker.js').read_text()
    assert '/static/settings-layout.js?v=121' in html
    assert '/static/settings-layout.js?v=121' in sw
    assert 'homebody-shell-v121' in sw
    gear = html.split('<symbol id="i-gear"', 1)[1].split('</symbol>', 1)[0]
    assert 'r="3"' in gear and 'M12 3v2.5' not in gear
