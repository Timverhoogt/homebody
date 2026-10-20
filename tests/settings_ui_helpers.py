"""Use the real responsive Settings navigation in browser checks."""


def choose(page, key):
    page.locator("#panel-settings").wait_for(state="visible")
    picker = page.locator("#settings-category-select")
    if picker.is_visible():
        picker.select_option(key)
    else:
        page.locator(f'[data-settings-nav="{key}"]').click()
    assert page.locator("[data-settings-category]:visible").count() == 1
    assert page.locator(f'[data-settings-category="{key}"]').is_visible()
