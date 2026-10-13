"""Update dialog scroll ownership, skipped-version offers and completion selection in real Chromium.

Every request is intercepted. Versions, release ids and notes below are synthetic TEST DATA only.
"""
import copy
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

SHOTS = Path(".artifacts/update-dialog")
RELEASED = ("!document.getElementById('update-dialog').open"
            " && !document.documentElement.classList.contains('update-dialog-scroll-lock')")
PAGE_STATE = ("[scrollX, scrollY, document.documentElement.getAttribute('style'),"
              " document.body.getAttribute('style')]")
FORCE_INSTALL = ("document.getElementById('update-install').disabled = false;"
                 " document.getElementById('update-install').click()")
LONG = "\n".join(f"Synthetic test note line {i}: fixture text only, not a real release." for i in range(120))


def offer(version, release_id):
    return {"version": version, "release_id": release_id, "tag": f"v{version}", "commit": f"{release_id:040d}"}


def success(job_id, old, new, notes=(), acknowledged=False, state="succeeded"):
    return {"id": job_id * 32, "state": state, "acknowledged": acknowledged, "from_version": old,
            "manifest": {"version": new}, "notes": list(notes),
            "error": "" if state == "succeeded" else "test failure"}


class Fixture:
    def __init__(self, page, catalog):
        self.page, self.catalog, self.posts = page, catalog, []
        self.read_status = 200
        self.post_status = {}
        page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.errors = []
        page.route("**/*", self.respond)

    def respond(self, route):
        url = urlparse(route.request.url)
        path = url.path
        if url.hostname != "homebody.test":
            route.abort()
        elif path.startswith("/static/"):
            route.fulfill(path=str(Path("homebody") / path.lstrip("/")))
        elif path == "/":
            route.fulfill(path="homebody/static/index.html", content_type="text/html")
        elif path == "/api/owner/session":
            route.fulfill(json={"owner": True, "name": "Fixture", "csrf": "fixture"})
        elif path == "/api/status":
            route.fulfill(json={"config": {}, "runtime": {"power_mode": "standby"}})
        elif path.startswith("/api/updates") and route.request.method == "POST":
            self.posts.append((path, route.request.post_data_json))
            status = self.post_status.get(path, 200)
            if status != 200:
                route.fulfill(status=status, json={"detail": f"test {status}"})
            elif path == "/api/updates/install":
                route.fulfill(json={"id": "f" * 32, "state": "queued"})
            else:
                if path == "/api/updates/acknowledge":
                    for entry in self.catalog["history"]:
                        if entry["id"] == route.request.post_data_json["job_id"]:
                            entry["acknowledged"] = True
                route.fulfill(json=self.catalog)
        elif path == "/api/updates":
            if self.read_status != 200:
                route.fulfill(status=self.read_status, body="test unavailable")
            else:
                route.fulfill(json=self.catalog)
        else:
            route.fulfill(json={})

    def refresh(self):
        self.page.evaluate("HomebodyUpdates.refresh()")


def catalog(current="0.5.8", available=None, notes=(), history=(), job=None):
    return {"current": {"version": current}, "supported": True, "checked_at": time.time(),
            "available": available, "notes": list(notes), "history": list(history), "job": job}


def boot(browser, catalog_body, **context):
    ctx = browser.new_context(**context)
    page = ctx.new_page()
    fixture = Fixture(page, catalog_body)
    page.goto("http://homebody.test")
    page.wait_for_function("window.HomebodyUpdates"
                           " && document.getElementById('update-current').textContent.includes('Homebody')")
    return ctx, page, fixture


def dialog_open(page):
    return page.evaluate("document.getElementById('update-dialog').open")


def assert_hit(page, selector):
    box = page.locator(selector).bounding_box()
    view = page.viewport_size
    assert box and box["width"] > 0 and box["height"] > 0, selector
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    assert 0 <= x <= view["width"] and 0 <= y <= view["height"], (selector, box, view)
    assert page.evaluate("([s, x, y]) => document.querySelector(s).contains(document.elementFromPoint(x, y))",
                         [selector, x, y]), selector


