/* Runs functions of dashboard/app.js against a stand-in page. app.js is a plain page
   script, so a copy of it with one export line added is imported as a module (from a
   private temporary folder, removed afterwards). argv[2] is a JSON list of calls,
   [name, argument]; the answers are printed as a JSON list in the same order. */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const CALLS = new Set([
  'collectIssues',
  'filesIntact',
  'appState',
  'validStatus',
  'fixedFloor',
  'raiseFixed',
  'backupSummary',
  'recentCopies',
  'integrityResult',
  'nextRun',
  'shortText',
  'pinRefusal',
  'guideFor',
  'appsSummary',
  'shownApps',
  'releaseNotes',
  'arrayShown',
  'words',
  'waitingWhen',
  'companionMood',
  'panelMoods',
  'newlyFinished',
  'catFile',
  'appSpec',
  'panelAlerts',
  'jobLine',
  'gettingStarted',
  'companionWords',
  'upgradeAtWork',
  'assistantSetUp',
  'askItem',
  'shownAnswer',
  'bugReport',
  'copied',
  'askIntro',
  'ago',
  'outputsBadge',
  'sensorBadge',
  'temperatureName',
  'blockedNext',
  'mediaOutcome',
  'appMenu',
  'cardsOn',
  'cardsSummary',
  'cardLine',
  'driveOffer'
]);
/* Calls answered from another function's result, with more of it; the ask* and media*
   ones run a piece of the page on a page that keeps what is put on it (ASK_PAGE below). */
const DETAILED = new Set([
  'issueDetails',
  'askFlow',
  'approveFlow',
  'bugBoxCopy',
  'cutOffEntry',
  'askSignedOut',
  'askTyped',
  'mediaFlow',
  'mediaSignedOut',
  'askInTurn',
  'wordsIn'
]);
/* What those need of the page script besides the functions above. */
const ASK_PAGE = [
  'ASK',
  'state',
  'askNeko',
  'approveRepair',
  'askProgress',
  'askEntry',
  'bugBox',
  'sendQuestion',
  'askNote',
  // The Media folders page (mediaFlow, mediaSignedOut below).
  'MEDIA',
  'renderMedia'
];
const calls = JSON.parse(process.argv[2]);

/* Anything the page touches: every property is another stand-in, every call returns one. */
function stub() {
  const f = function () {
    return stub();
  };
  return new Proxy(f, {
    get(_, key) {
      if (key === Symbol.toPrimitive) return () => '';
      if (key === 'then') return undefined;
      if (['matches', 'hidden', 'open', 'isSecureContext'].includes(key)) return false;
      if (['hash', 'hostname', 'value', 'userAgent', 'textContent'].includes(key)) return '';
      if (key === 'length') return 0;
      if (key === Symbol.iterator)
        return function* nothing() {
          /* no elements */
        };
      return stub();
    },
    set() {
      return true;
    },
    apply() {
      return stub();
    },
    construct() {
      return stub();
    }
  });
}
/* What this browser keeps: nothing, except a theme a call chooses (wordsIn). */
const kept = {};
const storage = {
  getItem: key => kept[key] ?? null,
  setItem() {
    /* not kept */
  },
  removeItem() {
    /* not kept */
  }
};
Object.assign(globalThis, {
  document: stub(),
  window: stub(),
  localStorage: storage,
  sessionStorage: storage,
  location: { hostname: '127.0.0.1', hash: '' },
  matchMedia: () => ({
    matches: false,
    addEventListener() {
      /* no changes here */
    }
  }),
  setInterval: () => 0,
  clearInterval() {
    /* nothing runs */
  },
  requestAnimationFrame() {
    /* nothing is drawn */
  },
  fetch: async () => {
    throw new Error('offline');
  }
});
Object.defineProperty(globalThis, 'navigator', { value: stub(), configurable: true });

