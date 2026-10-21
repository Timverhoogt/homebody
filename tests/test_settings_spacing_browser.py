"""Rendered Settings spacing, with all device/API traffic intercepted."""

# ruff: noqa: F811
import json
import os
from pathlib import Path

import pytest
from test_settings_navigation_browser import choose, settings_ui, ui  # noqa: F401

ROOT = Path(".artifacts/settings-spacing") / os.getenv("SPACING_STAGE", "after")


@pytest.mark.parametrize("width,height,theme", [(390, 760, "dark"), (320, 760, "light"), (1440, 900, "dark")])
def test_settings_footer_spacing(settings_ui, width, height, theme):
    page, _, posts, errors = settings_ui
    page.set_viewport_size({"width": width, "height": height})
    page.emulate_media(color_scheme=theme, reduced_motion="reduce")
    page.click("#tab-settings")
    choose(page, "voice")
    page.wait_for_timeout(160)
    ROOT.mkdir(parents=True, exist_ok=True)
    metrics = page.evaluate("""() => {
      const box=e=>{
        const b=e.getBoundingClientRect(),s=getComputedStyle(e);
        return {top:b.top+scrollY,bottom:b.bottom+scrollY,height:b.height,
                padding:s.padding,margin:s.margin,border:s.borderTopWidth};
      };
      const selectors=['#speech-provider-details','#settings-save','#settings-save-button',
                       '#settings-save > .compact-note','#settings-save > details'];
      const rows=[...document.querySelectorAll('#panel-settings details')]
        .filter(e=>e.getClientRects().length&&!e.open)
        .map(e=>({id:e.id,title:e.querySelector(':scope > summary')?.textContent,
                  details:box(e),summary:box(e.querySelector(':scope > summary'))}));
      const elements=Object.fromEntries(selectors.map(s=>[s,box(document.querySelector(s))]));
      return {elements,rows};
    }""")
    (ROOT / f"metrics-{width}.json").write_text(json.dumps(metrics, indent=2))
    page.locator("#settings-save").scroll_into_view_if_needed()
    page.screenshot(path=str(ROOT / f"voice-footer-{width}.png"), animations="disabled")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not posts and not errors
    helper = metrics["elements"]["#settings-save > .compact-note"]
    button = metrics["elements"]["#settings-save-button"]
    help_gap = helper["top"] - button["bottom"]
    assert 8 <= help_gap <= 16, f"Save helper gap: {help_gap}"
    for row in metrics["rows"]:
        assert row["summary"]["height"] >= 47.999, row
        assert row["details"]["bottom"] - row["summary"]["bottom"] <= 9, row
    rows_by_title = {row["title"]: row for row in metrics["rows"]}
    speech = rows_by_title["Speech recognition and spoken voices"]
    timing = rows_by_title["Advanced voice timing"]
    row_gap = timing["details"]["top"] - speech["details"]["bottom"]
    assert 8 <= row_gap <= 16, f"Doubled disclosure spacing: {row_gap}"
    route = page.locator(".global-robot-status .voice-route")
    assert route.evaluate("(e)=>getComputedStyle(e).borderTopWidth") == "0px"
    stop = page.locator("#voice-stop-button")
    stop.scroll_into_view_if_needed()
    assert stop.evaluate("""e => {
      const b=e.getBoundingClientRect();
      const hit=document.elementFromPoint(b.x+b.width/2,b.y+b.height/2);
      return b.height >= 47.999 && (hit===e || e.contains(hit));
    }""")
    for selector in ["#speech-provider-details > summary", "#settings-save > details > summary"]:
        page.locator(selector).click()
        assert page.locator(selector).evaluate("(e)=>e.parentElement.open")
    page.screenshot(path=str(ROOT / f"voice-expanded-{width}.png"), full_page=True, animations="disabled")
    assert not posts and not errors
