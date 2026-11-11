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
  let pickInputs = []; // the tick boxes of a list to pick from (the "pick" page)
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

  /* A card's look: on or off, or undecided (a switch setup asks about later, as long as
     it is left alone). The caption under the switch has a fixed width (setup.css), so
     "asked later" turning into "yes" never moves the card's title. */
  function cardClass(extra, on, undecided) {
    if (undecided) return 'extra undecided';
    return on ? 'extra on' : 'extra';
  }

  /* What an extra installs, folded so the cards stay short: a button at the bottom of the
     card opens the list (each package with what it is for) under it, and the button
     itself stays where it is. */
  function installsOf(extra) {
    const list = el('div', { class: 'installs', id: 'installs-' + extra.id });
    list.hidden = true;
    for (const p of extra.installs)
      list.append(
        el(
          'span',
          { class: 'chip', title: p.explanation },
          el('b', { text: p.name }),
          p.explanation ? ' · ' + p.explanation : ''
        )
      );
    const count = extra.installs.length;
    const words = 'Installs ' + count + (count === 1 ? ' package' : ' packages');
    const toggle = el('button', {
      type: 'button',
      class: 'packages-toggle',
      'aria-expanded': 'false',
      'aria-controls': list.id,
      text: '▸ ' + words
    });
    toggle.addEventListener('click', () => {
      list.hidden = !list.hidden;
      toggle.setAttribute('aria-expanded', String(!list.hidden));
      toggle.textContent = (list.hidden ? '▸ ' : '▾ ') + words;
    });
    return [toggle, list];
  }

  function buildChoices(extras) {
    const box = $('extras');
    box.replaceChildren();
    for (const extra of extras) {
      const input = el('input', { type: 'checkbox', 'aria-label': extra.title });
      input.checked = extra.on;
      const caption = el('span', { class: 'caption', text: extra.asked ? 'asked later' : '' });
      const card = el(
        'div',
        { class: cardClass(extra, extra.on, extra.asked) },
        el('h3', { text: extra.title }),
        el('div', { class: 'choice' }, caption, el('label', { class: 'switch' }, input, el('span'))),
        el('p', { text: extra.explanation })
      );
      input.addEventListener('change', () => {
        chosen[extra.id] = input.checked;
        card.className = cardClass(extra, input.checked, false);
        caption.textContent = input.checked ? 'yes' : 'no';
        void save({ [extra.id]: input.checked });
      });
      if (extra.installs.length) card.append(...installsOf(extra));
      box.append(card);
    }
    built = true;
  }

  /* Setup did not start (the settings file cannot be read right now, or setup has
     started already, said in `reason`): the button is itself again, to press once more. */
  function startAgain(reason) {
    if (reason) $('choose-note').textContent = reason;
    $('start').disabled = false;
    $('start').textContent = 'Start setup';
  }

  async function start() {
    $('start').disabled = true;
    $('start').textContent = 'Starting…';
    try {
      const answer = await post('api/start', { choices: chosen });
      if (answer.ok === false) startAgain(answer.reason);
    } catch {
      startAgain('');
    }
  }
  $('start').addEventListener('click', () => {
    void start();
  });

  /* ---- a question, answered here or in the terminal */

  function askButtons() {
    return $('ask-yesno').querySelectorAll('button');
  }

  /* The ways to answer: off while an answer is on its way, on again for the next question. */
  function askControls(enabled) {
    for (const id of ['ask-send', 'ask-page-send', 'ask-page-skip']) $(id).disabled = !enabled;
    for (const b of askButtons()) b.disabled = !enabled;
    for (const input of [...Object.values(pageInputs), ...pickInputs]) input.disabled = !enabled;
    for (const option of $('ask-options').children) option.disabled = !enabled;
  }

  /* `fields`: the other answers of a page (the backup times, a password's second entry), sent with the one asked. */
  async function answer(seq, text, skip = false, fields = null) {
    askControls(false);
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

  /* ---- pages: related questions shown and answered together.
     Each kind of page says what it asks (prompt), draws its rows (build), reads them
     (values), says why they cannot be sent yet (problem), and names its Send button. */

  const PAGES = {
    // When the backups run: three rows.
    backup_times: {
      prompt: () => 'When should the backups run? Times are on this PC’s clock.',
      build: buildBackupTimes,
      values: inputValues,
      problem: backupTimesProblem,
      send: () => 'Save these times',
      skip: true,
      note: () =>
        'Save sends all three at once; in the terminal they are asked one at a time. Skip for now keeps the times shown.'
    },
    // A password or PIN chosen now: two hidden fields that must match.
    new_password: {
      prompt: page => page.what + ': choose it and type it twice.',
      build: buildNewPassword,
      values: inputValues,
      problem: newPasswordProblem,
      send: () => 'Save',
      skip: false,
      note: page =>
        (page.optional ? 'Leave both empty to go on without one. ' : '') +
        'Hidden here and in the terminal, and written to no log.'
    },
    // One or several of a long list (the download sources): a search box and a list that scrolls.
    pick: {
      prompt: page => page.what,
      build: buildPick,
      values: pickValues,
      problem: pickProblem,
      send: page => (page.multiple ? 'Add the selected ones' : 'Choose this one'),
      skip: true,
      note: page =>
        page.multiple
          ? 'Tick as many as you like. Skip for now adds none.'
          : 'Skip for now chooses none; you can add one later.'
    }
  };

  function pageOf(q) {
    return q.page && Object.hasOwn(PAGES, q.page.form) ? q.page : null;
  }

  function inputValues() {
    return Object.fromEntries(Object.entries(pageInputs).map(([key, input]) => [key, input.value]));
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
      note ? el('small', { text: note }) : null
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

  function backupTimesProblem(page, values) {
    for (const key of ['daily', 'weekly', 'monthly'])
      if (!/^\d\d:\d\d$/.test(values[key])) return 'Every time needs hours and minutes, as HH:MM.';
    if (Number(values.monthly.slice(0, 2)) >= (page.night_end ?? 6))
      return 'The monthly check must start at night, before ' + nightEnd(page) + '.';
    return '';
  }

  function hiddenInput(key, label) {
    return el('input', { type: 'password', id: 'ask-page-' + key, autocomplete: 'new-password', 'aria-label': label });
  }

  /* Both fields, or only "the same again" when the first was typed in the terminal. */
  function buildNewPassword(page) {
    const again = hiddenInput('again', 'The same again');
    if (page.asking === 'again') {
      pageInputs = { again };
      $('ask-page').replaceChildren(row('The same again', [again], 'The first one was typed in the terminal.'));
      return;
    }
    pageInputs = { password: hiddenInput('password', page.what), again };
    $('ask-page').replaceChildren(row(page.what, [pageInputs.password]), row('The same again', [again]));
  }

  function newPasswordProblem(page, values) {
    if (page.asking === 'again') return values.again ? '' : 'Type it once more.';
    if (page.optional && !values.password && !values.again) return '';
    if (!values.password) return 'Type it in both fields.';
    if (page.digits && !/^\d+$/.test(values.password)) return 'Use digits only (0-9).';
    if (values.password.length < (page.least ?? 1))
      return 'Use at least ' + page.least + (page.digits ? ' digits.' : ' characters.');
    if (page.most && values.password.length > page.most) return 'Use at most ' + page.most + ' digits.';
    if (values.password !== values.again) return 'The two do not match. Type them again.';
    return '';
  }

  /* The list is drawn once; the search box only hides rows, so ticks are kept. */
  function buildPick(page) {
    const kind = page.multiple ? 'checkbox' : 'radio';
    const list = el('div', { class: 'pick', id: 'ask-pick', role: 'group', 'aria-label': page.what });
    pickInputs = [];
    (page.items || []).forEach((item, index) => {
      const input = el('input', { type: kind, name: 'ask-pick', value: String(index + 1) });
      pickInputs.push(input);
      list.append(
        el(
          'label',
          { class: 'pick-row' },
          input,
          el('span', { class: 'pick-name', text: item.label }),
          item.note ? el('small', { text: item.note }) : null
        )
      );
    });
    const count = el('small', { id: 'ask-pick-count', class: 'muted' });
    const search = el('input', {
      type: 'search',
      id: 'ask-pick-search',
      placeholder: 'Search the ' + (page.items || []).length + ' in the list',
      'aria-label': 'Search the list'
    });
    const counted = () => {
      const ticked = pickInputs.filter(input => input.checked).length;
      count.textContent = page.multiple ? ticked + ' selected' : '';
    };
    search.addEventListener('input', () => {
      const wanted = search.value.trim().toLowerCase();
      for (const label of list.children) label.hidden = !label.textContent.toLowerCase().includes(wanted);
    });
    list.addEventListener('change', counted);
    counted();
    pageInputs = { search };
    $('ask-page').replaceChildren(el('div', { class: 'pick-top' }, search, count), list);
  }

  /* The numbers ticked, as the terminal takes them: "3", or "1 4 7". */
  function pickValues() {
    return {
      choice: pickInputs
        .filter(input => input.checked)
        .map(input => input.value)
        .join(' ')
    };
  }

  function pickProblem(page, values) {
    return values.choice ? '' : 'Tick one in the list first, or press Skip for now.';
  }

  async function answerPage(q) {
    const page = pageOf(q);
    const kind = PAGES[page.form];
    const values = kind.values(page);
    const problem = kind.problem(page, values);
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

  /* What Enter alone keeps, when the question names it: "[admin]", "(Enter for 04:15)". */
  function defaultOf(text) {
    const named = /\bEnter (?:for|alone:) ([^)]+)\)/.exec(text) || /\[([^\]/]+)\]:?\s*$/.exec(text);
    return named ? named[1].trim() : '';
  }

  /* The numbered lines the terminal printed just before a question that asks for a
     number ("  1) sdb 256 GB", " 2. Radarr", "[1] ...", "1 = ..."): each can be clicked
     instead of typed. */
  function numberedChoices(q) {
    if (q.secret || !/number|which one|choose|type 1 or 2|\[1\]/i.test(q.text)) return [];
    const found = [];
    for (const line of q.context) {
      const numbered = /^\s*\[?(\d{1,3})\s?[\]).:=]\s+(\S.*)$/.exec(line);
      if (numbered) found.push({ number: numbered[1], label: numbered[2] });
    }
    return found.length >= 2 ? found : [];
  }

  function showChoices(q) {
    const target = $('ask-options');
    const choices = numberedChoices(q);
    target.hidden = !choices.length;
    target.replaceChildren();
    for (const choice of choices) {
      const button = el(
        'button',
        { type: 'button', class: 'option' },
        el('b', { text: choice.number }),
        el('span', { text: choice.label })
      );
      button.addEventListener('click', () => {
        $('ask-input').value = choice.number;
        for (const other of target.children) other.className = other === button ? 'option picked' : 'option';
        $('ask-send').focus();
      });
      target.append(button);
    }
  }

  function showPage(q, page) {
    const kind = page ? PAGES[page.form] : null;
    $('ask-page').hidden = !page;
    $('ask-page-actions').hidden = !page;
    pageInputs = {};
    pickInputs = [];
    // A page's rows are inputs of the same form: hidden, a required one would still
    // stop every later Send, so off the page they go.
    if (!page) {
      $('ask-page').replaceChildren();
      return;
    }
    kind.build(page);
    $('ask-page-send').textContent = kind.send(page);
    $('ask-page-skip').hidden = !kind.skip;
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
    const page = pageOf(q);
    // A list to pick from shows every row itself: the terminal's last lines are its tail.
    context.hidden = !q.context.length || page?.form === 'pick';
    context.scrollTop = context.scrollHeight; // the last lines matter most
    let prompt = q.text;
    if (page) prompt = PAGES[page.form].prompt(page);
    else if (q.secret) prompt = q.text + ' (a hidden entry: type it here or in the terminal)';
    $('ask-prompt').textContent = prompt;
    const yesno = q.kind === 'yesno';
    $('ask-answer').hidden = yesno || !!page;
    $('ask-yesno').hidden = !yesno;
    $('ask-skip').hidden = !canSkip(q);
    showPage(q, page);
    showChoices(page || yesno ? { ...q, context: [] } : q);
    const input = $('ask-input');
    input.type = q.secret ? 'password' : 'text';
    input.value = '';
    input.placeholder = q.secret || !defaultOf(q.text) ? '' : 'Enter alone keeps: ' + defaultOf(q.text);
    input.disabled = false;
    askControls(true);
    $('ask-note').textContent = page ? PAGES[page.form].note(page) : askNote(q, yesno);
    if (page) Object.values(pageInputs)[0]?.focus();
    else if (!yesno) input.focus();
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
    const waiting = state.question;
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
    // One notice at a time while setup runs, always in the same place (setup.css).
    $('notices').hidden = phase !== 'running';
    $('question').hidden = !state.question || phase !== 'running';
    $('working').hidden = !$('question').hidden;
    if (phase !== 'running') showQuestion(null);
  }

  function showGone(why) {
    // setup has ended and its server with it, or this window was opened again elsewhere
    $('notices').hidden = true;
    $('question').hidden = true;
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
