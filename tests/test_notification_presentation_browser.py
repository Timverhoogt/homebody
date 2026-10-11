"""Presentation and fallback regression checks against the real dashboard DOM."""

import pytest
from test_notifications_browser import browser_page as browser_page


@pytest.mark.parametrize("width", [390, 844, 1440])
def test_feedback_card_replaces_inline_text(browser_page, width):
    page, errors = browser_page
    page.set_viewport_size({"width": width, "height": 900})
    page.locator("#tab-settings").click()
    page.evaluate(
        "const m=document.getElementById('form-message'); "
        "m.textContent='Settings saved'; notifyFeedback(m,'ok')"
    )
    assert page.locator("#form-message").is_hidden()
    assert page.locator(".notification-title").inner_text() == "Settings"
    assert page.locator(".notification-text > span").inner_text() == "Settings saved"
    assert page.locator("#notification-status").inner_text() == "Settings: Settings saved"
    assert page.locator(".notification-ok").evaluate("el => getComputedStyle(el,'::before').content") == '\"✓\"'
    page.clock.fast_forward(6000)
    assert page.locator(".notification").count() == 0
    assert page.locator("#form-message").is_hidden(), "success must not leave stale green text"
    page.evaluate(
        "HomebodyNotifications.setSuppressed('test',true); "
        "notifyFeedback(document.getElementById('form-message'),'error')"
    )
    assert page.locator("#form-message").is_visible(), "unavailable notification retains inline fallback"
    assert not errors


def test_titles_are_text_only_and_updates_announced(browser_page):
    page, errors = browser_page
    page.evaluate("HomebodyNotifications.show('Saved', {id:'safe',kind:'ok',title:'<img src=x onerror=alert(1)>'})")
    assert page.locator(".notification img").count() == 0
    page.evaluate("HomebodyNotifications.show('Saved', {id:'safe',kind:'ok',title:'New title'})")
    assert page.locator("#notification-status").inner_text() == "New title: Saved"
    assert not errors


def test_sleep_feedback_and_false_success(browser_page):
    page, errors = browser_page
    page.route("**/api/power", lambda route: route.fulfill(json={"runtime":{"power_mode":"sleep"}}))
    page.evaluate("setPowerMode('sleep')")
    page.wait_for_function("document.querySelector('.notification-ok') !== null")
    assert page.locator("#power-message").is_hidden()
    assert "Sleep mode enabled" in page.locator(".notification-ok").inner_text()
    page.route("**/api/power", lambda route: route.fulfill(json={"runtime":{"power_mode":"standby"}}))
    page.evaluate("setPowerMode('sleep')")
    page.wait_for_function("document.querySelector('.notification-error') !== null")
    assert "not confirmed" in page.locator(".notification-error").inner_text()
    page.clock.resume()
    page.evaluate("() => new Promise(requestAnimationFrame)")
    page.screenshot(path=".pytest_cache/notification-browser/redesigned-phone.png", animations="disabled")
    assert not errors