const source = readFileSync(new URL('../dashboard/app.js', import.meta.url), 'utf8');
const folder = mkdtempSync(join(tmpdir(), 'neko-app-'));
let app;
try {
  const copy = join(folder, 'app.mjs');
  writeFileSync(copy, source + '\nexport { ' + [...CALLS, ...ASK_PAGE].join(', ') + ' };\n', { mode: 0o600 });
  app = await import(pathToFileURL(copy).href);
} finally {
  rmSync(folder, { recursive: true, force: true });
}
/* Calls that take more than the plain argument, by name; every other offered function is
   called with the argument as it is. */
const summary = issue => ({ id: issue.id, level: issue.level, title: issue.title });
const SPECIAL = {
  collectIssues: argument => app.collectIssues(app.validStatus(argument)).map(summary),
  // The same list with what each item says and which guide it opens.
  issueDetails: argument =>
    app.collectIssues(app.validStatus(argument)).map(i => ({ ...summary(i), detail: i.detail, guide: i.guide })),
  backupSummary: argument => app.backupSummary(app.validStatus(argument)),
  jobLine: ([label, job]) => app.jobLine(label, job),
  upgradeAtWork: ([release, s, asked]) => app.upgradeAtWork(release, s, asked),
  gettingStarted: argument => app.gettingStarted(app.validStatus(argument)),
  companionWords: ([mood, s, issues]) => app.companionWords(mood, app.validStatus(s), issues),
  // The hero's words in a theme (DASHBOARD.md, "Themes"), kept as Settings keeps it.
  wordsIn: ([look, mood, s, issues]) => {
    kept['neko-look'] = JSON.stringify(look);
    try {
      return app.companionWords(mood, s === null ? null : app.validStatus(s), issues);
    } finally {
      delete kept['neko-look'];
    }
  },
  appsSummary: argument => app.appsSummary(app.validStatus(argument)),
  assistantSetUp: argument => app.assistantSetUp(app.validStatus(argument)),
  askIntro: argument => app.askIntro(argument.status && app.validStatus(argument.status), argument.control),
  // The cat: a reading in, its moods out (with the issues the page would list for it).
  companionMood: argument => {
    const s = argument.status && app.validStatus(argument.status);
    return app.companionMood(s, s ? app.collectIssues(s) : [], argument.stale, argument.done);
  },
  panelMoods: argument => {
    const s = app.validStatus(argument.status);
    return app.panelMoods(s, app.collectIssues(s), argument.now);
  },
  panelAlerts: argument => {
    const s = app.validStatus(argument);
    return app.panelAlerts(s, app.collectIssues(s));
  },
  newlyFinished: argument => argument.map(s => app.newlyFinished(app.validStatus(s))?.id ?? null),
  catFile: argument => app.catFile(argument.mood, argument.small),
  appSpec: argument => app.appSpec(argument.id, argument.app),
  // What an app's menu says it is (the label its card shows too).
  appMenu: argument => app.appMenu(argument).subtitle,
  pinRefusal: argument => app.pinRefusal(argument.answer, argument.now),
  // The Fans page's count, with its readings answered (this stand-in page reached none).
  outputsBadge: argument => {
    app.state.controlError = null;
    return app.outputsBadge(argument);
  },
  waitingWhen: argument => argument.waiting.map(w => app.waitingWhen(w, argument.waiting, argument.now)),
  releaseNotes: argument => app.releaseNotes(argument.app, argument.version),
  // The apps Home offers (this stand-in page is the server's own, 127.0.0.1).
  shownApps: argument => app.shownApps(app.validStatus(argument), true).map(a => a.id),
  raiseFixed: argument => {
    // A stand-in Speed slider: what raiseFixed leaves in it and in the draft.
    const input = { min: 20, value: argument.fixed },
      shown = { textContent: '' };
    const field = { querySelector: which => (which === 'input' ? input : shown) };
    app.raiseFixed(field, argument);
    return { min: Number(input.min), value: Number(input.value), fixed: argument.fixed };
  }
};
// "copied" in a browser whose Clipboard API "works", "refuses", or is not there at all.
SPECIAL.copied = argument => {
  const clipboards = {
    works: { writeText: async () => undefined },
    refuses: {
      writeText: async () => {
        throw new Error('not allowed');
      }
    }
  };
  const clipboard = clipboards[argument];
  Object.defineProperty(globalThis, 'navigator', { value: clipboard ? { clipboard } : {}, configurable: true });
  return app.copied('text');
};

