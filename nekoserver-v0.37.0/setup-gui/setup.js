/* NekoServer setup window: asks setup's own little server what is happening and draws it.
   The terminal stays the place where questions are answered. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const MARKS = { success: '✓', running: '›', needs_setup: '!', unavailable: '–', failed: '✕', blocked: '✕', pending: '·' };
  const WORDS = { success: 'done', running: 'running', needs_setup: 'needs you', unavailable: 'not on this PC',
                  failed: 'did not finish', blocked: 'waiting on another step', pending: '' };
  let chosen = {};
  let built = false;
  let misses = 0;

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') node.className = v; else if (k === 'text') node.textContent = v; else node.setAttribute(k, v);
    }
    for (const child of children) if (child != null) node.append(child);
    return node;
  }

  function buildChoices(extras) {
    const box = $('extras');
    box.replaceChildren();
    for (const extra of extras) {
      chosen[extra.id] = extra.on;
      const input = el('input', { type: 'checkbox', 'aria-label': extra.title });
      input.checked = extra.on;
      input.addEventListener('change', () => { chosen[extra.id] = input.checked; save({ [extra.id]: input.checked }); });
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

  async function save(changes) {
    try { await fetch('api/choose', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(changes) }); }
    catch (e) { /* the next poll shows what the server has */ }
  }

  $('start').addEventListener('click', async () => {
    $('start').disabled = true;
    $('start').textContent = 'Starting…';
    try { await fetch('api/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ choices: chosen }) }); }
    catch (e) { $('start').disabled = false; $('start').textContent = 'Start setup'; }
  });

  let shownCurrent = null;

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

  let askedSeq = null;     // the question the form shows (rebuilt only when it changes)
  let sentSeq = null;      // the question an answer was sent for

  async function answer(seq, text) {
    sentSeq = seq;
    $('ask-send').disabled = true;
    for (const b of $('ask-yesno').querySelectorAll('button')) b.disabled = true;
    $('ask-note').textContent = 'Sent. Setup goes on in a moment…';
    try {
      const r = await fetch('api/answer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ seq, text }) });
      const { ok } = await r.json();
      if (!ok) $('ask-note').textContent = 'That question was already answered in the terminal.';
    } catch (e) { $('ask-note').textContent = 'Could not reach setup; answer in the terminal.'; }
  }

  $('ask').addEventListener('submit', (e) => { e.preventDefault(); if (askedSeq != null) answer(askedSeq, $('ask-input').value); });
  for (const b of $('ask-yesno').querySelectorAll('button')) b.addEventListener('click', () => { if (askedSeq != null) answer(askedSeq, b.dataset.answer); });

  function showQuestion(q, current) {
    const form = $('ask');
    if (!q) { form.hidden = true; askedSeq = null; return; }
    form.hidden = false;
    if (q.seq === askedSeq) return;          // already on screen; keep what is being typed
    askedSeq = q.seq;
    $('ask-label').textContent = current ? 'A QUESTION · ' + current.title : 'A QUESTION';
    $('ask-context').textContent = q.context.join('\n');
    $('ask-context').hidden = !q.context.length;
    $('ask-context').scrollTop = $('ask-context').scrollHeight;   // the last lines matter most
    $('ask-prompt').textContent = q.secret ? 'Setup is asking for a hidden entry (a password or PIN). Type it here or in the terminal.' : q.text;
    const yesno = q.kind === 'yesno';
    $('ask-answer').hidden = yesno;
    $('ask-yesno').hidden = !yesno;
    const input = $('ask-input');
    input.type = q.secret ? 'password' : 'text';
    input.value = '';
    input.disabled = false;
    $('ask-send').disabled = false;
    for (const b of $('ask-yesno').querySelectorAll('button')) b.disabled = false;
    $('ask-note').textContent = q.secret ? 'Not shown in the terminal either.' : yesno ? '' : 'Enter alone sends an empty answer where setup allows one.';
    if (!yesno) input.focus();
  }

  function showNow(state) {
    const current = state.steps.find((s) => s.id === state.current);
    const waiting = state.steps.filter((s) => s.status === 'needs_setup');
    showQuestion(state.question, current);
    $('now').hidden = !!state.question;
    if (current) {
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
    } else {
      $('now-label').textContent = 'BETWEEN STEPS';
      $('now-title').textContent = 'Getting the next step ready…';
      $('now-text').textContent = waiting.length ? 'Still needs you: ' + waiting.map((s) => s.title).join(', ') : '';
      $('now-packages').replaceChildren();
      $('now-notes').replaceChildren();
    }
    stepList($('steps'), state.steps, state.current);
  }

  function render(state) {
    $('version').textContent = 'setup · v' + state.version;
    const phase = state.phase;
    for (const id of ['choose', 'running', 'finished', 'gone']) $(id).hidden = id !== phase;
    $('progress').hidden = phase === 'choose';
    const badge = $('phase-badge');
    if (phase === 'choose') { badge.textContent = 'Ready when you are'; badge.className = 'badge'; if (!built) buildChoices(state.extras); }
    if (phase === 'running') {
      const pct = state.total ? Math.round(100 * state.done / state.total) : 0;
      $('fill').style.width = pct + '%';
      $('count').textContent = state.done + ' of ' + state.total + ' steps done';
      badge.textContent = state.question ? 'Waiting for you' : 'Working…';
      badge.className = 'badge' + (state.question ? ' waiting' : '');
      showNow(state);
    }
    if (phase === 'finished') {
      $('fill').style.width = '100%';
      $('count').textContent = state.done + ' of ' + state.total + ' steps done';
      const left = state.steps.filter((s) => ['needs_setup', 'failed', 'blocked'].includes(s.status));
      $('finish-title').textContent = left.length ? 'NekoServer is almost ready' : 'NekoServer is ready';
      badge.textContent = left.length ? 'Almost ready' : 'Ready';
      badge.className = 'badge ' + (left.length ? 'waiting' : 'done');
      // The terminal's ruled lines and its own title are the card's title here.
      $('finish-lines').textContent = state.finish.filter((l) => !/^=+$/.test(l.trim()) && !/^\s*NekoServer is (ready|almost ready)!?$/.test(l)).join('\n').trim();
      stepList($('steps-final'), state.steps, null);
    }
    $('question').hidden = !state.question || phase !== 'running';
    if (phase !== 'running') showQuestion(null);
  }

  async function poll() {
    try {
      const response = await fetch('api/state', { cache: 'no-store' });
      if (!response.ok) throw new Error(response.status);
      misses = 0;
      render(await response.json());
    } catch (e) {
      misses += 1;
      if (misses >= 4) {   // setup has ended and its server with it
        for (const id of ['choose', 'running', 'finished']) $(id).hidden = true;
        $('gone').hidden = false;
        $('question').hidden = true;
        $('phase-badge').textContent = 'Setup ended';
        $('phase-badge').className = 'badge off';
      }
    }
    setTimeout(poll, misses ? 2000 : 1000);
  }
  poll();
})();
