/* Runs setup-gui/setup.js against a stand-in page: the states to show come from
   argv[3] (a JSON list, one per poll), and what the page did is printed as JSON. */
'use strict';
const fs = require('fs');
const states = JSON.parse(process.argv[3]);
const posts = [];
const elements = {};
function element(id) {
  const node = {
    id, hidden: false, disabled: false, checked: false, value: '', textContent: '', className: '', style: {},
    dataset: {}, children: [], listeners: {}, type: '',
    addEventListener(kind, fn) { (this.listeners[kind] = this.listeners[kind] || []).push(fn); },
    append(...c) { this.children.push(...c); }, replaceChildren(...c) { this.children = c; },
    setAttribute(k, v) { this[k] = v; }, querySelectorAll() { return []; }, focus() {}, scrollIntoView() {},
    fire(kind) { for (const fn of this.listeners[kind] || []) fn({ preventDefault() {} }); },
  };
  return node;
}
global.document = {
  getElementById: (id) => (elements[id] = elements[id] || element(id)),
  createElement: (tag) => element(tag),
};
let poll = 0;
global.fetch = async (route, options) => {
  if (options && options.method === 'POST') { posts.push([route, JSON.parse(options.body)]); return { json: async () => ({ ok: true }) }; }
  const state = states[Math.min(poll++, states.length - 1)];
  return { ok: true, json: async () => state };
};
const waits = [];
global.setTimeout = (fn) => { waits.push(fn); };
eval(fs.readFileSync(process.argv[2], 'utf8'));
(async () => {
  const settle = () => new Promise((r) => setImmediate(r));
  for (let i = 0; i < states.length; i++) {
    await settle(); await settle();
    if (states[i].phase === 'choose' && process.argv[4] === 'start') elements.start.fire('click');
    await settle();
    const next = waits.shift(); if (next && i < states.length - 1) next();
  }
  await settle();
  const title = elements['finish-title'] ? elements['finish-title'].textContent : '';
  console.log(JSON.stringify({ posts, title }));
})();
