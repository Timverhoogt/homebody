"""Focused Settings integration proof. APIs are intercepted: never a robot/host write."""
import json
import os
from pathlib import Path
import time

import pytest
from test_assistant_modes_browser import ui  # noqa: F401

ROOT=Path(os.environ.get('SETTINGS_PROOF_DIR','.artifacts/settings/final'))

@pytest.fixture
def settings_ui(ui):
    page,state,posts,errors=ui
    state['config'].update(language='en',continuous_conversation=False,motion_enabled=True,
                           robot_tools_enabled=False,camera_enabled=False,camera_feed_enabled=False)
    state['runtime'].update(motors_enabled=True,head_safely_folded=False,last_error='',robot_busy=False)
    state['badge']={'status':'idle','running':False}
    state['agent_connection']={'state':'connected','label':'Hermes','fresh_for_ms':25000,'detail':'Bridge available on the local network.'}
    page.route('**/api/status',lambda r:r.fulfill(json={
        'config':state['config'],'runtime':state['runtime'],'agent_connection':state['agent_connection']}))
    page.route('**/api/updates',lambda r:r.fulfill(json={
        'supported':True,'current':{'version':'0.5.45'},'available':None,'checked_at':time.time(),
        'job':None,'history':[],'error':''}))
    page.route('**/api/models',lambda r:r.fulfill(json={'configured':True,'models':[{'id':'hermes-agent','label':'Hermes default'}]}))
    page.route('**/api/audio/volume',lambda r:r.fulfill(json={
        'volume':35,'muted':False,'can_unmute':True}))
    def save(route):
        if route.request.method!='POST':
            route.fulfill(json={'ok':True,'config':state['config']});return
        body=json.loads(route.request.post_data)
        posts.append(('/api/settings',body))
        state['config'].update(body)
        route.fulfill(json={'ok':True,'config':state['config'],'note':'Saved'})
    page.route('**/api/settings',save)
    posts.clear();errors.clear();page.reload()
    page.wait_for_function("!document.querySelector('main').hidden && typeof updateStatus==='function'")
    yield page,state,posts,errors


def test_original_settings_controls_are_single_and_form_owned(settings_ui):
    page,_,posts,errors=settings_ui
    before=json.loads(Path('tests/fixtures/settings-controls.json').read_text())
    actual=page.locator('input[id],select[id],textarea[id],button[id]').evaluate_all('(xs)=>xs.filter(x=>x.id).map(x=>({id:x.id,tag:x.tagName,type:x.type,name:x.name,form:x.form?.id||null}))')
    by_id={item['id']:item for item in actual}
    assert len(by_id)==len(actual),'duplicate settings controls'
    assert not [item['id'] for item in before if item['id'] not in by_id],'original functional controls lost'
    assert not [(item,by_id[item['id']]) for item in before if item['form']=='settings-form' and by_id[item['id']]['form']!='settings-form'],'moved fields lost form owner'
    assert not posts and not errors
    ROOT.mkdir(parents=True,exist_ok=True)
    (ROOT/'control-inventory.json').write_text(json.dumps({'before':len(before),'global_controls_after':len(actual),'form_ownership_preserved':True,'original_ids_preserved':True},indent=2))


def choose(page,key):
    picker=page.locator('#settings-category-select')
    if picker.is_visible():
        picker.select_option(key)
    else:
        page.locator(f'[data-settings-nav="{key}"]').click()
    assert page.locator('[data-settings-category]:visible').count()==1
    assert page.locator(f'[data-settings-category="{key}"]').is_visible()


@pytest.mark.parametrize('width,theme',[(320,'light'),(390,'light'),(1440,'dark')])
def test_real_categories_are_separate_and_fit(settings_ui,width,theme):
    page,_,posts,errors=settings_ui
    page.set_viewport_size({'width':width,'height':900})
    page.emulate_media(color_scheme=theme,reduced_motion='reduce')
    page.click('#tab-settings')
    ROOT.mkdir(parents=True,exist_ok=True)
    for key in ['everyday','voice','privacy','connections','system']:
        choose(page,key)
        page.wait_for_timeout(180)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),key
        assert page.locator('[data-settings-category]:visible').count()==1
        assert page.locator(f'[data-settings-nav="{key}"]').get_attribute('aria-current')=='page'
        if width!=320 or key=='everyday':
            page.screenshot(path=str(ROOT/f'{key}-{width}-{theme}-top.png'),animations='disabled')
            page.screenshot(path=str(ROOT/f'{key}-{width}-{theme}-full.png'),full_page=True,animations='disabled')
    assert not posts,posts
    assert not errors,errors


def test_save_draft_and_hidden_validation_through_visible_controls(settings_ui):
    page,state,posts,errors=settings_ui
    page.click('#tab-settings');choose(page,'voice')
    page.locator('#language').fill('nl')
    baseline=page.evaluate('payloadFromForm()')
    choose(page,'connections')
    bridge=page.locator('#bridge_url')
    if not bridge.is_visible():
        page.locator('details').filter(has=bridge).last.locator('summary').first.click()
    original=bridge.input_value();bridge.fill('')
    choose(page,'everyday');choose(page,'voice')
    assert page.locator('#language').input_value()=='nl','draft lost on category change'
    page.locator('#settings-save-button').click()
    assert bridge.is_visible(),'invalid field stranded in a hidden section'
    assert page.evaluate('document.activeElement.id')=='bridge_url'
    assert not posts,'invalid form must not save'
    bridge.fill(original);choose(page,'voice')
    with page.expect_response(lambda r:r.url.endswith('/api/settings') and r.request.method=='POST') as ack:
        page.locator('#settings-save-button').click()
    assert ack.value.ok
    assert posts==[('/api/settings',baseline)],posts
    assert state['config']['language']=='nl'
    page.locator('#notifications').get_by_text('Saved',exact=True).wait_for(state='visible')
    assert not errors,errors


def test_real_connection_badge_and_permission_link_reveal_without_writes(settings_ui):
    page,state,posts,errors=settings_ui
    page.click('#tab-settings');choose(page,'system')
    page.locator('#agent-connection-badge').click()
    assert page.locator('[data-settings-category="connections"]').is_visible()
    assert page.locator('#agent-connection-details').is_visible()
    assert page.evaluate('document.activeElement.id') in ['agent-connection-details','agent-connection-retry']
    choose(page,'privacy')
    link=page.locator('[data-everyday-setting="camera_enabled"]')
    if not link.is_visible():
        page.locator('#everyday-permissions > summary').click()
    link.click()
    assert page.locator('#camera_enabled').is_visible()
    assert page.evaluate('document.activeElement.id')=='camera_enabled'
    assert not page.locator('#camera_enabled').is_checked()
    page.reload();page.wait_for_function("!document.querySelector('main').hidden")
    page.click('#tab-settings')
    assert page.locator('[data-settings-category="privacy"]').is_visible()
    assert not posts,posts
    assert not errors,errors
