"""Actual navigation: Everyday is not a household administration screen."""
# ruff: noqa: F811
from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401
from test_household_setup_browser import serve

from homebody.config import AppConfig


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_everyday_keeps_settings_out_of_the_conversation(ui, theme):
    page, _, _, errors = ui
    page.set_viewport_size({"width": 390, "height": 844})
    page.emulate_media(color_scheme=theme)
    state = serve(page, AppConfig())
    page.click("#tab-dashboard")
    for selector in ("#household-card", "#everyday-permissions", "#everyday-technical-details"):
        assert page.locator(selector).is_hidden()
        assert page.locator(selector).evaluate("e => !!e.closest('#panel-settings')")
    assert "keys and coordinates" not in page.locator("#panel-dashboard").inner_text()
    folder = Path('.artifacts/screenshots/everyday-settings')
    folder.mkdir(parents=True, exist_ok=True)
    for position in ('top', 'bottom'):
        page.evaluate('scrollTo(0, 0)' if position == 'top' else 'scrollTo(0, document.body.scrollHeight)')
        page.screenshot(path=str(folder / f'everyday-{theme}-{position}.png'))
    page.click('[data-everyday-setting=household-heading]')
    assert page.locator('#panel-settings').is_visible()
    assert page.locator('#household-card').is_visible()
    assert state['posts'] == []  # navigation never approves, revokes or captures
    page.locator('#household-heading').scroll_into_view_if_needed()
    page.screenshot(path=str(folder / f'settings-{theme}.png'))
    assert not errors
