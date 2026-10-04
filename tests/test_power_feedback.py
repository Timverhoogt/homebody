"""Execute the power handler against a minimal DOM, without robot access."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_power_feedback_and_panel_independent_click():
    if not shutil.which("node"):
        pytest.skip("Node is required for the frontend behavioral test")
    source = Path("homebody/static/main.js").read_text()
    handler = source[source.index("let powerToastTimer;"):source.index("async function responseDetail")]
    harness = r'''
const assert = require('node:assert/strict');
let powerTransitionPending = false;
const elements = {};
function element(id, power) {
  return elements[id] = { id, dataset: power ? {power} : {}, textContent: power || '', disabled: false,
    classList: {add(){}, remove(){}}, setAttribute(k,v){this[k]=v;}, removeAttribute(k){delete this[k];},
    closest(){return null;}, addEventListener(event, fn){this.click=fn;} };
}
const home = element('home-awake', 'awake');
const robot = element('robot-awake', 'awake');
for (const id of ['power-message','robot-message','emotion-select','robot-stop-button']) element(id);
const $ = id => elements[id];
const document = {querySelectorAll(){return [home, robot];},
 createElement(){return element('power-toast');}, body:{appendChild(){}}};
const window = {};
let timerId = 0;
function setTimeout(){return ++timerId;}
function clearTimeout(){}
async function refreshStatus(){home.disabled=false;robot.disabled=false;}
let resolveRequest;
let calls=0;
let fetch = () => {calls++; return new Promise(resolve => {resolveRequest=resolve;});};
'''
    assertions = r'''
(async () => {
  // Home control has no panel ancestor: click must still reach fetch.
  home.click();
  assert.equal(calls,1);
  assert.equal(home.textContent,'Waking…');
  assert.equal(home['aria-busy'],'true');
  assert.equal(home.disabled,true);
  assert.equal($('power-toast').textContent,'Waking…');
  await setPowerMode('awake');
  assert.equal(calls,1, 'duplicate transition must be blocked');
  resolveRequest({ok:true,json:async()=>({runtime:{power_mode:'awake',motors_enabled:true}})});
  await new Promise(setImmediate);
  assert.equal(powerTransitionPending,false);
  assert.equal(home.textContent,'awake');
  assert.equal(home['aria-busy'],undefined);
  assert.equal($('power-toast').className,'power-toast ok');
  // A false success from the server must not be shown as Awake.
  fetch=async()=>({ok:true,json:async()=>({runtime:{power_mode:'standby',motors_enabled:false}})});
  await setPowerMode('awake');
  assert.equal($('power-toast').className,'power-toast error');
  assert.match($('power-toast').textContent,/not confirmed/);
  fetch=async()=>{throw new Error('network unavailable');};
  await setPowerMode('awake');
  assert.match($('power-toast').textContent,/network unavailable/);
  assert.equal(powerTransitionPending,false);
  assert.equal(home.disabled,false);
})().catch(e => {console.error(e); process.exitCode=1;});
'''
    subprocess.run(["node", "-e", harness + handler + assertions], check=True, timeout=10)
