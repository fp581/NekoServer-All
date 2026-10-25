/* Runs setup-gui/setup.js against a stand-in page. The states to show come from
   argv[2] (a JSON list, one per poll); argv[3] "start" presses Start setup on the
   choose screen. What the page did is printed as JSON. */
import { setImmediate as settle } from 'node:timers/promises';

const states = JSON.parse(process.argv[2]);
const pressStart = process.argv[3] === 'start';
const posts = [];
const elements = {};

function element(id) {
  return {
    id, hidden: false, disabled: false, checked: false, value: '', textContent: '', className: '', style: {},
    dataset: {}, children: [], listeners: {}, type: '',
    addEventListener(kind, fn) {
      this.listeners[kind] ??= [];
      this.listeners[kind].push(fn);
    },
    append(...c) { this.children.push(...c); },
    replaceChildren(...c) { this.children = c; },
    setAttribute(k, v) { this[k] = v; },
    querySelectorAll() { return []; },
    focus() { /* nothing to focus in the stand-in page */ },
    scrollIntoView() { /* nothing to scroll in the stand-in page */ },
    fire(kind) {
      for (const fn of this.listeners[kind] ?? []) fn({ preventDefault() { /* no default action here */ } });
    },
  };
}

globalThis.document = {
  getElementById(id) {
    elements[id] ??= element(id);
    return elements[id];
  },
  createElement: (tag) => element(tag),
};
let poll = 0;
globalThis.fetch = async (route, options) => {
  if (options?.method === 'POST') {
    posts.push([route, JSON.parse(options.body)]);
    return { json: async () => ({ ok: true }) };
  }
  const state = states[Math.min(poll, states.length - 1)];
  poll += 1;
  return { ok: true, json: async () => state };
};
const waits = [];
globalThis.setTimeout = (fn) => { waits.push(fn); };

await import('../setup-gui/setup.js');
for (let i = 0; i < states.length; i++) {
  await settle();
  await settle();
  if (states[i].phase === 'choose' && pressStart) elements.start.fire('click');
  await settle();
  const next = waits.shift();
  if (next && i < states.length - 1) next();
}
await settle();
console.log(JSON.stringify({ posts, title: elements['finish-title']?.textContent ?? '' }));
