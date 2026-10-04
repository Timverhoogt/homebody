"""Execute power feedback behavior without accessing a robot."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_power_feedback_and_panel_independent_click():
    if not shutil.which("node"):
        pytest.skip("Node is required for the frontend behavioral test")
    source = Path("homebody/static/main.js").read_text()
    handler = source[source.index("function showPowerToast") : source.index("async function responseDetail")]
    harness = Path("tests/fixtures/notifications_dom.cjs").read_text()
    notifications = Path("homebody/static/notifications.js").read_text()
    setup = r"""
let powerTransitionPending = false;
const home = element('home-awake', 'awake');
const robot = element('robot-awake', 'awake');
for (const id of ['power-message','robot-message','emotion-select','robot-stop-button']) element(id);
async function refreshStatus(){home.disabled=false;robot.disabled=false;}
let resolveRequest;
let calls=0;
let fetch = () => {calls++; return new Promise(resolve => {resolveRequest=resolve;});};
"""
    assertions = r"""
(async () => {
  home.click();
  assert.equal(calls,1);
  assert.equal(home.textContent,'Waking…');
  assert.equal(home['aria-busy'],'true');
  assert.equal(home.disabled,true);
  assert.equal(notices()[0].children[0].textContent,'Waking…');
  assert.equal(timers.size,0);
  notices()[0].children[1].click();
  assert.equal(notices().length,0);
  assert.equal(powerTransitionPending,true, 'dismissal must not cancel the robot operation');
  await setPowerMode('awake');
  assert.equal(calls,1);
  resolveRequest({ok:true,json:async()=>({runtime:{power_mode:'awake',motors_enabled:true}})});
  await new Promise(setImmediate);
  assert.equal(powerTransitionPending,false);
  assert.equal(home.textContent,'awake');
  assert.equal(home['aria-busy'],undefined);
  assert.equal(notices()[0].className,'notification notification-ok');
  const successTimer = [...timers.keys()][0];
  fetch=async()=>({ok:true,json:async()=>({runtime:{power_mode:'standby',motors_enabled:false}})});
  await setPowerMode('awake');
  assert.equal(notices().length,1);
  assert.equal(notices()[0].className,'notification notification-error');
  assert.match(notices()[0].textContent,/not confirmed/);
  assert.equal(timers.has(successTimer),false);
  advance(60000);
  assert.equal(notices().length,1, 'errors remain until dismissed');
  notices()[0].children[1].click();
  fetch=async()=>{throw new Error('network unavailable');};
  await setPowerMode('awake');
  assert.match(notices()[0].textContent,/network unavailable/);
  assert.equal(powerTransitionPending,false);
  assert.equal(home.disabled,false);
})().catch(e => {console.error(e); process.exitCode=1;});
"""
    subprocess.run(["node", "-e", harness + notifications + setup + handler + assertions], check=True, timeout=10)