def swipe(page, cdp, x, y0, y1, steps=12):
    """A real touch drag through Chromium's input pipeline (finger moves y0 -> y1)."""
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y0}]})
    for i in range(1, steps + 1):
        point = {"x": x, "y": y0 + (y1 - y0) * i / steps}
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [point]})
        page.wait_for_timeout(16)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(250)


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as p:
        instance = p.chromium.launch()
        yield instance
        instance.close()


SCROLL_CASES = [
    ("phone", {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True}, False),
    ("landscape", {"viewport": {"width": 568, "height": 320}, "has_touch": True, "is_mobile": True}, False),
    ("large-text", {"viewport": {"width": 390, "height": 844}, "has_touch": True, "is_mobile": True}, True),
    ("desktop", {"viewport": {"width": 1280, "height": 800}}, False),
]


@pytest.mark.parametrize("name,context,large_text", SCROLL_CASES, ids=[c[0] for c in SCROLL_CASES])
def test_open_dialog_owns_scrolling_and_restores_page(browser, name, context, large_text):
    notes = [{"version": "0.5.9", "notes": LONG}, {"version": "0.5.10", "notes": LONG}]
    ctx, page, fx = boot(browser, catalog(available=offer("0.5.10", 510), notes=notes), **context)
    page.evaluate("""() => {
      const spacer = document.createElement('div'); spacer.id = 'test-spacer'; spacer.style.height = '4000px';
      document.querySelector('main').appendChild(spacer);
      document.body.style.color = 'rgb(1, 2, 3)';
      document.body.classList.add('camera-app-fullscreen-active');
    }""")
    if large_text:
        page.add_style_tag(content="#update-dialog, #update-dialog * { font-size: 28px !important; }")
    page.evaluate("window.scrollTo(0, 700)")
    before = page.evaluate(PAGE_STATE)
    assert before[1] == 700
    page.evaluate("document.getElementById('update-open').click()")
    assert dialog_open(page)
    view = page.viewport_size
    # Notes start collapsed, so the dialog may have nothing to scroll: gestures must still stay put.
    box = page.locator("#update-dialog").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 20)
    for _ in range(5):
        page.mouse.wheel(0, 600)
    page.wait_for_timeout(400)
    assert page.evaluate("scrollY") == 700, "wheel over the collapsed dialog scrolled the dashboard"
    page.evaluate("document.getElementById('update-close').focus()")
    for key in ("PageDown", "End", "ArrowDown", "PageUp", "Home"):
        page.keyboard.press(key)
    page.wait_for_timeout(400)
    assert page.evaluate("scrollY") == 700, "keyboard scrolling from the dialog moved the dashboard"
    cdp = ctx.new_cdp_session(page) if context.get("has_touch") else None
    if context.get("has_touch"):
        assert cdp is not None
        for y0, y1 in ((box["y"] + box["height"] - 10, box["y"] + 10), (box["y"] + 10, box["y"] + box["height"] - 10)):
            swipe(page, cdp, box["x"] + box["width"] / 2, y0, y1)
            assert page.evaluate("scrollY") == 700, "touch over the collapsed dialog scrolled the dashboard"
        swipe(page, cdp, view["width"] - 4, view["height"] - 10, 10)
        assert page.evaluate("scrollY") == 700, "touch over the backdrop scrolled the dashboard"
    page.locator("#update-details summary").click()
    box = page.locator("#update-dialog").bounding_box()
    # Wheel inside the dialog scrolls the dialog, then keeps going well past its end: no chaining.
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + min(box["height"] / 2, 120))
    for _ in range(40):
        page.mouse.wheel(0, 800)
    page.wait_for_timeout(400)
    assert page.evaluate("document.getElementById('update-dialog').scrollTop") > 0
    assert page.evaluate("scrollY") == 700, "dialog wheel chained into the dashboard"
    # Wheel over the backdrop must not move the dashboard either.
    page.mouse.move(view["width"] - 3, 3)
    for _ in range(5):
        page.mouse.wheel(0, 600)
    page.wait_for_timeout(400)
    assert page.evaluate("scrollY") == 700, "backdrop wheel scrolled the dashboard"
    if context.get("has_touch"):
        assert cdp is not None
        page.evaluate("document.getElementById('update-dialog').scrollTop = 0")
        x, top, bottom = box["x"] + box["width"] / 2, box["y"] + 10, box["y"] + box["height"] - 10
        swipe(page, cdp, x, bottom, top)
        assert page.evaluate("document.getElementById('update-dialog').scrollTop") > 0, "touch missed the notes"
        # Past the start, then past the end of the notes: neither edge may hand the gesture to the page.
        for y0, y1 in ((top, bottom), (top, bottom)):
            swipe(page, cdp, x, y0, y1)
        assert page.evaluate("scrollY") == 700, "touch at the top edge moved the dashboard"
        page.evaluate("document.getElementById('update-dialog').scrollTop = 1e6")
        for y0, y1 in ((bottom, top), (bottom, top)):
            swipe(page, cdp, x, y0, y1)
        assert page.evaluate("scrollY") == 700, "touch at the bottom edge moved the dashboard"
        swipe(page, cdp, view["width"] - 4, view["height"] - 10, 10)
        assert page.evaluate("scrollY") == 700, "touch over the backdrop moved the dashboard"
    # The footer actions stay reachable at the end of long notes and at the start.
    page.evaluate("document.getElementById('update-dialog').scrollTop = 1e6")
    page.wait_for_timeout(100)
    assert_hit(page, "#update-install")
    assert_hit(page, "#update-close")
    page.evaluate("document.getElementById('update-dialog').scrollTop = 0")
    page.wait_for_timeout(100)
    assert_hit(page, "#update-close")
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / f"scroll-{name}.png"))
    assert page.evaluate("getComputedStyle(document.documentElement).overflowY") == "hidden"
    # Anything that still moves the page programmatically behind the modal is undone on release.
    page.evaluate("document.getElementById('test-spacer').scrollIntoView()")
    page.keyboard.press("Escape")
    # Native Escape closes synchronously but dispatches `close` as a task; the release must follow it.
    page.wait_for_function(RELEASED)
    after = page.evaluate(PAGE_STATE)
    assert after == before
    assert page.evaluate("document.body.classList.contains('camera-app-fullscreen-active')")
    # The page scrolls normally again after release.
    page.evaluate("window.scrollTo(0, 900)")
    assert page.evaluate("scrollY") == 900
    assert fx.errors == []
    ctx.close()


