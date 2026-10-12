const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
class Element {
  constructor() { this.hidden = false; this.disabled = false; this.textContent = ''; this.children = []; this.events = {}; this.open = false; }
  addEventListener(name, cb) { this.events[name] = cb; }
  appendChild(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
for (const id of ['update-notice','update-notice-text','update-open','update-later','update-check','update-preview',
  'update-current','update-status','update-history','update-dialog','update-notes','update-install','update-ack','update-close']) elements.set(id, new Element());
let calls = [], confirm = false;
const catalog = {checked_at:Date.now()/1000,current:{version:'0.4.0'}, supported:true, available:{version:'0.5.0',release_id:42},
  notes:[{version:'0.4.1',notes:'<script>never execute</script>'},{version:'0.5.0',notes:'latest'}],history:[],job:null};
let response = catalog;
let fail = false;
const window = {confirm:()=>confirm, fetch: async (url, options={}) => {
  calls.push([url, options]);
  if (fail) throw new Error('disconnected');
  return {ok:true,json:async()=>response};
}};
const context = vm.createContext({window, document:{getElementById:id=>elements.get(id),createElement:()=>new Element()},
  setInterval:()=>0, setTimeout:()=>0, clearTimeout:()=>{}, AbortController, Date, console});
vm.runInContext(fs.readFileSync('homebody/static/updates.js','utf8'), context);
const ui = window.HomebodyUpdates;
(async () => {
 await ui.refresh();
 assert.equal(elements.get('update-current').textContent, 'Installed Homebody 0.4.0');
 assert.equal(elements.get('update-notice').hidden, false);
 await elements.get('update-preview').events.click();
 assert.match(elements.get('update-notes').textContent, /<script>never execute<\/script>/);
 assert.equal(elements.get('update-dialog').open, true);
 await elements.get('update-install').events.click();
 assert.equal(calls.filter(c=>c[0].endsWith('/install')).length,0,'cancel must not install');
 confirm = true;
 response = {id:'a'.repeat(32),state:'queued'};
 await elements.get('update-install').events.click();
 assert.equal(calls.filter(c=>c[0].endsWith('/install')).length,1);
 assert.equal(JSON.parse(calls.find(c=>c[0].endsWith('/install'))[1].body).confirm,'INSTALL');
 assert.doesNotMatch(elements.get('update-status').textContent,/success|updated/i);
 fail = true;
 await ui.refresh();
 assert.equal(elements.get('update-install').disabled, true);
 assert.match(elements.get('update-status').textContent,/reconnect/i);
 fail = false;
 response = {...catalog,job:{state:'succeeded'},history:[{id:'b'.repeat(32),state:'succeeded',acknowledged:false,
  from_version:'0.4.0',manifest:{version:'0.5.0'},notes:catalog.notes}]};
 await ui.refresh();
 assert.equal(elements.get('update-dialog').open,true);
 assert.match(elements.get('update-notes').textContent,/0.4.1/);
 await elements.get('update-ack').events.click();
 assert.ok(calls.some(c=>c[0].endsWith('/acknowledge')));
 response = {...catalog,deferred_release_id:42};
 await ui.refresh();
 assert.equal(elements.get('update-notice').hidden,true);
 console.log('Update UI behavioral contracts passed');
})().catch(error=>{console.error(error);process.exit(1);});
