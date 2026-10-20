# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""Visible-route walkthrough. Fixture screenshots are not hardware acceptance."""
import json
from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401

OUT = Path('.artifacts/screenshots/clutter-review')


@pytest.mark.parametrize('width,theme', [(390, 'light'), (390, 'dark'), (1440, 'light')])
def test_remaining_clutter_walkthrough(ui, width, theme):
    page, state, posts, errors = ui
    page.set_viewport_size({'width': width, 'height': 844})
    page.emulate_media(color_scheme=theme)
    page.route('**/api/updates', lambda route: route.fulfill(json={
        'current': {'version': '0.5.45'}, 'supported': True, 'checked_at': 1,
        'available': None, 'history': [], 'job': None, 'notes': [],
    }))
    page.evaluate('HomebodyUpdates.refresh()')
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []

    def shot(screen, position):
        page.evaluate('() => new Promise(requestAnimationFrame)')
        name = f'{screen}-{width}-{theme}-{position}.png'
        page.screenshot(path=str(OUT / name), animations='disabled')
        manifest.append({'screen': screen, 'width': width, 'theme': theme,
                         'state': position, 'file': name, 'inspected': False})

    for tab in ['dashboard', 'announce', 'kids', 'robot', 'settings', 'agent']:
        if tab == 'announce':
            page.click('#everyday-announce-link')
        else:
            page.click(f'#tab-{tab}')
        page.evaluate('window.scrollTo(0,0)')
        assert page.locator(f'#panel-{tab}').is_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        for position, fraction in [('top', 0), ('middle', .5), ('bottom', 1)]:
            page.evaluate('(f) => window.scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*f)', fraction)
            shot(tab, position)
        if tab == 'dashboard':
            for selector in ['#companion-details', '#companion-advanced',
                             '.presence-controls', '.companion-suggestions']:
                assert page.locator(selector).is_hidden()
                assert page.locator('#panel-settings').locator(selector).count() == 1
        if tab == 'kids':
            copy = page.locator('#panel-kids').text_content()
            assert 'Faces, screens, documents and writing are refused' not in copy
            assert 'Camera, personal' not in page.locator('#kids-session-detail').inner_text()
            page.locator('#kids-guide > summary').click()
            page.locator('#kids-guide').scroll_into_view_if_needed()
            shot('kids', 'parent-help')
        if tab == 'robot':
            assert page.locator('#motor-wake-button').is_hidden()
            assert page.locator('#robot-stop-button').is_visible()
            viewer = page.locator('#camera-viewer')
            assert viewer.bounding_box()['height'] <= 120
            viewer.scroll_into_view_if_needed()
            shot('robot', 'camera-off')
            page.locator('.camera-diagnostics > summary').click()
            page.locator('#camera-copy-report').scroll_into_view_if_needed()
            assert page.locator('#camera-copy-report').is_visible()
            shot('robot', 'camera-details')
        if tab == 'settings':
            page.locator('#companion-details > summary').click()
            page.locator('#companion-details').scroll_into_view_if_needed()
            shot('settings', 'sources')
            page.locator('#companion-advanced > summary').click()
            page.locator('#presentation-start').scroll_into_view_if_needed()
            shot('settings', 'advanced')
    (OUT / f'manifest-{width}-{theme}.json').write_text(json.dumps(manifest, indent=2))
    assert not errors
