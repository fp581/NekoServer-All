/* NekoServer setup window: asks setup's own little server what is happening and draws it.
   Questions can be answered here or in the terminal. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const MARKS = { success: '✓', running: '›', needs_setup: '!', unavailable: '–', failed: '✕', blocked: '✕', pending: '·' };
  const WORDS = { success: 'done', running: 'running', needs_setup: 'needs you', unavailable: 'not on this PC',
                  failed: 'did not finish', blocked: 'waiting on another step', pending: '' };
  const HEADERS = { 'Content-Type': 'application/json' };
  let chosen = {};          // only the switches the person pressed: one left alone is asked about by setup
  let built = false;
  let misses = 0;
  let shownCurrent = null;   // the running step the list was last scrolled to
  let askedSeq = null;       // the question the form shows (rebuilt only when it changes)

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') node.className = v; else if (k === 'text') node.textContent = v; else node.setAttribute(k, v);
    }
    for (const child of children) if (child != null) node.append(child);
    return node;
  }

  async function post(route, payload) {
    const response = await fetch(route, { method: 'POST', headers: HEADERS, body: JSON.stringify(payload) });
    return response.json();
  }

  /* ---- before setup starts: the optional parts */

  async function save(changes) {
    try { await post('api/choose', changes); }
    catch { /* the next poll shows what the server has; the switch stays as pressed */ }
  }

  function buildChoices(extras) {
    const box = $('extras');
    box.replaceChildren();
    for (const extra of extras) {
      const input = el('input', { type: 'checkbox', 'aria-label': extra.title });
      input.checked = extra.on;
      input.addEventListener('change', () => { chosen[extra.id] = input.checked; void save({ [extra.id]: input.checked }); });
      const installs = el('div', { class: 'installs' });
      for (const p of extra.installs) installs.append(el('span', { class: 'chip', title: p.explanation }, el('b', { text: p.name }), p.explanation ? ' · ' + p.explanation : ''));
      box.append(el('div', { class: 'extra' },
        el('h3', { text: extra.title }),
        el('label', { class: 'switch' }, input, el('span')),
        el('p', { text: extra.explanation }),
        extra.installs.length ? installs : null));
    }
    built = true;
  }

  async function start() {
    $('start').disabled = true;
    $('start').textContent = 'Starting…';
    try { await post('api/start', { choices: chosen }); }
    catch { $('start').disabled = false; $('start').textContent = 'Start setup'; }
  }
  $('start').addEventListener('click', () => { void start(); });

  /* ---- a question, answered here or in the terminal */

  function askButtons() { return $('ask-yesno').querySelectorAll('button'); }

  async function answer(seq, text) {
    $('ask-send').disabled = true;
    for (const b of askButtons()) b.disabled = true;
    $('ask-note').textContent = 'Sent. Setup goes on in a moment…';
    try {
      const { ok } = await post('api/answer', { seq, text });
      if (!ok) $('ask-note').textContent = 'That question was already answered in the terminal.';
    } catch {
      $('ask-note').textContent = 'Could not reach setup; answer in the terminal.';
    }
  }

  $('ask').addEventListener('submit', (e) => { e.preventDefault(); if (askedSeq != null) void answer(askedSeq, $('ask-input').value); });
  for (const b of askButtons()) b.addEventListener('click', () => { if (askedSeq != null) void answer(askedSeq, b.dataset.answer); });

  function askNote(q, yesno) {
    if (q.secret) return 'Not shown in the terminal either.';
    if (yesno) return '';
    return 'Enter alone sends an empty answer where setup allows one.';
  }

  function showQuestion(q, current) {
    const form = $('ask');
    if (!q) { form.hidden = true; askedSeq = null; return; }
    form.hidden = false;
    if (q.seq === askedSeq) return;          // already on screen; keep what is being typed
    askedSeq = q.seq;
    $('ask-label').textContent = current ? 'A QUESTION · ' + current.title : 'A QUESTION';
    const context = $('ask-context');
    context.textContent = q.context.join('\n');
    context.hidden = !q.context.length;
    context.scrollTop = context.scrollHeight;   // the last lines matter most
    $('ask-prompt').textContent = q.secret ? 'Setup is asking for a hidden entry (a password or PIN). Type it here or in the terminal.' : q.text;
    const yesno = q.kind === 'yesno';
    $('ask-answer').hidden = yesno;
    $('ask-yesno').hidden = !yesno;
    const input = $('ask-input');
    input.type = q.secret ? 'password' : 'text';
    input.value = '';
    input.disabled = false;
    $('ask-send').disabled = false;
    for (const b of askButtons()) b.disabled = false;
    $('ask-note').textContent = askNote(q, yesno);
    if (!yesno) input.focus();
  }

  /* ---- while it runs */

  function stepList(target, steps, current) {
    target.replaceChildren();
    for (const s of steps) {
      const word = s.status in WORDS ? WORDS[s.status] : s.status;
      const item = el('li', { class: s.status },
        el('span', { class: 'mark', 'aria-hidden': 'true', text: MARKS[s.status] || '·' }),
        el('div', {}, s.title + (word ? ' — ' + word : ''),
          s.reason && s.status !== 'success' ? el('small', { text: s.reason }) : null));
      if (s.status === 'running') item.setAttribute('aria-current', 'step');
      target.append(item);
      // Keep the step under way in view as the list moves on (not on every poll).
      if (s.status === 'running' && current !== shownCurrent) { item.scrollIntoView({ block: 'center' }); shownCurrent = current; }
    }
  }

  function showCurrent(state, current) {
    $('now-label').textContent = 'Step ' + (state.steps.indexOf(current) + 1) + ' of ' + state.total;
    $('now-title').textContent = current.title;
    $('now-text').textContent = current.explanation;
    const box = $('now-packages');
    box.replaceChildren();
    if (current.packages.length) {
      box.append(el('div', { class: 'eyebrow', text: 'Installing' }));
      for (const p of current.packages) box.append(el('span', { class: 'package' }, el('b', { text: p.name }), p.explanation ? ' — ' + p.explanation : ''));
    }
    $('now-notes').replaceChildren(...current.notes.map((n) => el('li', { text: n })));
  }

  function showBetween(state) {
    const waiting = state.steps.filter((s) => s.status === 'needs_setup');
    $('now-label').textContent = 'BETWEEN STEPS';
    $('now-title').textContent = 'Getting the next step ready…';
    $('now-text').textContent = waiting.length ? 'Still needs you: ' + waiting.map((s) => s.title).join(', ') : '';
    $('now-packages').replaceChildren();
    $('now-notes').replaceChildren();
  }

  function progress(state) {
    const pct = state.total ? Math.round(100 * state.done / state.total) : 0;
    $('fill').style.width = pct + '%';
    $('count').textContent = state.done + ' of ' + state.total + ' steps done';
  }

  function badge(text, kind) {
    const node = $('phase-badge');
    node.textContent = text;
    node.className = 'badge' + (kind ? ' ' + kind : '');
  }

  function renderChoose(state) {
    badge('Ready when you are', '');
    if (!built) buildChoices(state.extras);
  }

  function renderRunning(state) {
    progress(state);
    const waiting = state.question || state.terminal;
    badge(waiting ? 'Waiting for you' : 'Working…', waiting ? 'waiting' : '');
    const current = state.steps.find((s) => s.id === state.current);
    showQuestion(state.question, current);
    $('now').hidden = !!state.question;
    if (current) showCurrent(state, current); else showBetween(state);
    stepList($('steps'), state.steps, state.current);
  }

  function renderFinished(state) {
    progress(state);
    // Setup's own verdict (the first checkup included), as the terminal shows it.
    const ready = state.complete === true;
    $('finish-title').textContent = ready ? 'NekoServer is ready' : 'NekoServer is almost ready';
    badge(ready ? 'Ready' : 'Almost ready', ready ? 'done' : 'waiting');
    // The terminal's ruled lines and its own title are the card's title here.
    $('finish-lines').textContent = state.finish.filter((l) => !/^=+$/.test(l.trim()) && !/^\s*NekoServer is (ready|almost ready)!?$/.test(l)).join('\n').trim();
    stepList($('steps-final'), state.steps, null);
  }

  function render(state) {
    $('version').textContent = 'setup · v' + state.version;
    const phase = state.phase;
    for (const id of ['choose', 'running', 'finished', 'gone']) $(id).hidden = id !== phase;
    $('progress').hidden = phase === 'choose';
    if (phase === 'choose') renderChoose(state);
    if (phase === 'running') renderRunning(state);
    if (phase === 'finished') renderFinished(state);
    $('question').hidden = !state.question || phase !== 'running';
    // A program of its own asks in the terminal only (its questions cannot come here).
    $('terminal').hidden = !state.terminal || !!state.question || phase !== 'running';
    $('terminal-what').textContent = state.terminal ? state.terminal + ': its questions are asked there only.' : '';
    if (phase !== 'running') showQuestion(null);
  }

  function showGone() {   // setup has ended and its server with it
    for (const id of ['choose', 'running', 'finished']) $(id).hidden = true;
    $('gone').hidden = false;
    $('question').hidden = true;
    $('terminal').hidden = true;
    badge('Setup ended', 'off');
  }

  async function poll() {
    try {
      const response = await fetch('api/state', { cache: 'no-store' });
      if (!response.ok) throw new Error(response.status);
      misses = 0;
      render(await response.json());
    } catch {
      // Setup's server answers until setup ends; a few misses in a row mean it is gone.
      misses += 1;
      if (misses >= 4) showGone();
    }
    setTimeout(() => { void poll(); }, misses ? 2000 : 1000);
  }
  void poll();
})();