def test_scroll_lock_released_on_every_exit(browser):
    notes = [{"version": "0.5.9", "notes": LONG}]
    body = catalog(available=offer("0.5.9", 509), notes=notes)
    ctx, page, fx = boot(browser, body, viewport={"width": 390, "height": 844})
    page.evaluate("""() => { const s = document.createElement('div'); s.id = 'test-spacer'; s.style.height = '4000px';
      document.querySelector('main').appendChild(s); window.scrollTo(0, 500); }""")
    locked = "getComputedStyle(document.documentElement).overflowY === 'hidden'"
    # Reconnect re-shows the update notice above the viewport, so Chromium's scroll anchoring may legitimately
    # move scrollY between cycles; each exit must restore the position from just before that dialog opened.
    opened_at = []

    def reopen():
        opened_at.append(page.evaluate("scrollY"))
        page.evaluate("document.getElementById('update-open').click()")
        assert dialog_open(page) and page.evaluate(locked)

    def released():
        assert not dialog_open(page)
        assert not page.evaluate(locked)
        assert page.evaluate("scrollY") == opened_at[-1]

    reopen()
    page.locator("#update-close").click()
    released()
    reopen()
    page.keyboard.press("Escape")
    page.wait_for_function(RELEASED)
    released()
    reopen()
    fx.read_status = 502
    fx.refresh()
    released()
    fx.read_status = 200
    fx.refresh()
    reopen()
    page.once("dialog", lambda d: d.accept())
    page.locator("#update-install").click()
    page.wait_for_function("!document.getElementById('update-dialog').open")
    released()
    assert opened_at[:3] == [500, 500, 500]
    assert fx.posts[-1] == ("/api/updates/install", {"confirm": "INSTALL", "release_id": 509})
    ctx.close()