/* ---- Ask Neko on a page that keeps what is put on it ----
   The four calls below run the page's own code from the press of a button to what the
   person then sees: the elements are plain objects that remember their text, their
   children and their click, the server is a function that answers each request, and
   the page's one-second wait between two looks at an answer does not wait. */
function keptPage() {
  const byId = new Map();
  const element = tag => {
    const node = {
      tag,
      className: '',
      textContent: '',
      value: '',
      hidden: false,
      selected: false,
      parentElement: null,
      children: [],
      clicked: null,
      append(...added) {
        for (const given of added) {
          // A string is a text node (longText puts one between its parts).
          const child = typeof given === 'string' ? { textContent: given, children: [] } : given;
          child.parentElement = node;
          node.children.push(child);
        }
      },
      replaceChildren(...added) {
        node.children = [];
        node.append(...added);
      },
      addEventListener(_, listener) {
        node.clicked = listener;
      },
      select() {
        node.selected = true;
      },
      showModal() {
        node.open = true; // the PIN dialog
      },
      setAttribute(name, value) {
        node[name] = value;
      },
      focus() {
        /* nothing to move */
      }
    };
    return node;
  };
  const byIdOf = id => {
    if (!byId.has(id)) byId.set(id, element('#' + id));
    return byId.get(id);
  };
  globalThis.document = { createElement: element, getElementById: byIdOf, body: element('body') };
  globalThis.setTimeout = (run, wait) => {
    if (wait <= 1000) void Promise.resolve().then(run); // the wait between two looks at an answer
    return 0; // the longer ones (a request's time limit, a message fading) never come
  };
  globalThis.clearTimeout = () => undefined;
  app.ASK.log.length = 0;
  app.ASK.busy = false;
  app.state.control = { access: { signed_in: true, pin_set: true } };
  return byIdOf;
}
/* Every word under an element, in the order the page shows it. */
function shown(node) {
  return [node.textContent, node.value, ...node.children.map(shown)].filter(Boolean).join('\n');
}
function find(node, wanted) {
  if (wanted(node)) return node;
  for (const child of node.children) {
    const found = find(child, wanted);
    if (found) return found;
  }
  return null;
}
const buttonNamed = (node, label) => find(node, n => n.tag === 'button' && n.textContent === label);
/* The server: `answer(op or path, body)` gives each reply; every request is noted. */
function server(answer) {
  const sent = [];
  globalThis.fetch = async (path, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    sent.push([path, body]);
    const reply = answer(path, body);
    return {
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => reply,
      text: async () => JSON.stringify(reply)
    };
  };
  return sent;
}
// A question from the press of Ask to the answer: what was sent, and what the page then
// keeps and shows of the question. argument: {typed, kept, answer}.
SPECIAL.askFlow = async argument => {
  const byId = keptPage();
  const sent = server(path => {
    if (path === '/api/control/ask') return { ok: true, id: 'abc', question: argument.kept, excerpt: '' };
    return { text: argument.answer, done: true, failed: null, repair: null };
  });
  await app.askNeko(argument.typed);
  const entry = app.ASK.log[0];
  return { sent, question: entry.question, shown: shown(byId('ask-log')), report: app.bugReport(entry) };
};
// Questions one after the other, each answered as `answers` says ({text} or {failed}):
// what each one sent to /api/control/ask. argument: {questions, answers}.
SPECIAL.askInTurn = async argument => {
  keptPage();
  let n = -1;
  const sent = server((path, body) => {
    if (path === '/api/control/ask') {
      n += 1;
      return { ok: true, id: 'a' + n, question: body.question, excerpt: '' };
    }
    const said = argument.answers[n];
    return { text: said.text ?? '', done: !said.failed, failed: said.failed ?? null, repair: null };
  });
  for (const question of argument.questions) {
    await app.askNeko(question);
  }
  return sent.filter(([path]) => path === '/api/control/ask').map(([, body]) => body);
};
// Approve on an update's card while `waiting` is the version that waits on the server:
// what each press sends, and the card after it. argument: {shown, waiting}.
SPECIAL.approveFlow = async argument => {
  const byId = keptPage();
  const sent = server((path, body) => {
    if (path === '/api/control/assist-check') {
      return { ok: true, id: body.action, does: 'Updates Jellyfin to 10.9.2 now.', warning: '', sig: argument.waiting };
    }
    if (body.sig === argument.waiting) return { ok: true, message: 'Jellyfin: updating now.' };
    return {
      ok: false,
      error: 'E42',
      message: 'A newer version arrived meanwhile, so nothing was installed',
      changed: true
    };
  });
  const entry = {
    question: 'Is there an update?',
    answer: 'Yes.',
    repair: {
      id: 'update_app:jellyfin',
      does: 'Updates Jellyfin to 10.9.1 now.',
      warning: '',
      sig: argument.shown,
      state: 'offered'
    }
  };
  app.ASK.log.push(entry);
  await app.approveRepair(entry);
  const first = {
    sent: sent.splice(0),
    card: shown(byId('ask-log')),
    state: entry.repair.state,
    sig: entry.repair.sig
  };
  // The card as it is now: its Approve button, pressed (when it still has one).
  const again = buttonNamed(byId('ask-log'), 'Approve');
  if (again) await again.clicked();
  return { first, second: { sent, card: shown(byId('ask-log')), state: entry.repair.state } };
};
// Copy on a bug report in a browser whose Clipboard API "works", "refuses" or is "missing".
SPECIAL.bugBoxCopy = async argument => {
  const byId = keptPage();
  const clipboards = {
    works: { writeText: async () => undefined },
    refuses: {
      writeText: async () => {
        throw new Error('not allowed');
      }
    }
  };
  const clipboard = clipboards[argument];
  Object.defineProperty(globalThis, 'navigator', { value: clipboard ? { clipboard } : {}, configurable: true });
  const box = app.bugBox('NekoServer x\nQuestion: why?');
  const area = find(box, n => n.tag === 'textarea');
  await buttonNamed(box, 'Copy').clicked();
  return { lines: shown(box), selected: area.selected, said: byId('toast').textContent };
};
// An answer that failed part-way, as the page gets it and shows it. argument: what
// /api/ask/<id> returned.
SPECIAL.cutOffEntry = async argument => {
  keptPage();
  const sent = server(() => ({ ok: true, does: 'Restarts Jellyfin.', warning: '', sig: '' }));
  const entry = { question: 'Why is it down?', answer: '' };
  app.ASK.log.push(entry);
  app.askProgress(entry, argument);
  await Promise.resolve();
  return { sent, repair: entry.repair ?? null, shown: shown(app.askEntry(entry)), report: app.bugReport(entry) };
};

