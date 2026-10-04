"""Deterministic notification service behavior; no browser or robot required."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "scenario",
    [
        "queue",
        "deduplicate",
        "pause",
        "visibility",
        "focus",
        "privacy",
        "capacity",
        "safe_text",
        "fullscreen",
    ],
)
def test_notification_service(scenario):
    if not shutil.which("node"):
        pytest.skip("Node is required")
    scripts = {
        "queue": r"""
for(let i=0;i<4;i++) N.show(`message-${i}`,{id:`id-${i}`,kind:'ok'});
assert.equal(notices().length,4);assert.equal(notices().filter(n=>!n.hidden).length,3);
advance(6000);assert.equal(notices().length,1);assert.equal(notices()[0].hidden,false);
advance(5999);assert.equal(notices().length,1);advance(1);assert.equal(notices().length,0);
N.show('Loading',{id:'operation',kind:'pending'});const node=notices()[0];
N.show('Finished',{id:'operation',kind:'ok'});assert.equal(notices()[0],node);
advance(6000);assert.equal(notices().length,0);
""",
        "deduplicate": r"""
N.show('Saved',{id:'settings',kind:'ok'});advance(3000);
N.show('Saved',{id:'settings',kind:'ok'});assert.equal(notices().length,1);
advance(3000);assert.equal(notices().length,0);
N.show('Error',{kind:'error'});N.show('Error',{kind:'error'});assert.equal(notices().length,1);
""",
        "pause": r"""
N.show('Saved',{kind:'ok'});const node=notices()[0];advance(2000);
node.pointerenter();advance(10000);assert.equal(notices().length,1);
node.pointerleave();advance(3999);assert.equal(notices().length,1);
advance(1);assert.equal(notices().length,0);
""",
        "visibility": r"""
N.show('Saved',{kind:'ok'});advance(1000);
document.hidden=true;listeners.get('visibilitychange')();advance(10000);
assert.equal(notices().length,1);
document.hidden=false;listeners.get('visibilitychange')();advance(4999);
assert.equal(notices().length,1);advance(1);assert.equal(notices().length,0);
""",
        "focus": r"""
N.show('First',{kind:'error'});N.show('Second',{kind:'error'});
const first=notices()[0];first.children[1].focus();first.children[1].click();
assert.equal(document.activeElement,notices()[0].children[1]);
N.clear();N.show('Saved',{kind:'ok'});const node=notices()[0];node.focusin();
advance(10000);assert.equal(notices().length,1);
node.focusout({relatedTarget:document.body});advance(6000);assert.equal(notices().length,0);
""",
        "privacy": r"""
N.show('Private',{kind:'error'});N.setSuppressed('owner',true);
assert.equal(notices().length,0);assert.equal(N.show('Late private result'),null);
N.setSuppressed('kids',true);N.setSuppressed('owner',false);
assert.equal(N.show('Still locked'),null);N.setSuppressed('kids',false);
N.show('Visible');assert.equal(notices().length,1);
listeners.get('pagehide')();assert.equal(notices().length,0);
""",
        "capacity": r"""
for(let i=0;i<12;i++)N.show(`Error ${i}`,{kind:'error'});
assert.equal(N.show('overflow',{kind:'error'}),null);assert.equal(notices().length,12);
N.clear();N.show('Saved',{kind:'ok'});
for(let i=0;i<11;i++)N.show(`Error ${i}`,{kind:'error'});
N.show('New critical',{kind:'error'});assert.equal(notices().length,12);
assert.equal(notice('Saved'),undefined);assert.ok(notice('New critical'));
""",
        "safe_text": r"""
N.show('<img src=x onerror=alert(1)>',{kind:'error'});
assert.equal(notices()[0].children[0].textContent,'<img src=x onerror=alert(1)>');
assert.equal(notices()[0].children[0].children.length,0);
assert.equal(document.body.children.find(n=>n.role==='alert').textContent,'<img src=x onerror=alert(1)>');
assert.equal(notices()[0].children[1]['aria-label'],'Dismiss notification');
""",
        "fullscreen": r"""
N.show('Camera error',{kind:'error'});const container=document.body.children.find(n=>n.id==='notifications');
document.fullscreenElement=element();listeners.get('fullscreenchange')();
assert.equal(container.parent,document.fullscreenElement);
document.fullscreenElement=null;listeners.get('fullscreenchange')();assert.equal(container.parent,document.body);
""",
    }
    harness = Path("tests/fixtures/notifications_dom.cjs").read_text()
    service = Path("homebody/static/notifications.js").read_text()
    subprocess.run(
        ["node", "-e", harness + service + "\nconst N=window.HomebodyNotifications;\n" + scripts[scenario]],
        check=True,
        timeout=10,
    )
