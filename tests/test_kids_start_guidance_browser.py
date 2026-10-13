# ruff: noqa: F811 -- imported fixture is intentionally a test parameter
"""Mocked Chromium: family guidance agrees with automatic first-turn listening."""

from pathlib import Path

import pytest
from test_assistant_modes_browser import ui  # noqa: F401


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_kids_start_guidance_matches_first_listen(ui, width):
    page, state, posts, errors = ui
    page.set_viewport_size({"width": width, "height": 900})
    page.click("#tab-kids")
    guide = page.locator(".kids-guide-card")
    guide.scroll_into_view_if_needed()
    text = guide.inner_text()
    assert "first answer automatically" in text
    assert "no wake phrase needed" in text
    assert "resume after listening stops" in text
    assert "After Reachy's greeting, say" not in text
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    destination = Path(__file__).resolve().parents[1] / ".artifacts" / "kids-guidance"
    destination.mkdir(parents=True, exist_ok=True)
    talking = guide.locator(".kids-guide-grid > div").first
    talking.evaluate("element => element.scrollIntoView({block: 'center'})")
    talking.screenshot(path=str(destination / f"guide-{width}.png"))
    assert not errors
