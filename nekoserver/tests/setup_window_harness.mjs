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
    setAttribute(k, v) { this[k] = v; if (k === 'id') elements[v] = this; },
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
// {"refuseStart": "why"} in any state: Start setup is answered as the server's 409 is.
const refusal = states.find((state) => state.refuseStart)?.refuseStart;
let failures = states.find((state) => state.failAnswer)?.failAnswer ?? 0;
globalThis.fetch = async (route, options) => {
  if (options?.method === 'POST') {
    posts.push([route, JSON.parse(options.body)]);
    if (route === 'api/start' && refusal) return { json: async () => ({ ok: false, reason: refusal }) };
    // {"login": {...}} in any state: what Show login is answered with.
    if (route === 'api/login') return { json: async () => states.find((state) => state.login)?.login ?? { ok: false } };
    // {"failAnswer": 1} in any state: that many answers are not delivered (the network dropped).
    if (route === 'api/answer' && failures > 0) {
      failures -= 1;
      throw new TypeError('Failed to fetch');
    }
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
  // A page of fields (the backup times): {"fill": {"ask-page-daily": "23:30"}} types into
  // its rows and presses Save; {"skipPage": true} presses its Skip for now.
  if (states[i].fill) {
    for (const [id, value] of Object.entries(states[i].fill)) elements[id].value = value;
    elements.ask.fire('submit');
  }
  if (states[i].skipPage) elements['ask-page-skip'].fire('click');
  // A list to pick from: {"tick": [2, 5]} ticks those rows and presses its Send;
  // {"search": "rad"} types into its search box.
  if (states[i].search != null) {
    elements['ask-pick-search'].value = states[i].search;
    elements['ask-pick-search'].fire('input');
  }
  // {"tickRecommended": true} presses the list's "Tick the N recommended".
  if (states[i].tickRecommended) elements['ask-pick-recommended'].fire('click');
  if (states[i].tick) {
    for (const n of states[i].tick) elements['ask-pick'].children[n - 1].children[0].checked = true;
    elements['ask-pick'].fire('change');
    elements.ask.fire('submit');
  }
  // A sign-in step: {"showLogin": 1} presses the first card's Show login (twice: {"showLogin": [1, 1]}).
  for (const n of [states[i].showLogin ?? []].flat()) {
    elements['ask-page'].children[n - 1].children[3].fire('click');
    await settle();
    await settle();
  }
  // Steps that did not finish: {"retry": 1} presses the first card's Retry this step;
  // {"leave": true} the button under the cards.
  if (states[i].retry) {
    const card = elements['ask-page'].children[states[i].retry - 1];
    card.children[card.children.length - 1].fire('click');
  }
  if (states[i].leave) elements.ask.fire('submit');
  // The terminal's numbered lines as buttons: {"choose": 2} presses the second, then Send.
  if (states[i].choose) {
    elements['ask-options'].children[states[i].choose - 1].fire('click');
    elements.ask.fire('submit');
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
  marks: marks(elements['steps-final']), progressHidden: elements.progress?.hidden ?? null, cards,
  pageHidden: elements['ask-page']?.hidden ?? null, answerHidden: elements['ask-answer']?.hidden ?? null,
  rows: (elements['ask-page']?.children ?? []).map((row) => [row.children[0]?.textContent ?? '', (row.children[1]?.children ?? []).map((input) => input.value), row.children[2]?.textContent ?? '']),
  fieldTypes: (elements['ask-page']?.children ?? []).map((row) => (row.children[1]?.children ?? []).map((input) => input.type)),
  options: (elements['ask-options']?.children ?? []).map((button) => [button.children[0].textContent, button.children[1].textContent, button.className]),
  optionsHidden: elements['ask-options']?.hidden ?? null,
  pick: (elements['ask-pick']?.children ?? []).map((label) => [label.children[0].type, label.children[1].textContent, label.hidden]),
  pickCount: elements['ask-pick-count']?.textContent ?? '',
  askOff: ['ask-send', 'ask-page-send', 'ask-page-skip', 'ask-yes', 'ask-no'].filter((id) => elements[id]?.disabled),
  pickNotes: (elements['ask-pick']?.children ?? []).map((label) => label.children[2]?.textContent ?? ''),
  recommended: [elements['ask-pick-recommended']?.textContent ?? '', elements['ask-pick-recommended']?.hidden ?? null],
  advice: elements['ask-pick-advice']?.textContent ?? '',
  send: elements['ask-page-send']?.textContent ?? '', skipHidden: elements['ask-page-skip']?.hidden ?? null,
  placeholder: elements['ask-input']?.placeholder ?? '', yesnoHidden: elements['ask-yesno']?.hidden ?? null,
  retry: (elements['ask-page']?.children ?? []).filter((card) => card.className === 'retry').map((card) => card.children.map((part) => part.children.length ? [part.children[0].textContent, part.children[1].children.map((li) => li.textContent)] : part.textContent)),
  contextHidden: elements['ask-context']?.hidden ?? null,
  logins: (elements['ask-page']?.children ?? []).filter((card) => card.className === 'retry login').map((card) => ({ title: card.children[0].textContent, user: card.children[1].textContent,
    hidden: card.children[2].hidden, password: card.children[2].children[0]?.children[0]?.value ?? null, qr: card.children[2].children[1]?.src ?? null,
    button: card.children[3].textContent, note: card.children[4].textContent })),
  now: [elements['now-label']?.textContent ?? '', elements['now-title']?.textContent ?? '', elements['now-text']?.textContent ?? '', elements['now-text']?.className ?? ''],
  stepsHidden: elements.steps?.hidden ?? null, askLabel: elements['ask-label']?.textContent ?? '', running: elements.running?.className ?? '',
  prompt: elements['ask-prompt']?.textContent ?? '', note: elements['ask-note']?.textContent ?? '',
  start: [elements.start?.disabled ?? null, elements.start?.textContent ?? ''], chooseNote: elements['choose-note']?.textContent ?? '' }));
