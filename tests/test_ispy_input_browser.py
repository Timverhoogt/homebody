# ruff: noqa: F811 -- imported pytest fixture is intentionally used as a test parameter
import json
from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401

ACTIVE = {"active": True, "locked": True, "profile": {"activity": "ispy"}}
TOKEN = "a" * 32


@pytest.mark.parametrize("width", [320, 390])
def test_typed_guess_enter_sends_once_and_fits_phone(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 1000})
    sent = []
    page.route("**/api/kids/ispy/input", lambda r: r.fulfill(json={"available": True, "token": TOKEN}))

    def guess(route):
        sent.append(json.loads(route.request.post_data))
        route.fulfill(json={"accepted": True})

    page.route("**/api/kids/ispy/guess", guess)
    state["runtime"]["kids_mode"] = ACTIVE
    page.evaluate("s => updateStatus({config:s.config,runtime:s.runtime})", state)
    page.click("#tab-kids")
    page.wait_for_function("!document.querySelector('#kids-ispy-guess').disabled")
    page.fill("#kids-ispy-guess", "blue cup")
    page.press("#kids-ispy-guess", "Enter")
    page.wait_for_function("document.querySelector('#kids-ispy-guess-message').textContent.includes('Guess checked')")
    assert sent == [{"text": "blue cup", "token": TOKEN}]
    assert page.input_value("#kids-ispy-guess") == ""
    assert not page.evaluate("Object.values({...localStorage,...sessionStorage}).some(x=>x.includes('blue cup'))")
    page.locator("#kids-ispy-guess-form").scroll_into_view_if_needed()
    geometry = page.evaluate("""() => {
        const a=document.querySelector('#kids-ispy-guess').getBoundingClientRect();
        const b=document.querySelector('#kids-ispy-guess-send').getBoundingClientRect();
        return {gap:b.left-a.right,right:b.right,height:b.height};}""")
    assert geometry["gap"] >= 9 and geometry["right"] <= width and geometry["height"] >= 48
    Path("/home/timv/.hermes/cache/scratch/hb-ew/screens").mkdir(exist_ok=True)
    page.screenshot(path=f"/home/timv/.hermes/cache/scratch/hb-ew/screens/ispy-input-{width}.png")
    assert not errors


def test_pending_guess_and_late_reply_do_not_survive_end(ui):
    page, state, posts, errors = ui
    held = []
    page.route("**/api/kids/ispy/input", lambda r: r.fulfill(json={"available": True, "token": TOKEN}))
    page.route("**/api/kids/ispy/guess", lambda r: held.append(r))
    state["runtime"]["kids_mode"] = ACTIVE
    page.evaluate("s => updateStatus({config:s.config,runtime:s.runtime})", state)
    page.click("#tab-kids")
    page.wait_for_function("!document.querySelector('#kids-ispy-guess').disabled")
    page.fill("#kids-ispy-guess", "cup")
    page.press("#kids-ispy-guess", "Enter")
    page.wait_for_function("document.querySelector('#kids-ispy-guess-send').disabled")
    assert len(held) == 1
    state["runtime"]["kids_mode"] = {}
    page.evaluate("HomebodyISpyInput.update({active:false})")
    held[0].fulfill(json={"accepted": True})
    page.wait_for_timeout(100)
    assert page.locator("#kids-ispy-guess-form").is_hidden()
    assert page.input_value("#kids-ispy-guess") == ""
    assert page.locator("#kids-ispy-guess-message").inner_text() == ""
    assert not errors


def test_failed_guess_has_feedback_and_non_ispy_has_no_input(ui):
    page, state, posts, errors = ui
    page.route("**/api/kids/ispy/input", lambda r: r.fulfill(json={"available": True, "token": TOKEN}))
    page.route(
        "**/api/kids/ispy/guess",
        lambda r: r.fulfill(status=409, json={"detail": "This turn ended. Refresh and try again."}),
    )
    state["runtime"]["kids_mode"] = ACTIVE
    page.evaluate("s => updateStatus({config:s.config,runtime:s.runtime})", state)
    page.click("#tab-kids")
    page.wait_for_function("!document.querySelector('#kids-ispy-guess').disabled")
    page.fill("#kids-ispy-guess", "cup")
    page.click("#kids-ispy-guess-send")
    page.wait_for_function(
        "document.querySelector('#kids-ispy-guess-message').textContent.includes('This turn ended')"
    )
    page.evaluate("HomebodyISpyInput.update({active:true,profile:{activity:'story'}})")
    assert page.locator("#kids-ispy-guess-form").is_hidden()
    assert not errors