// A question typed and sent (Ask pressed) on a device not signed in: what is left in the
// question box, whether the PIN dialog opened, and what was sent. argument: the question.
SPECIAL.askSignedOut = async argument => {
  const byId = keptPage();
  app.state.control = { access: { signed_in: false, pin_set: true } };
  const sent = server(() => ({ ok: false, error: 'E45', message: 'Too many wrong PINs' }));
  byId('ask-input').value = argument;
  app.sendQuestion();
  await Promise.resolve();
  return { box: byId('ask-input').value, dialog: byId('dialog').open === true, sent };
};

// A question typed (each key runs the box's input listener, askNote) and Ask pressed,
// signed in, on a server whose Neko reads `limit` characters: what the note under the
// box said while typing, what was sent, and the history.
// argument: {typed, limit}.
SPECIAL.askTyped = async argument => {
  const byId = keptPage();
  app.state.status = { assistant: { set_up: true, model: 'small', question_chars: argument.limit } };
  const sent = server(path => {
    if (path === '/api/control/ask') return { ok: true, id: 'abc', question: argument.typed.trim(), excerpt: '' };
    return { text: 'Because.', done: true, failed: null, repair: null };
  });
  byId('ask-input').value = argument.typed;
  app.askNote();
  const typing = byId('ask-note').textContent;
  app.sendQuestion();
  for (let i = 0; i < 20; i++) await Promise.resolve();
  return { typing, note: byId('ask-note').textContent, sent, shown: shown(byId('ask-log')) };
};

