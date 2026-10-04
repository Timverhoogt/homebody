"""Actual browser update confirmation/reconnect/text-only behavior; all network is intercepted."""
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest


def test_updates_in_chromium():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        catalog = {"current": {"version": "0.4.0"}, "supported": True, "checked_at": time.time(),
                   "available": {"version": "0.5.0", "release_id": 42}, "history": [], "job": None,
                   "notes": [{"version": "0.4.1", "notes": '<img src=x onerror="alert(1)">'},
                             {"version": "0.5.0", "notes": "New updates"}]}
        installed = []
        disconnected = False
        def respond(route):
            path = urlparse(route.request.url).path
            if path.startswith("/static/"):
                route.fulfill(path=str(Path("homebody") / path.lstrip("/")))
            elif path == "/":
                route.fulfill(path="homebody/static/index.html", content_type="text/html")
            elif path == "/api/owner/session":
                route.fulfill(json={"owner": True, "name": "Fixture", "csrf": "fixture"})
            elif path == "/api/status":
                route.fulfill(json={"config": {}, "runtime": {"power_mode": "standby"}})
            elif path == "/api/updates/install":
                installed.append(route.request.post_data_json)
                route.fulfill(json={"id": "a" * 32, "state": "queued"})
            elif path.startswith("/api/updates"):
                if disconnected:
                    route.fulfill(status=502, body="Restarting")
                else:
                    route.fulfill(json=catalog)
            else:
                route.fulfill(json={})
        page.route("**/*", respond)
        page.goto("http://homebody.test")
        page.locator("#update-open").click()
        assert page.locator("#update-dialog").is_visible()
        assert page.locator("#update-notes img").count() == 0
        assert "0.4.1" in page.locator("#update-notes").inner_text()
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator("#update-install").click()
        assert installed == []
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("#update-install").click()
        page.wait_for_function("!document.getElementById('update-dialog').open")
        assert installed == [{"confirm": "INSTALL", "release_id": 42}]
        disconnected = True
        page.evaluate("HomebodyUpdates.refresh()")
        assert page.locator("#update-install").is_disabled()
        disconnected = False
        catalog["history"] = [{"id": "a" * 32, "state": "succeeded", "acknowledged": False,
                               "from_version": "0.4.0", "manifest": {"version": "0.5.0"},
                               "notes": catalog["notes"]}]
        catalog["current"] = {"version": "0.5.0"}
        page.evaluate("HomebodyUpdates.refresh()")
        assert page.locator("#update-dialog").is_visible()
        assert "Updated and verified" in page.locator("#update-notes").inner_text()
        page.locator("#update-ack").click()
        page.wait_for_function("!document.getElementById('update-dialog').open")
        page.evaluate("HomebodyUpdates.refresh()")
        assert not page.locator("#update-dialog").is_visible()
        browser.close()
