/* NekoServer setup window: asks setup's own little server what is happening and draws it.
   Questions can be answered here or in the terminal. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  // A step waiting for another one has not failed: it runs once that one is done.
  const MARKS = {
    success: '✓',
    running: '›',
    needs_setup: '!',
    unavailable: '–',
    failed: '✕',
    blocked: '…',
    pending: '·'
  };
  const WORDS = {
    success: 'done',
    running: 'running',
    needs_setup: 'needs you',
    unavailable: 'not on this PC',
    failed: 'did not finish',
    blocked: 'waiting',
    pending: ''
  };
  const HEADERS = { 'Content-Type': 'application/json' };
  let chosen = {}; // only the switches the person pressed: one left alone is asked about by setup
  let built = false;
  let misses = 0;
  let shownCurrent = null; // the running step the list was last scrolled to
  let askedSeq = null; // the question the form shows (rebuilt only when it changes)
  let askedQuestion = null; // that question itself (its page, when it is on one)
  let pageInputs = {}; // the fields of the page shown, by key
  let finishedShown = false; // the finishing screen is up: it stays, also once setup has closed

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else node.setAttribute(k, v);
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
    try {
      const answer = await post('api/choose', changes);
      $('choose-note').textContent = answer.ok === false && answer.reason ? answer.reason : '';
    } catch {
      /* the next poll shows what the server has; the switch stays as pressed */
    }
  }

  function buildChoices(extras) {
    const box = $('extras');
    box.replaceChildren();
    for (const extra of extras) {
      const input = el('input', { type: 'checkbox', 'aria-label': extra.title });
      input.checked = extra.on;
      // A switch setup asks about later, as long as it is left alone: shown as undecided.
      const caption = el('span', { class: 'caption', text: extra.asked ? 'asked later' : '' });
      const card = el(
        'div',
        { class: extra.asked ? 'extra undecided' : 'extra' },
        el('h3', { text: extra.title }),
        el('div', { class: 'choice' }, caption, el('label', { class: 'switch' }, input, el('span'))),
        el('p', { text: extra.explanation })
      );
      input.addEventListener('change', () => {
        chosen[extra.id] = input.checked;
        card.className = 'extra';
        caption.textContent = input.checked ? 'yes' : 'no';
        void save({ [extra.id]: input.checked });
      });
      const installs = el('div', { class: 'installs' });
      for (const p of extra.installs)
        installs.append(
          el(
            'span',
            { class: 'chip', title: p.explanation },
            el('b', { text: p.name }),
            p.explanation ? ' · ' + p.explanation : ''
          )
        );
      if (extra.installs.length) card.append(installs);
      box.append(card);
    }
    built = true;
  }

  async function start() {
    $('start').disabled = true;
    $('start').textContent = 'Starting…';
    try {
      await post('api/start', { choices: chosen });
    } catch {
      $('start').disabled = false;
      $('start').textContent = 'Start setup';
    }
  }
  $('start').addEventListener('click', () => {
    void start();
  });

  /* ---- a question, answered here or in the terminal */

  function askButtons() {
    return $('ask-yesno').querySelectorAll('button');
  }

  /* `fields`: the other answers of a page (the backup times), sent with the one asked. */
  async function answer(seq, text, skip = false, fields = null) {
    for (const id of ['ask-send', 'ask-page-send', 'ask-page-skip']) $(id).disabled = true;
    for (const b of askButtons()) b.disabled = true;
    for (const input of Object.values(pageInputs)) input.disabled = true;
    $('ask-note').textContent = skip ? 'Left for now. Setup goes on in a moment…' : 'Sent. Setup goes on in a moment…';
    try {
      let body = { seq, text };
      if (skip) body = { seq, text: '', skip: true };
      else if (fields) body = { seq, text, fields };
      const { ok } = await post('api/answer', body);
      if (!ok) $('ask-note').textContent = 'That question was already answered in the terminal.';
    } catch {
      $('ask-note').textContent = 'Could not reach setup; answer in the terminal.';
    }
  }

  $('ask').addEventListener('submit', e => {
    e.preventDefault();
    if (askedSeq == null) return;
    if (askedQuestion && pageOf(askedQuestion)) void answerPage(askedQuestion);
    else void answer(askedSeq, $('ask-input').value);
  });
  for (const b of askButtons())
    b.addEventListener('click', () => {
      if (askedSeq != null) void answer(askedSeq, b.dataset.answer, b.id === 'ask-skip');
    });
  $('ask-page-skip').addEventListener('click', () => {
    if (askedSeq != null) void answer(askedSeq, '', true);
  });

  /* ---- related questions on one page: when the backups run, three rows */

  function pageOf(q) {
    return q.page && q.page.form === 'backup_times' ? q.page : null;
  }

  function nightEnd(page) {
    return String(page.night_end ?? 6).padStart(2, '0') + ':00';
  }

  function timeInput(key, value, label) {
    const input = el('input', { type: 'time', id: 'ask-page-' + key, step: '60', required: '', 'aria-label': label });
    input.value = value;
    return input;
  }

  function row(title, inputs, note) {
    return el(
      'div',
      { class: 'row' },
      el('span', { class: 'row-title', text: title }),
      el('span', { class: 'row-inputs' }, ...inputs),
      el('small', { text: note })
    );
  }

  /* Each row says what runs at its time (the same words setup says in the terminal). */
  function buildBackupTimes(page) {
    const f = page.fields;
    const day = el('select', { id: 'ask-page-weekly_day', 'aria-label': 'Day of the weekly USB copy' });
    for (const name of page.weekdays || []) day.append(el('option', { value: name, text: name }));
    day.value = f.weekly_day;
    pageInputs = {
      daily: timeInput('daily', f.daily, 'Time of the daily backups'),
      weekly_day: day,
      weekly: timeInput('weekly', f.weekly, 'Time of the weekly USB copy'),
      monthly: timeInput('monthly', f.monthly, 'Time of the monthly check')
    };
    $('ask-page').replaceChildren(
      row(
        'Daily backups',
        [pageInputs.daily],
        "The media apps' snapshot at this time, the music copy 30 minutes before it and the copy of this PC's own settings an hour after it."
      ),
      row(
        'Weekly USB copy',
        [day, pageInputs.weekly],
        "Your photos and Nextcloud files onto the USB drive, with Immich's database copy 45 minutes before it."
      ),
      row(
        'Monthly check and updates',
        [pageInputs.monthly],
        'On the 1st: every backup copy checked, a snapshot of every app and a restore test; the weekly app updates start at this time too. It must start at night, before ' +
          nightEnd(page) +
          '.'
      )
    );
  }

  function pageValues() {
    return Object.fromEntries(Object.entries(pageInputs).map(([key, input]) => [key, input.value]));
  }

  function pageProblem(page, values) {
    for (const key of ['daily', 'weekly', 'monthly'])
      if (!/^\d\d:\d\d$/.test(values[key])) return 'Every time needs hours and minutes, as HH:MM.';
    if (Number(values.monthly.slice(0, 2)) >= (page.night_end ?? 6))
      return 'The monthly check must start at night, before ' + nightEnd(page) + '.';
    return '';
  }

  async function answerPage(q) {
    const page = pageOf(q);
    const values = pageValues();
    const problem = pageProblem(page, values);
    if (problem) {
      $('ask-note').textContent = problem;
      return;
    }
    // The terminal's prompt stands for one field: its answer is that value, the rest ride along.
    await answer(q.seq, values[page.asking] ?? '', false, values);
  }

  /* A step you confirm yourself (the phone, a TV test) can be left for later: the
     window's "Skip for now" does what Ctrl+D in the terminal does. */
  const CONFIRM_STEP = 'Have you completed this step successfully?';
  function canSkip(q) {
    return q.kind === 'yesno' && q.text.startsWith(CONFIRM_STEP);
  }

  function askNote(q, yesno) {
    if (q.secret) return 'Not shown in the terminal either.';
    if (yesno) return '';
    return 'Enter alone sends an empty answer where setup allows one.';
  }

  function showQuestion(q, current) {
    const form = $('ask');
    if (!q) {
      form.hidden = true;
      askedSeq = null;
      askedQuestion = null;
      pageInputs = {};
      return;
    }
    form.hidden = false;
    if (q.seq === askedSeq) return; // already on screen; keep what is being typed
    askedSeq = q.seq;
    askedQuestion = q;
    $('ask-label').textContent = current ? 'A QUESTION · ' + current.title : 'A QUESTION';
    const context = $('ask-context');
    context.textContent = q.context.join('\n');
    context.hidden = !q.context.length;
    context.scrollTop = context.scrollHeight; // the last lines matter most
    const page = pageOf(q);
    let prompt = q.text;
    if (page) prompt = 'When should the backups run? Times are on this PC’s clock.';
    else if (q.secret) prompt = q.text + ' (a hidden entry: type it here or in the terminal)';
    $('ask-prompt').textContent = prompt;
    const yesno = q.kind === 'yesno';
    $('ask-answer').hidden = yesno || !!page;
    $('ask-yesno').hidden = !yesno;
    $('ask-skip').hidden = !canSkip(q);
    $('ask-page').hidden = !page;
    $('ask-page-actions').hidden = !page;
    pageInputs = {};
    if (page) buildBackupTimes(page);
    const input = $('ask-input');
    input.type = q.secret ? 'password' : 'text';
    input.value = '';
    input.disabled = false;
    for (const id of ['ask-send', 'ask-page-send', 'ask-page-skip']) $(id).disabled = false;
    for (const b of askButtons()) b.disabled = false;
    let note = askNote(q, yesno);
    if (page)
      note =
        'Save sends all three at once; in the terminal they are asked one at a time. Skip for now keeps the times shown.';
    $('ask-note').textContent = note;
    if (!yesno && !page) input.focus();
  }

  /* ---- while it runs */

  function stepList(target, steps, current) {
    target.replaceChildren();
    for (const s of steps) {
      const word = s.status in WORDS ? WORDS[s.status] : s.status;
      const item = el(
        'li',
        { class: s.status },
        el('span', { class: 'mark', 'aria-hidden': 'true', text: MARKS[s.status] || '·' }),
        el(
          'div',
          {},
          s.title + (word ? ' — ' + word : ''),
          s.reason && s.status !== 'success' ? el('small', { text: s.reason }) : null
        )
      );
      if (s.status === 'running') item.setAttribute('aria-current', 'step');
      target.append(item);
      // Keep the step under way in view as the list moves on (not on every poll).
      if (s.status === 'running' && current !== shownCurrent) {
        item.scrollIntoView({ block: 'center' });
        shownCurrent = current;
      }
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
      for (const p of current.packages)
        box.append(
          el('span', { class: 'package' }, el('b', { text: p.name }), p.explanation ? ' — ' + p.explanation : '')
        );
    }
    $('now-notes').replaceChildren(...current.notes.map(n => el('li', { text: n })));
  }

  function showBetween(state) {
    const waiting = state.steps.filter(s => s.status === 'needs_setup');
    $('now-label').textContent = 'BETWEEN STEPS';
    $('now-title').textContent = 'Getting the next step ready…';
    $('now-text').textContent = waiting.length ? 'Still needs you: ' + waiting.map(s => s.title).join(', ') : '';
    $('now-packages').replaceChildren();
    $('now-notes').replaceChildren();
  }

  function progress(state) {
    const pct = state.total ? Math.round((100 * state.done) / state.total) : 0;
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
    const current = state.steps.find(s => s.id === state.current);
    showQuestion(state.question, current);
    $('now').hidden = !!state.question;
    if (current) showCurrent(state, current);
    else showBetween(state);
    stepList($('steps'), state.steps, state.current);
  }

  function renderFinished(state) {
    progress(state);
    // Setup's own verdict (the first checkup included), as the terminal shows it.
    const ready = state.complete === true;
    const title = state.title || (ready ? 'NekoServer is ready' : 'NekoServer is almost ready');
    $('finish-title').textContent = title;
    // Stopped before the steps began (a site that could not be reached, say): not "finished".
    $('finish-label').textContent = state.stopped ? 'STOPPED' : 'FINISHED';
    let word = 'Needs you';
    if (ready) {
      word = 'Ready';
    } else if (state.stopped) {
      word = 'Stopped';
    }
    badge(word, ready ? 'done' : 'waiting');
    // The terminal's ruled lines and its own title are the card's title here.
    $('finish-lines').textContent = state.finish
      .filter(l => !/^=+$/.test(l.trim()) && l.trim().replace(/!$/, '') !== title.replace(/!$/, ''))
      .join('\n')
      .trim();
    $('steps-final').hidden = !state.steps.length;
    stepList($('steps-final'), state.steps, null);
    finishedShown = true;
  }

  function render(state) {
    $('version').textContent = 'setup · v' + state.version;
    const phase = state.phase;
    for (const id of ['choose', 'running', 'finished', 'gone', 'moved']) $(id).hidden = id !== phase;
    $('progress').hidden = phase === 'choose' || !state.total;
    if (phase === 'choose') renderChoose(state);
    if (phase === 'running') renderRunning(state);
    if (phase === 'finished') renderFinished(state);
    $('question').hidden = !state.question || phase !== 'running';
    // A program of its own asks in the terminal only (its questions cannot come here).
    $('terminal').hidden = !state.terminal || !!state.question || phase !== 'running';
    $('terminal-what').textContent = state.terminal ? state.terminal + ': its questions are asked there only.' : '';
    if (phase !== 'running') showQuestion(null);
  }

  function showGone(why) {
    // setup has ended and its server with it, or this window was opened again elsewhere
    $('question').hidden = true;
    $('terminal').hidden = true;
    if (finishedShown && why !== 'moved') {
      // The finishing screen is what the person needs (addresses, what is left): it stays.
      $('finish-closed').hidden = false;
      badge('Setup closed', 'off');
      return;
    }
    const shown = why === 'moved' ? 'moved' : 'gone';
    for (const id of ['choose', 'running', 'finished', 'gone', 'moved']) $(id).hidden = id !== shown;
    badge(why === 'moved' ? 'Opened elsewhere' : 'Setup ended', 'off');
  }

  let stopped = false;
  async function poll() {
    try {
      const response = await fetch('api/state', { cache: 'no-store' });
      if (response.status === 403) {
        // The address was opened again in another window: that one works now.
        stopped = true;
        showGone('moved');
        return;
      }
      if (!response.ok) throw new Error(response.status);
      misses = 0;
      render(await response.json());
    } catch {
      // Setup's server answers until setup ends; a few misses in a row mean it is gone.
      misses += 1;
      if (misses >= 4) showGone('ended');
    }
    if (!stopped)
      setTimeout(
        () => {
          void poll();
        },
        misses ? 2000 : 1000
      );
  }
  void poll();
})();