/* ---- the Media folders page ----
   A signed-in device opens the page: it asks for a look, follows the answer, and draws
   each kind with the form to add a folder; the form is then filled in and sent.
   argument: {result: what media-state answers as the last result, look: what media-change
   answers a look (accepted when not given), working: whether media-state says a change is
   at work, progress: how far the copy at work is, before: the answer the page showed already,
   add: {path, downloads, how}}. Returns what was sent, what the page then shows,
   and what the form sent. */
async function settle() {
  for (let i = 0; i < 30; i++) await Promise.resolve();
}
SPECIAL.mediaFlow = async argument => {
  const byId = keptPage();
  app.state.route = 'media';
  const before = argument.before ?? null; // what the page showed before (media-state's answer)
  Object.assign(app.MEDIA, { answer: before, asked: false, refused: null, formKey: '', browse: null });
  Object.assign(app.MEDIA.form, { kind: '', path: '', how: 'in_place', downloads: false, backup: true });
  const sent = server((path, body) => {
    if (path === '/api/control/media-state') {
      return { ok: true, working: Boolean(argument.working), result: argument.result, progress: argument.progress };
    }
    if (body?.change === 'look' && argument.look) return argument.look;
    return { ok: true, id: 'abc', message: 'Working on it. This page shows how it ends.' };
  });
  app.renderMedia();
  await settle();
  const looked = sent.splice(0);
  const page = shown(byId('media-folders'));
  const form = byId('media-add').children[0];
  const add = argument.add;
  let formSent = null;
  if (form && add) {
    const path = find(form, n => n.tag === 'input' && n.type === 'text');
    path.value = add.path;
    path.clicked(); // its input listener
    if (add.how === 'copy') {
      find(form, n => n.tag === 'input' && n.type === 'radio' && n.value === 'copy').clicked();
    }
    if (add.backup === false) {
      find(
        form,
        n => n.tag === 'input' && n.type === 'radio' && n.name === 'media-backup' && n.value === 'false'
      ).clicked();
    }
    const downloads = find(form, n => n.tag === 'input' && n.type === 'checkbox');
    downloads.checked = add.downloads;
    downloads.clicked();
    form.clicked({ preventDefault() {} });
    await settle();
    formSent = sent.filter(([p]) => p === '/api/control/media-change').map(([, body]) => body);
  }
  return { looked, page, form: form ? shown(form) : null, formSent };
};
// A device not signed in: the page shows how to unlock, and asks the server nothing.
SPECIAL.mediaSignedOut = async () => {
  const byId = keptPage();
  app.state.route = 'media';
  app.state.control = { access: { signed_in: false, pin_set: true } };
  Object.assign(app.MEDIA, { answer: null, asked: false, formKey: '' });
  const sent = server(() => ({ ok: true }));
  app.renderMedia();
  await settle();
  return { sent, page: shown(byId('media-folders')), form: byId('media-add').children.length };
};

/* One call after the other: the ones above each bring their own page and server. */
function called([name, argument]) {
  if (!CALLS.has(name) && !DETAILED.has(name)) throw new Error('Not a function the harness offers: ' + name);
  return (SPECIAL[name] ?? app[name])(argument);
}
const answers = await calls.reduce(
  (done, call) => done.then(async list => [...list, await called(call)]),
  Promise.resolve([])
);
process.stdout.write(JSON.stringify(answers));