def test_skipped_versions_preview_and_direct_install(browser):
    # Out-of-order on purpose: lexical order would put 0.5.10 before 0.5.9.
    notes = [{"version": v, "notes": f"TEST DATA notes for {v}"} for v in ("0.5.12", "0.5.10", "0.5.9", "0.5.11")]
    ctx, page, fx = boot(browser, catalog(current="0.5.8", available=offer("0.5.12", 512), notes=notes),
                         viewport={"width": 390, "height": 844})
    page.locator("#update-open").click()
    summary = page.locator("#update-summary").inner_text()
    assert "0.5.8" in summary and "0.5.12" in summary
    assert "one direct" in summary.lower() and "not installed separately" in summary.lower()
    page.locator("#update-details summary").click()
    headings = page.locator("#update-notes section h3").all_inner_texts()
    assert headings == ["Homebody 0.5.9", "Homebody 0.5.10", "Homebody 0.5.11", "Homebody 0.5.12"]
    assert page.locator("#update-notes section").nth(1).inner_text().endswith("TEST DATA notes for 0.5.10")
    coverage = page.locator("#update-coverage").inner_text()
    assert "4 published releases" in coverage and "incomplete" not in coverage
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / "skipped-versions-phone.png"))
    messages = []
    page.once("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.locator("#update-install").click()
    assert "0.5.12" in messages[0] and "0.5.8" in messages[0]
    page.wait_for_function("!document.getElementById('update-dialog').open")
    assert fx.posts == [("/api/updates/install", {"confirm": "INSTALL", "release_id": 512})]
    ctx.close()


@pytest.mark.parametrize("notes,expected", [
    ([], "unavailable"),
    ([{"version": "0.5.9", "notes": "TEST DATA"}], "may be incomplete"),
    ([{"version": "0.5.9", "notes": "TEST"}, {"version": "0.5.10", "notes": "x" * 100000}], "shortened"),
])
def test_missing_or_truncated_notes_never_claim_complete_range(browser, notes, expected):
    ctx, page, fx = boot(browser, catalog(available=offer("0.5.10", 510), notes=notes))
    page.locator("#update-open").click()
    coverage = page.locator("#update-coverage").inner_text()
    assert expected in coverage
    assert "0.5.10" in page.locator("#update-summary").inner_text()
    ctx.close()


def test_newest_completion_once_without_rotation_or_auto_ack(browser):
    history = [success("a", "0.5.8", "0.5.9", [{"version": "0.5.9", "notes": "TEST old"}]),
               success("b", "0.5.9", "0.5.10", [{"version": "0.5.10", "notes": "TEST new"}])]
    body = catalog(current="0.5.10", history=history)
    ctx, page, fx = boot(browser, body, viewport={"width": 390, "height": 844})
    page.wait_for_function("document.getElementById('update-dialog').open")
    assert "0.5.10" in page.locator("#update-summary").inner_text()
    seen = []
    for _ in range(3):
        fx.refresh()
        seen.append(page.locator("#update-summary").inner_text())
    assert all("to 0.5.10" in s for s in seen), seen
    page.locator("#update-close").click()
    for _ in range(3):
        fx.refresh()
        assert not dialog_open(page), "closed completion re-opened or older one rotated in"
    assert fx.posts == []
    assert all(not h["acknowledged"] for h in body["history"])
    page.click("#tab-settings")
    page.locator("#updates-card details > summary").first.click()
    assert page.locator("#update-history > details").count() == 2
    page.locator("#update-history > details > summary").first.click()
    fx.refresh()
    assert page.evaluate("document.querySelector('#update-history > details').open"), "poll collapsed History"
    reopen = page.locator("#update-history button")
    assert reopen.count() == 1
    reopen.click()
    assert dialog_open(page) and "to 0.5.10" in page.locator("#update-summary").inner_text()
    page.locator("#update-ack").click()
    page.wait_for_function("!document.getElementById('update-dialog').open")
    assert fx.posts == [("/api/updates/acknowledge", {"job_id": "b" * 32})]
    fx.refresh()
    assert not dialog_open(page)
    assert body["history"][0]["acknowledged"] is False
    ctx.close()


@pytest.mark.parametrize("history,job,current", [
    ([success("a", "0.5.8", "0.5.9"), success("c", "0.5.9", "0.5.10", state="rolled_back")], None, "0.5.9"),
    ([success("a", "0.5.8", "0.5.9"), success("c", "0.5.9", "0.5.10", state="failed")], "failed", "0.5.9"),
    ([success("a", "0.5.8", "0.5.9")], "installing", "0.5.9"),
    ([success("a", "0.5.8", "0.5.9")], None, "0.5.8"),
], ids=["rolled-back", "failed-job", "active-job", "not-current"])
def test_stale_success_not_resurrected(browser, history, job, current):
    job_body = {**history[-1], "id": "d" * 32, "state": job} if job else None
    ctx, page, fx = boot(browser, catalog(current=current, history=history, job=job_body))
    for _ in range(2):
        fx.refresh()
        assert not dialog_open(page)
    ctx.close()


def test_acknowledge_failure_keeps_dialog_and_owner_loss_releases(browser):
    body = catalog(current="0.5.10", history=[success("b", "0.5.9", "0.5.10")])
    ctx, page, fx = boot(browser, body, viewport={"width": 390, "height": 844})
    page.wait_for_function("document.getElementById('update-dialog').open")
    fx.post_status["/api/updates/acknowledge"] = 500
    page.locator("#update-ack").click()
    page.wait_for_function("document.getElementById('update-dialog-error').textContent.includes('not confirmed')")
    assert dialog_open(page) and page.locator("#update-dialog-error").is_visible()
    assert page.locator("#update-ack").is_enabled()
    assert_hit(page, "#update-dialog-error")
    assert_hit(page, "#update-ack")
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SHOTS / "ack-failure-phone.png"))
    fx.refresh()
    assert dialog_open(page) and "to 0.5.10" in page.locator("#update-summary").inner_text()
    fx.post_status["/api/updates/acknowledge"] = 401
    page.locator("#update-ack").click()
    page.wait_for_function("!document.getElementById('update-dialog').open")
    assert page.evaluate("getComputedStyle(document.documentElement).overflowY") != "hidden"
    assert body["history"][0]["acknowledged"] is False
    ctx.close()


