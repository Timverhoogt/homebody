const assert = require('node:assert/strict');
const nodes = [];
const elements = new Map();
const listeners = new Map();
let now = 0;
let timerId = 0;
const timers = new Map();
function element(id = '', power) {
  const node = {
    id, hidden: false, dataset: power ? {power} : {}, disabled: false,
    children: [], ownText: power || '', parent: null,
    classList: {add(){}, remove(){}},
    setAttribute(k,v){this[k]=v;}, removeAttribute(k){delete this[k];},
    closest(){return null;}, addEventListener(event, fn){this[event]=fn;},
    append(...children){for (const child of children) {child.parent=this;this.children.push(child);}},
    appendChild(child){child.remove?.(); this.append(child);},
    remove(){if(this.parent) this.parent.children=this.parent.children.filter(n=>n!==this);this.parent=null;},
    contains(child){return child===this || this.children.some(n=>n.contains?.(child));},
    focus(){document.activeElement=this;this.parent?.focusin?.({});},
    get textContent(){return this.ownText + this.children.map(child => child.textContent).join('');},
    set textContent(value){this.ownText=value;this.children=[];},
  };
  nodes.push(node);
  if(id) elements.set(id,node);
  return node;
}
const document = {
  hidden: false, activeElement: null, fullscreenElement: null,
  body: element('body'),
  addEventListener(event,fn){listeners.set(event,fn);},
  createElement(){return element();},
  getElementById(id){return elements.get(id);},
  querySelectorAll(){return nodes.filter(n=>n.dataset.power);},
};
const window = {addEventListener(event,fn){listeners.set(event,fn);}};
const performance = {now:()=>now};
const $ = id => elements.get(id);
function setTimeout(callback, delay){timers.set(++timerId, {callback, at: now+delay, delay});return timerId;}
function clearTimeout(id){timers.delete(id);}
function advance(ms){
  const until=now+ms;
  while(true){
    const next=[...timers].filter(([,t])=>t.at<=until).sort((a,b)=>a[1].at-b[1].at)[0];
    if(!next)break;
    now=next[1].at;timers.delete(next[0]);next[1].callback();
  }
  now=until;
}
function notices(){return document.body.children.find(n=>n.id==='notifications')?.children.filter(n=>n.className.startsWith('notification ')) || [];}
function notice(id){return notices().find(n=>n.children[0].textContent.includes(id));}
