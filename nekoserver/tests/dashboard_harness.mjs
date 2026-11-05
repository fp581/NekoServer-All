/* Runs functions of dashboard/app.js against a stand-in page. app.js is a plain page
   script, so a copy of it with one export line added is imported as a module (from a
   private temporary folder, removed afterwards). argv[2] is a JSON list of calls,
   [name, argument]; the answers are printed as a JSON list in the same order. */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const CALLS = new Set(['collectIssues', 'filesIntact', 'appState', 'validStatus', 'fixedFloor', 'raiseFixed',
  'backupSummary', 'recentCopies', 'integrityResult', 'nextRun', 'shortText', 'pinRefusal', 'guideFor', 'appsSummary',
  'shownApps', 'releaseNotes', 'arrayShown', 'words', 'waitingWhen', 'companionMood', 'panelMoods', 'newlyFinished',
  'catFile', 'appSpec']);
/* Calls answered from another function's result, with more of it. */
const DETAILED = new Set(['issueDetails']);
const calls = JSON.parse(process.argv[2]);

/* Anything the page touches: every property is another stand-in, every call returns one. */
function stub() {
  const f = function () { return stub(); };
  return new Proxy(f, {
    get(_, key) {
      if (key === Symbol.toPrimitive) return () => '';
      if (key === 'then') return undefined;
      if (['matches', 'hidden', 'open', 'isSecureContext'].includes(key)) return false;
      if (['hash', 'hostname', 'value', 'userAgent', 'textContent'].includes(key)) return '';
      if (key === 'length') return 0;
      if (key === Symbol.iterator) return function* nothing() { /* no elements */ };
      return stub();
    },
    set() { return true; },
    apply() { return stub(); },
    construct() { return stub(); },
  });
}
const storage = { getItem: () => null, setItem() { /* not kept */ }, removeItem() { /* not kept */ } };
Object.assign(globalThis, {
  document: stub(), window: stub(), localStorage: storage, sessionStorage: storage,
  location: { hostname: '127.0.0.1', hash: '' },
  matchMedia: () => ({ matches: false, addEventListener() { /* no changes here */ } }),
  setInterval: () => 0, clearInterval() { /* nothing runs */ },
  requestAnimationFrame() { /* nothing is drawn */ },
  fetch: async () => { throw new Error('offline'); },
});
Object.defineProperty(globalThis, 'navigator', { value: stub(), configurable: true });

const source = readFileSync(new URL('../dashboard/app.js', import.meta.url), 'utf8');
const folder = mkdtempSync(join(tmpdir(), 'neko-app-'));
let app;
try {
  const copy = join(folder, 'app.mjs');
  writeFileSync(copy, source + '\nexport { ' + [...CALLS].join(', ') + ' };\n', { mode: 0o600 });
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
  appsSummary: argument => app.appsSummary(app.validStatus(argument)),
  // The cat: a reading in, its moods out (with the issues the page would list for it).
  companionMood: argument => {
    const s = argument.status && app.validStatus(argument.status);
    return app.companionMood(s, s ? app.collectIssues(s) : [], argument.stale, argument.done);
  },
  panelMoods: argument => {
    const s = app.validStatus(argument.status);
    return app.panelMoods(s, app.collectIssues(s), argument.now);
  },
  newlyFinished: argument => argument.map(s => app.newlyFinished(app.validStatus(s))?.id ?? null),
  catFile: argument => app.catFile(argument.mood, argument.small),
  appSpec: argument => app.appSpec(argument.id, argument.app),
  pinRefusal: argument => app.pinRefusal(argument.answer, argument.now),
  waitingWhen: argument => argument.waiting.map(w => app.waitingWhen(w, argument.waiting, argument.now)),
  releaseNotes: argument => app.releaseNotes(argument.app, argument.version),
  // The apps Home offers (this stand-in page is the server's own, 127.0.0.1).
  shownApps: argument => app.shownApps(app.validStatus(argument), true).map(a => a.id),
  raiseFixed: argument => {
    // A stand-in Speed slider: what raiseFixed leaves in it and in the draft.
    const input = { min: 20, value: argument.fixed }, shown = { textContent: '' };
    const field = { querySelector: which => (which === 'input' ? input : shown) };
    app.raiseFixed(field, argument);
    return { min: Number(input.min), value: Number(input.value), fixed: argument.fixed };
  },
};
const answers = calls.map(([name, argument]) => {
  if (!CALLS.has(name) && !DETAILED.has(name)) throw new Error('Not a function the harness offers: ' + name);
  return (SPECIAL[name] ?? app[name])(argument);
});
process.stdout.write(JSON.stringify(answers));
