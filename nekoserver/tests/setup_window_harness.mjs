/* Runs setup-gui/setup.js against a stand-in page. The states to show come from
   argv[2] (a JSON list, one per poll; {"fail": 403} answers that poll with that
   status, {"fail": "gone"} as a server that is no longer there); argv[3] "start"
   presses Start setup on the choose screen. What the page did is printed as JSON. */
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
  if (state.fail === 'gone') throw new TypeError('Failed to fetch');
  if (state.fail) return { ok: false, status: state.fail, json: async () => ({}) };
  return { ok: true, status: 200, json: async () => state };
};
const waits = [];
globalThis.setTimeout = (fn) => { waits.push(fn); };

await import('../setup-gui/setup.js');
// One poll after another, in order: each step waits for the page to settle, may press
// Start, then lets the page's next poll timer fire.
async function step(i) {
  await settle();
  await settle();
  if (states[i].phase === 'choose' && pressStart) elements.start.fire('click');
  if (states[i].phase === 'choose' && states[i].press) {
    for (const card of elements.extras.children) if (card.children[0].textContent === states[i].press) card.children[1].children[1].children[0].fire('change');
  }
  await settle();
  const next = waits.shift();
  if (next && i < states.length - 1) next();
}
await states.reduce((done, _state, i) => done.then(() => step(i)), Promise.resolve());
await settle();
const shown = ['choose', 'running', 'finished', 'gone', 'moved', 'finish-closed'].filter((id) => elements[id] && !elements[id].hidden);
const marks = (list) => (list?.children ?? []).map((li) => [li.className, li.children[0].textContent, li.children[1].children[0]]);
const cards = (elements.extras?.children ?? []).map((card) => [card.className, card.children[1].children[0].textContent]);
console.log(JSON.stringify({ posts, title: elements['finish-title']?.textContent ?? '', shown,
  badge: elements['phase-badge']?.textContent ?? '', label: elements['finish-label']?.textContent ?? '', lines: elements['finish-lines']?.textContent ?? '',
  marks: marks(elements['steps-final']), progressHidden: elements.progress?.hidden ?? null, cards }));
