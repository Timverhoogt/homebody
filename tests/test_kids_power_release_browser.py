# ruff: noqa: F811
"""Final narrow UI regressions; every API call uses an isolated fixture."""
import pytest
from test_assistant_modes_browser import ui  # noqa: F401


def test_awake_power_does_not_claim_a_listening_voice_session(ui):
    page, state, posts, errors = ui
    state['runtime'].update(power_mode='awake', state='speaking', motors_enabled=True)
    page.evaluate("document.getElementById('tab-settings').click()")  # behind More on phones
    page.evaluate('refreshStatus()')
    page.wait_for_function("document.getElementById('compact-power-mode').textContent==='Awake'")
    assert 'listening' not in page.locator('#compact-power-detail').inner_text().lower()
    assert not posts and not errors


@pytest.mark.parametrize('font_scale',[1,1.25])
def test_short_phone_setup_entry_and_navigation_labels_are_uncovered(ui,font_scale):
    page, state, posts, errors = ui
    page.emulate_media(color_scheme='dark',reduced_motion='reduce')
    page.set_viewport_size({'width':360,'height':640})
    page.evaluate('scale=>document.documentElement.style.fontSize=`${16*scale}px`',font_scale)
    page.click('#tab-kids')
    page.evaluate('window.scrollTo(0,0)')
    geometry=page.evaluate("""() => {
      const e=document.getElementById('kids-host-setup-link'),b=e.getBoundingClientRect();
      // The fixed phone bar is .tabs-primary (destinations + More); .tabs is the static top-bar wrapper.
      const nav=document.querySelector('.tabs-primary').getBoundingClientRect();
      const hit=document.elementFromPoint(b.left+b.width/2,b.top+b.height/2);
      const labels=[...document.querySelectorAll('.tabs-primary button')].filter(e=>!e.hidden).map(button=>{
        const range=document.createRange();range.selectNodeContents(button);
        const text=range.getBoundingClientRect(),box=button.getBoundingClientRect();
        return {label:button.textContent,left:text.left,right:text.right,boxLeft:box.left,boxRight:box.right};
      });
      return {top:b.top,bottom:b.bottom,navTop:nav.top,hit:hit===e||e.contains(hit),labels};
    }""")
    assert geometry['top']>=0 and geometry['bottom']<=geometry['navTop'] and geometry['hit'],geometry
    for label in geometry['labels']:
        assert label['left']>=label['boxLeft']-0.001 and label['right']<=label['boxRight']+0.001,label
    assert not posts and not errors
