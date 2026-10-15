"""Real Chromium Bluetooth controls; BlueZ and devices are synthetic route fixtures, not physical acceptance."""

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

TARGET = "A4:AE:12:A6:9F:BE"
PHONE = "1A:31:F9:1F:20:ED"


def device(address, name, **flags):
    return {"address": address, "name": name, "paired": False, "bonded": False, "connected": False, **flags}


@pytest.fixture
def browser_page():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        state = {
            "adapter_available": True,
            "adapter_powered": True,
            "scan_active": False,
            "devices": [device(PHONE, "Android-5453"), device(TARGET, "Wireless Controller")],
            "gamepad_enabled": False,
            "gamepad_connected": False,
            "last_error": "",
            "operation": {"name": "", "address": "", "phase": "", "error": "", "input_path": ""},
        }
        calls = []
        behavior = {"pair": "reject"}

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
                        "host": {"bluetooth_controller_supported": True, "label": "Fixture"},
                        "runtime": {"state": "waiting_for_wake_word", "power_mode": "standby", "kids_mode": {}},
                    }
                )
            elif path == "/api/bluetooth/status":
                route.fulfill(json={"ok": True, **state})
            elif path == "/api/bluetooth/pair":
                args = route.request.post_data_json
                calls.append(("pair", args))
                assert route.request.headers["x-homebody-csrf"] == "fixture"
                error = f"BlueZ did not confirm connection for {args['address']}"
                state["operation"] = {
                    "name": "pair", "address": args["address"], "phase": "failed", "error": error, "input_path": "",
                }
                if behavior["pair"] == "reject":
                    route.fulfill(status=502, json={"detail": error})
                else:  # HTTP 200 whose body still carries an operation failure.
                    route.fulfill(json={"ok": True, **state})
            elif path.startswith("/api/updates"):
                route.fulfill(
                    json={"current": {"version": "0.5.16"}, "checked_at": 1, "history": [], "supported": True}
                )
            else:
                route.fulfill(json={})

        page.route("**/*", respond)
        page.goto("http://homebody.test")
        page.set_default_timeout(8000)
        page.locator("#tab-robot").click()
        page.wait_for_function("document.querySelectorAll('#bluetooth-device-select option').length === 3")
        page.wait_for_function("!document.querySelector('.bluetooth-card').hidden")
        page.locator(".bluetooth-card > summary").click()
        yield page, state, calls, behavior
        browser.close()


def poll(page):
    page.evaluate("refreshBluetooth()")
    page.wait_for_function("!bluetoothRefreshPending")


@pytest.mark.parametrize("width", [390, 1440])
def test_selection_is_explicit_and_bound_to_mac_through_polls(browser_page, width, tmp_path):
    page, state, calls, _ = browser_page
    page.set_viewport_size({"width": width, "height": 900})
    select = page.locator("#bluetooth-device-select")
    # No implicit first-device (phone) selection.
    assert select.input_value() == ""
    assert page.locator("#bluetooth-pair-button").is_disabled()

    select.select_option(TARGET)
    assert not page.locator("#bluetooth-pair-button").is_disabled()
    for _ in range(3):
        poll(page)
        assert select.input_value() == TARGET
    # Reordered discovery with a new device sorting first keeps the chosen MAC.
    state["devices"] = [device("00:11:22:33:44:55", "AAA Speaker"), *state["devices"]]
    poll(page)
    assert select.input_value() == TARGET

    # When the chosen device vanishes, nothing else is silently selected.
    state["devices"] = [device(PHONE, "Android-5453")]
    poll(page)
    assert select.input_value() == ""
    assert page.locator("#bluetooth-pair-button").is_disabled()
    state["devices"] = [device(PHONE, "Android-5453"), device(TARGET, "Wireless Controller")]
    poll(page)
    assert select.input_value() == ""
    assert calls == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    shots = Path(os.getenv("HOMEBODY_TEST_SCREENSHOTS", str(tmp_path)))
    shots.mkdir(parents=True, exist_ok=True)
    page.locator(".bluetooth-card").screenshot(path=str(shots / f"bluetooth-selection-{width}.png"))


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("mode", ["reject", "false_success"])
def test_pair_failure_is_retained_and_not_overwritten_by_polls(browser_page, width, mode, tmp_path):
    page, state, calls, behavior = browser_page
    behavior["pair"] = mode
    page.set_viewport_size({"width": width, "height": 900})
    page.locator("#bluetooth-device-select").select_option(TARGET)
    page.locator("#bluetooth-pair-button").click()
    page.wait_for_function(
        "document.getElementById('bluetooth-message').textContent.includes('did not confirm connection')"
    )
    assert calls == [("pair", {"address": TARGET})]
    message = page.locator("#bluetooth-message")
    assert "error" in (message.get_attribute("class") or "")
    for _ in range(2):
        poll(page)
        assert "did not confirm connection" in message.inner_text()
        assert "updated" not in message.inner_text()
        assert "Paired, bonded" not in message.inner_text()
    assert state["last_error"] == ""  # status poll itself succeeded
    assert page.locator("#gamepad-enabled").is_checked() is False
    shots = Path(os.getenv("HOMEBODY_TEST_SCREENSHOTS", str(tmp_path)))
    shots.mkdir(parents=True, exist_ok=True)
    page.locator(".bluetooth-card").screenshot(path=str(shots / f"bluetooth-pair-error-{mode}-{width}.png"))
