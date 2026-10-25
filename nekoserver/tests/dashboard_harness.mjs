/* Runs functions of dashboard/app.js against a stand-in page. app.js is a plain page
   script, so a copy of it with one export line added is imported as a module (from a
   private temporary folder, removed afterwards). argv[2] is a JSON list of calls,
   [name, argument]; the answers are printed as a JSON list in the same order. */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const CALLS = new Set(['collectIssues', 'filesIntact', 'appState', 'validStatus', 'fixedFloor', 'raiseFixed']);
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
const answers = calls.map(([name, argument]) => {
  if (!CALLS.has(name)) throw new Error('Not a function the harness offers: ' + name);
  if (name === 'collectIssues') {
    return app.collectIssues(app.validStatus(argument)).map(i => ({ id: i.id, level: i.level, title: i.title }));
  }
  if (name === 'raiseFixed') {
    // A stand-in Speed slider: what raiseFixed leaves in it and in the draft.
    const input = { min: 20, value: argument.fixed }, shown = { textContent: '' };
    const field = { querySelector: which => (which === 'input' ? input : shown) };
    app.raiseFixed(field, argument);
    return { min: Number(input.min), value: Number(input.value), fixed: argument.fixed };
  }
  return app[name](argument);
});
process.stdout.write(JSON.stringify(answers));
