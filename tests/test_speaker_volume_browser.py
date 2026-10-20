"""Real Chromium controls; network/media/hardware are explicit fixtures, not physical acceptance."""

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from settings_ui_helpers import choose


@pytest.fixture
def browser_page():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        state = {"volume": 62, "muted": False, "can_unmute": True}
        calls = []
        behavior = {"failure": False, "malformed": False, "held": None, "hold_get": False}

        def respond(route):
            path = urlparse(route.request.url).path
            if path.startswith("/static/"):
                route.fulfill(path=str(Path("homebody") / path.lstrip("/")))
            elif path == "/":
                route.fulfill(path="homebody/static/index.html", content_type="text/html")
            elif path == "/api/owner/session":
                route.fulfill(json={"owner": True, "name": "Fixture", "csrf": "fixture"})
            elif path == "/api/status":
                route.fulfill(
                    json={
                        "config": {},
                        "runtime": {"state": "waiting_for_wake_word", "power_mode": "standby", "kids_mode": {}},
                    }
                )
            elif path == "/api/audio/volume":
                if route.request.method == "GET":
                    if behavior["hold_get"]:
                        behavior["held"] = route
                    else:
                        route.fulfill(json=state)
                else:
                    args = route.request.post_data_json
                    calls.append(args)
                    assert route.request.headers["x-homebody-csrf"] == "fixture"
                    if behavior["failure"]:
                        route.fulfill(status=409, json={"detail": "Mixer change not confirmed"})
                    elif behavior["malformed"]:
                        route.fulfill(json={"ok": True})
                    else:
                        value = args.get("volume", 0 if args.get("muted") else 30)
                        state.update(volume=value, muted=value == 0, can_unmute=True)
                        route.fulfill(json=state)
            elif path.startswith("/api/updates"):
                route.fulfill(
                    json={"current": {"version": "0.4.0"}, "checked_at": 1, "history": [], "supported": True}
                )
            else:
                route.fulfill(json={})

        page.route("**/*", respond)
        page.goto("http://homebody.test")
        page.evaluate("document.getElementById('tab-settings').click()")  # behind More on phones
        choose(page,"voice")
        yield page, state, calls, behavior
        browser.close()


def wait_ready(page):
    page.wait_for_function("document.getElementById('speaker-confirmed')?.textContent.includes('62%')")


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_volume_help_has_clear_spacing_and_secondary_actions_share_row(browser_page, width, theme, tmp_path):
    page, _, calls, _ = browser_page
    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(color_scheme=theme)
    wait_ready(page)
    actions = page.locator("#speaker-actions").bounding_box()
    helper = page.locator("#speaker-help").bounding_box()
    mute = page.locator("#speaker-mute").bounding_box()
    refresh = page.locator("#speaker-refresh").bounding_box()
    assert helper["y"] - (actions["y"] + actions["height"]) >= 16
    assert abs(mute["y"] - refresh["y"]) < 1
    assert mute["height"] >= 48 and refresh["height"] >= 48
    assert not page.locator("#speaker-details").get_attribute("open")
    assert "Microphone settings are unchanged" in page.locator("#speaker-help").inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert calls == []  # spacing inspection must never change speaker volume
    shots = Path(os.getenv("HOMEBODY_TEST_SCREENSHOTS", str(tmp_path)))
    shots.mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollBy(0, document.getElementById('speaker-card').getBoundingClientRect().top - 100)")
    page.locator("#speaker-card").screenshot(path=str(shots / f"speaker-spacing-{theme}-{width}.png"))


@pytest.mark.parametrize("width", [390, 1440])
def test_volume_slider_explicit_apply_and_mute_in_phone_and_desktop(browser_page, width):
    page, state, calls, _ = browser_page
    page.set_viewport_size({"width": width, "height": 844})
    assert page.locator("#speaker-volume").count() == 1
    wait_ready(page)
    slider = page.locator("#speaker-volume")
    slider.fill("30")
    assert calls == []
    assert "62%" in page.locator("#speaker-confirmed").inner_text()
    assert "30%" in page.locator("#speaker-draft").inner_text()
    page.locator("#speaker-apply").click()
    page.wait_for_function("document.getElementById('speaker-confirmed').textContent.includes('30%')")
    assert calls == [{"volume": 30}]
    page.locator("#speaker-mute").click()
    page.wait_for_function("document.getElementById('speaker-confirmed').textContent.includes('Muted')")
    page.locator("#speaker-mute").click()
    page.wait_for_function("document.getElementById('speaker-confirmed').textContent.includes('30%')")
    assert calls == [{"volume": 30}, {"muted": True}, {"muted": False}]
    assert page.locator("#speaker-card").bounding_box()["width"] <= width
    page.locator("#speaker-card").screenshot(
        path=str(Path(__file__).resolve().parents[1] / ".artifacts" / f"homebody-volume-fixture-{width}.png")
    )


@pytest.mark.parametrize("mode", ["failure", "malformed"])
def test_failed_or_false_success_does_not_claim_changed_volume(browser_page, mode):
    page, _, calls, behavior = browser_page
    wait_ready(page)
    behavior[mode] = True
    page.locator("#speaker-volume").fill("20")
    page.locator("#speaker-apply").click()
    page.wait_for_function("document.getElementById('speaker-message').textContent.includes('not confirmed')")
    assert "62%" in page.locator("#speaker-confirmed").inner_text()
    assert calls == [{"volume": 20}]
    assert "Speaker set" not in page.locator("#speaker-message").inner_text()
    assert page.locator("#speaker-apply").is_disabled()
    assert page.locator("#speaker-mute").is_disabled()
    page.locator("#speaker-refresh").click()
    page.wait_for_function("!document.getElementById('speaker-mute').disabled")
    assert page.locator("#speaker-confirmed").inner_text() == "Speaker · 62%"


def test_polling_does_not_overwrite_an_unapplied_slider_edit(browser_page):
    page, state, calls, _ = browser_page
    wait_ready(page)
    page.locator("#speaker-volume").fill("40")
    state.update(volume=30)
    page.evaluate("HomebodyVolume.refresh()")
    assert page.locator("#speaker-volume").input_value() == "40"
    assert "30%" in page.locator("#speaker-confirmed").inner_text()
    assert calls == []


def test_late_poll_cannot_reenable_after_kids_or_owner_loss(browser_page):
    page, _, calls, behavior = browser_page
    wait_ready(page)
    behavior["hold_get"] = True
    page.evaluate("void HomebodyVolume.refresh()")
    page.wait_for_function("document.getElementById('speaker-refresh').disabled")
    page.evaluate("HomebodyVolume.update({state:'waiting_for_wake_word',kids_mode:{locked:true}})")
    if behavior["held"]:
        try:
            behavior["held"].fulfill(json={"volume": 80, "muted": False, "can_unmute": True})
        except Exception:
            pass  # aborted HTTP request
    assert page.locator("#speaker-volume").is_disabled()
    assert page.locator("#speaker-apply").is_disabled()
    assert "80%" not in page.locator("#speaker-confirmed").inner_text()
    page.evaluate("HomebodyVolume.owner(false)")
    assert page.locator("#speaker-mute").is_disabled()
    assert calls == []
