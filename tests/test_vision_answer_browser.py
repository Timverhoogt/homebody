"""Browser-only vision UI checks; synthetic answers, no robot/camera requests."""
pytest_plugins = ["test_notifications_browser"]


def open_vision(page):
    page.set_default_timeout(5000)
    page.clock.resume()
    page.route('**/api/status', lambda r: r.fulfill(json={
        'config': {'local_vision_enabled': True, 'camera_enabled': True},
        'runtime': {'state': 'waiting_for_wake_word', 'power_mode': 'awake'},
    }))
    page.evaluate("document.querySelector('#tab-robot').click()")
    page.evaluate("document.querySelector('#local-vision-ask').hidden = false")
    page.locator('#local-vision-question').fill('What is on the desk?')


def test_vision_answer_separates_content_and_metadata(browser_page):
    page, errors = browser_page
    page.route('**/api/vision/describe', lambda r: r.fulfill(json={
        'ok': True, 'answer': 'A red cup.\n<img src=x onerror=alert(1)>',
        'model': 'qwen3-vl:2b-instruct', 'latency_ms': 5442}))
    open_vision(page)
    page.locator('#local-vision-ask-button').click()
    page.wait_for_function("document.querySelector('#local-vision-result')?.dataset.state === 'ready'")
    assert page.locator('#local-vision-answer').inner_text() == 'A red cup.\n<img src=x onerror=alert(1)>'
    assert page.locator('#local-vision-answer img').count() == 0
    assert page.locator('#local-vision-result-question').inner_text() == 'What is on the desk?'
    assert page.locator('#local-vision-model').inner_text() == 'qwen3-vl:2b-instruct'
    assert page.locator('#local-vision-timing').inner_text() == '5.4 s'
    assert page.locator('#local-vision-copy').is_enabled()
    assert not errors


def test_vision_pending_error_and_retry(browser_page):
    page, errors = browser_page
    pending = []
    page.route('**/api/vision/describe', lambda r: pending.append(r))
    open_vision(page)
    page.locator('#local-vision-ask-button').click()
    page.wait_for_function("document.querySelector('#local-vision-result')?.dataset.state === 'loading'")
    assert page.locator('#local-vision-ask-button').is_disabled()
    assert page.locator('#local-vision-result').get_attribute('aria-busy') == 'true'
    assert page.locator('#local-vision-copy').is_hidden()
    pending.pop().fulfill(status=503, json={'detail': 'Model unavailable'})
    page.wait_for_function("document.querySelector('#local-vision-result').dataset.state === 'error'")
    assert page.locator('#local-vision-answer').inner_text() == 'Model unavailable'
    assert page.locator('#local-vision-ask-button').is_enabled()
    assert page.locator('#local-vision-meta').is_hidden()
    page.locator('#local-vision-ask-button').click()
    page.wait_for_function("document.querySelector('#local-vision-result').dataset.state === 'loading'")
    pending.pop().fulfill(json={'ok': True, 'answer': 'A cup', 'model': 'test', 'latency_ms': 2000})
    page.wait_for_function("document.querySelector('#local-vision-result').dataset.state === 'ready'")
    assert not errors


def test_vision_empty_answer_is_not_success(browser_page):
    page, errors = browser_page
    page.route('**/api/vision/describe', lambda r: r.fulfill(json={'ok': True, 'answer': '  '}))
    open_vision(page)
    page.locator('#local-vision-ask-button').click()
    page.wait_for_function("document.querySelector('#local-vision-result')?.dataset.state === 'error'")
    assert 'empty answer' in page.locator('#local-vision-answer').inner_text()
    assert not errors


def test_vision_mobile_layout_and_copy(browser_page):
    page, errors = browser_page
    page.evaluate("""window.__copied = '';
        Object.defineProperty(navigator, 'clipboard', {
            value: {writeText: async t => {window.__copied = t}}
        })""")
    page.route('**/api/vision/describe', lambda r: r.fulfill(json={
        'ok': True, 'answer': 'A red notebook lies beside a blue mug.\nThe title reads LOCAL VISION TEST.',
        'model': 'qwen3-vl:2b-instruct', 'latency_ms': 5442}))
    open_vision(page)
    page.locator('#local-vision-ask-button').click()
    page.wait_for_function("document.querySelector('#local-vision-result')?.dataset.state === 'ready'")
    page.locator('#local-vision-copy').click()
    assert page.evaluate('window.__copied').startswith('A red notebook')
    assert 'qwen3' not in page.evaluate('window.__copied')
    for width in (390, 1280):
        page.set_viewport_size({'width': width, 'height': 900})
        page.locator('#local-vision-result').scroll_into_view_if_needed()
        box = page.locator('#local-vision-result').bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= width
        page.screenshot(path=f'.pytest_cache/notification-browser/vision-{width}.png', animations='disabled')
    assert not errors