def test_install_bound_to_previewed_release(browser):
    body = catalog(current="0.5.8", available=offer("0.5.11", 511),
                   notes=[{"version": "0.5.11", "notes": "TEST DATA 0.5.11"}])
    ctx, page, fx = boot(browser, body, viewport={"width": 390, "height": 844})
    page.locator("#update-open").click()
    changed = copy.deepcopy(body)
    changed["available"] = offer("0.5.12", 512)
    changed["notes"] = [{"version": "0.5.11", "notes": "TEST"}, {"version": "0.5.12", "notes": "TEST DATA unseen"}]
    fx.catalog = changed
    fx.refresh()
    assert dialog_open(page)
    assert "0.5.11" in page.locator("#update-summary").inner_text(), "open preview silently swapped release"
    assert page.locator("#update-install").is_disabled()
    assert "0.5.12" in page.locator("#update-dialog-error").inner_text()
    assert_hit(page, "#update-dialog-error")
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.wait_for_timeout(600)  # let the dialog's entry motion settle for a readable capture
    page.screenshot(path=str(SHOTS / "changed-offer-phone.png"))
    # The handler itself refuses too, even if something re-enabled the button.
    page.on("dialog", lambda d: d.accept())
    page.evaluate(FORCE_INSTALL)
    page.wait_for_timeout(200)
    assert [p for p in fx.posts if p[0].endswith("/install")] == [], "installed an unseen release"
    page.locator("#update-close").click()
    page.locator("#update-open").click()
    assert "0.5.12" in page.locator("#update-summary").inner_text()
    page.locator("#update-install").click()
    page.wait_for_function("!document.getElementById('update-dialog').open")
    assert fx.posts == [("/api/updates/install", {"confirm": "INSTALL", "release_id": 512})]
    # Removed offer while previewing: no install of the stale target.
    fx.catalog = copy.deepcopy(body)
    fx.posts.clear()
    fx.refresh()
    page.locator("#update-open").click()
    gone = copy.deepcopy(body)
    gone["available"] = None
    gone["notes"] = []
    fx.catalog = gone
    fx.refresh()
    assert page.locator("#update-install").is_disabled()
    assert "no longer" in page.locator("#update-dialog-error").inner_text()
    page.evaluate(FORCE_INSTALL)
    page.wait_for_timeout(200)
    assert fx.posts == []
    assert fx.errors == []
    ctx.close()
