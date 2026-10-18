"""Chromium UI with intercepted requests only; no listening or physical controls."""

# ruff: noqa: F811
from test_assistant_modes_browser import ui  # noqa: F401
from test_household_setup_browser import serve

from homebody.config import AppConfig


def test_idle_connection_status_and_categorical_failure(ui):
    page, state, posts, errors = ui
    state["runtime"]["power_mode"] = "standby"
    serve(page, AppConfig())
    page.locator("#household-connection > summary").click()
    response = {
        "connected": True,
        "tools_active": False,
        "homebody_context": None,
        "url": "https://household.invalid",
        "enabled": True,
    }
    calls = []

    def connection(route):
        calls.append(route.request.url.rsplit("/", 1)[-1])
        route.fulfill(json=response)

    page.route("**/api/household/tools/*", connection)
    page.evaluate("HomebodyHousehold.reload()")
    page.wait_for_function(
        "document.getElementById('household-connection-message').textContent.includes('tools inactive')"
    )
    page.locator("#household-connection-connect").click()
    page.wait_for_function(
        "document.getElementById('household-connection-message').textContent.includes('reconnect explicitly')"
    )
    assert "connect" in calls
    assert not posts
    response["tools_active"] = True
    page.evaluate("HomebodyHousehold.reload()")
    page.wait_for_function(
        "document.getElementById('household-connection-message').textContent.includes('tools active')"
    )
    response["connected"] = False
    response["tools_active"] = False
    page.evaluate("HomebodyHousehold.reload()")
    page.wait_for_function(
        "document.getElementById('household-connection-message').textContent.includes('disconnected')"
    )
    page.route(
        "**/api/household/tools/connect",
        lambda route: route.fulfill(
            status=502, json={"detail": "Household protocol incompatible or attachment refused"}
        ),
    )
    page.locator("#household-connection-connect").click()
    page.wait_for_function(
        "document.getElementById('household-connection-message').textContent.includes('protocol incompatible')"
    )
    assert not posts and not errors
