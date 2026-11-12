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
  const chosen = {}; // the switches pressed here that are not saved yet
  let switches = {}; // every switch as it stands: {id: on}
  let cards = []; // the cards of the first page
  let stage = 'apps'; // the page before setup starts, as setup's server says: 'apps' or 'review'
  let startLabel = 'Install selected apps';
  let built = false;
  let misses = 0;
  let shownCurrent = null; // the running step the list was last scrolled to
  let askedSeq = null; // the question the form shows (rebuilt only when it changes)
  let askedQuestion = null; // that question itself (its page, when it is on one)
  let pageInputs = {}; // the fields of the page shown, by key
  let pickInputs = []; // the tick boxes of a list to pick from (the "pick" page)
  let finishedShown = false; // the finishing screen is up: it stays, also once setup has closed
  let finishBuilt = false; // the finishing page's cards are made (once: a login shown stays shown)

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

  /* ---- before setup starts: what this server is to do, then the review of it */

  let saving = Promise.resolve(); // the last switch on its way to setup's server

  async function save(changes) {
    try {
      const answer = await post('api/choose', changes);
      $('choose-note').textContent = answer.ok === false && answer.reason ? answer.reason : '';
      // Saved: from now on the switch follows setup's server again.
      if (answer.ok !== false) for (const id of Object.keys(changes)) if (chosen[id] === changes[id]) delete chosen[id];
    } catch {
      /* the next poll shows what the server has; the switch stays as pressed */
    }
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

  /* Every card as it stands now. One that needs another (the downloads: films and
     shows, or music) is off and cannot be pressed while none of those is on. The
     caption always says yes or no, in a fixed width, so nothing moves. */
  function showCards() {
    for (const card of cards) {
      const needs = card.extra.needs || [];
      const offered = !needs.length || needs.some(id => switches[id]);
      const on = switches[card.extra.id] && offered;
      card.input.checked = on;
      card.input.disabled = !offered;
      card.node.className = card.extra.under
        ? 'sub-switch' + (offered ? '' : ' unavailable')
        : 'extra' + (on ? ' on' : '') + (offered ? '' : ' unavailable');
      card.caption.textContent = on ? 'yes' : 'no';
    }
  }

  /* One card for each part a person can leave out, under the heading of its kind. */
  function buildChoices(extras) {
    const box = $('extras');
    box.replaceChildren();
    cards = [];
    let part = '';
    for (const extra of extras) {
      if (extra.part && extra.part !== part) {
        part = extra.part;
        box.append(el('h2', { class: 'part-title', text: part }));
      }
      const input = el('input', { type: 'checkbox', 'aria-label': extra.title });
      const caption = el('span', { class: 'caption' });
      const parent = extra.under ? cards.find(card => card.extra.id === extra.under) : null;
      // A switch inside a card (finding and downloading): a line of the card it belongs to.
      const node = parent
        ? el(
            'div',
            { class: 'sub-switch', title: extra.explanation },
            el('span', { class: 'sub-title', text: extra.title }),
            caption,
            el('label', { class: 'switch' }, input, el('span'))
          )
        : el(
            'div',
            { class: 'extra' },
            el('h3', { text: extra.title }),
            el('div', { class: 'choice' }, caption, el('label', { class: 'switch' }, input, el('span'))),
            el('p', { text: extra.explanation }),
            extra.small ? el('small', { class: 'card-small', text: extra.small }) : null
          );
      input.addEventListener('change', () => {
        switches[extra.id] = input.checked;
        chosen[extra.id] = input.checked;
        showCards();
        saving = save({ [extra.id]: input.checked });
      });
      if (extra.installs.length) node.append(...installsOf(extra));
      if (parent) parent.node.append(node);
      else box.append(node);
      cards.push({ extra, input, caption, node });
    }
    built = true;
  }

  /* The review: what Install will do, as setup's server says it from the settings. */
  function showReview(review) {
    $('review-title').textContent = review.title;
    if (!$('start').disabled) $('start').textContent = review.button;
    startLabel = review.button;
    $('review-sections').replaceChildren(
      ...review.sections.map(section =>
        el(
          'div',
          { class: 'review-part' },
          el('h2', { text: section.title }),
          el('ul', {}, ...section.lines.map(line => el('li', { text: line })))
        )
      )
    );
  }

  function showStage() {
    $('choose-apps').hidden = stage !== 'apps';
    $('review').hidden = stage !== 'review';
  }

  /* Continue and Back are setup's to follow: it asks for the drives between the two
     pages, here and in the terminal, and then says which page is shown. */
  async function press(route, note) {
    try {
      const answer = await post(route, {});
      $(note).textContent = answer.ok === false && answer.reason ? answer.reason : '';
    } catch {
      $(note).textContent = 'That did not reach setup. Press it once more.';
    }
  }

  /* Setup did not start (the settings file cannot be read right now, or setup has
     started already, said in `reason`): the button is itself again, to press once more. */
  function startAgain(reason) {
    if (reason) $('review-note').textContent = reason;
    $('start').disabled = false;
    $('start').textContent = startLabel;
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
  /* Continue: on to the drives and then the review, once the last switch has been saved. */
  $('continue').addEventListener('click', () => {
    void (async () => {
      await saving;
      await press('api/continue', 'choose-note');
    })();
  });
  $('back').addEventListener('click', () => {
    void press('api/back', 'review-note');
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
      // Not sent: the question is still waiting, so the ways to answer come back on
      // (they stayed off, and the same question on the next poll did not bring them back).
      askControls(true);
      $('ask-note').textContent = 'Could not reach setup, so that was not sent. Try again, or answer in the terminal.';
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
    if (askedSeq == null) return;
    const page = askedQuestion && pageOf(askedQuestion);
    // A question that can wait takes its own "not now" answer (what Enter alone gives in
    // the terminal); a step that is left takes what Ctrl+D gives there.
    if (page?.form === 'ask' && !page.leave && page.skip != null) void answer(askedSeq, page.skip);
    else void answer(askedSeq, '', true);
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
      prompt: page => page.what + ': choose one, and enter it in both fields.',
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

  // Steps that did not finish: each with its reason, the fix setup knows and "Retry this step".
  PAGES.retry = {
    prompt: page =>
      page.steps.length === 1
        ? page.steps[0].title + ' did not finish.'
        : 'These steps did not finish. Each can be tried again now.',
    build: buildRetry,
    values: page => ({ answer: page.leave }),
    problem: () => '',
    send: page => (page.leave === 'no' ? 'Not now' : 'Finish'),
    skip: false,
    note: () => 'A retry runs that step again, then the steps that waited for it. What is done stays done.'
  };

  // A step that asks you to sign in to apps: each saved login, shown only when asked for.
  PAGES.logins = {
    prompt: () => 'Your login for this step is saved on this server.',
    build: buildLogins,
    values: () => ({ answer: '' }),
    problem: () => '',
    send: () => 'Go on',
    skip: false,
    note: () =>
      'A login is fetched from this PC when you press Show login, and is in no log. Check that nobody else can see your screen.'
  };

  // A few named values asked together (what one download source needs to sign in).
  PAGES.fields = {
    prompt: page => page.what,
    build: buildFields,
    values: inputValues,
    problem: () => '',
    send: () => 'Save',
    skip: true,
    note: page =>
      page.note ||
      'Leave a field empty to keep its default. Skip for now leaves this one out; the others are not affected.'
  };

  // A question in its own words (questions.py): a short question, a line under it, and
  // buttons that say what they do, or a box to fill in. Nothing here tells anyone to type.
  PAGES.ask = {
    prompt: page => page.title,
    build: buildAsk,
    values: () => ({ answer: pageInputs.answer ? pageInputs.answer.value : '' }),
    problem: () => '',
    send: page => page.send,
    sends: page => page.kind === 'text' || page.kind === 'secret',
    skip: page => page.leave || page.skip != null,
    note: page => page.hint
  };

  // Several steps about one thing (the phone, the TV): each with a sliding yes of its
  // own, and one Continue. All on yes confirms the part; with some still off, those stay
  // for the next run of setup and the ones marked are kept.
  PAGES.part = {
    prompt: page => page.title,
    build: buildPart,
    values: partValues,
    problem: () => '',
    send: () => 'Continue',
    skip: false,
    note: () => 'Slide each step to yes once it is done. Steps you leave off are kept for next time.'
  };

  // The drives connected to this PC: one is chosen by clicking it (its number is what the
  // terminal takes). Nothing is typed back: choosing a drive changes nothing on it.
  PAGES.drives = {
    prompt: page => page.what,
    build: buildDrives,
    values: pickValues,
    problem: (page, values) => (values.choice ? '' : 'Choose a drive in the list first.'),
    send: () => 'Use this drive',
    skip: page => page.skippable,
    note: page => page.names
  };

  // The logins of the apps: one choice, and a row for each app that is typed in only
  // where the choice or the app needs it.
  PAGES.accounts = {
    prompt: page => page.title,
    build: buildAccounts,
    values: accountsValues,
    problem: accountsProblem,
    send: () => 'Save these logins',
    skip: false,
    note: () =>
      'Hidden here and in the terminal, and written to no log. Wherever setup asks you to sign in, Show login shows the saved login.'
  };

  // Where everything is stored: a line for each role with the drives to choose from, a
  // line for where each app keeps your files, and the removable drive of the offline copy.
  PAGES.storage = {
    prompt: page => page.title,
    build: buildStorage,
    values: inputValues,
    problem: storageProblem,
    send: () => 'Use these drives',
    skip: false,
    note: page => page.names
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

  function capital(text) {
    return text.charAt(0).toUpperCase() + text.slice(1);
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
    // How many nightly backups are kept: a number for each kind, within its limits.
    const counts = (page.counts || []).map(count => {
      const input = el('input', {
        type: 'number',
        id: 'ask-page-keep_' + count.group,
        min: String(count.least),
        max: String(count.most),
        step: '1',
        inputmode: 'numeric',
        'aria-label': 'How many ' + count.what + ' are kept'
      });
      input.value = f['keep_' + count.group] ?? '';
      pageInputs['keep_' + count.group] = input;
      return row(
        'Keep the latest ' + count.what,
        [input],
        'From ' +
          count.least +
          ' to ' +
          count.most +
          '. An older one is removed only after a new backup has succeeded. This never deletes your photos, films or music.' +
          (count.review ? ' ' + count.review + ' Leave it empty to leave them as they are.' : '')
      );
    });
    // What is backed up, where it goes and when: read, not changed here.
    const table = el('div', { class: 'backup-table', id: 'ask-page-backups' });
    for (const line of page.table || [])
      table.append(
        el(
          'div',
          { class: 'backup-row' },
          el('b', { text: line.what }),
          el('span', { text: [line.when, line.kept, line.covers].filter(Boolean).join(' · ') }),
          line.same_drive
            ? el('small', {
                text: 'This backup is on the same drive as what it protects. If that drive fails, both are gone.'
              })
            : null,
          line.note ? el('small', { text: line.note }) : null
        )
      );
    $('ask-page').replaceChildren(
      row(
        'Daily backups',
        [pageInputs.daily],
        page.daily_words
          ? capital(page.daily_words) + '.'
          : "The media apps' snapshot at this time, the music copy 30 minutes before it and the copy of this PC's own settings an hour after it."
      ),
      // Only on a server that has the weekly copy (gate run B4); its inputs keep their times.
      page.weekly_copy === false
        ? null
        : row(
            'Weekly USB copy',
            [day, pageInputs.weekly],
            page.weekly_words
              ? capital(page.weekly_words) + ', onto the USB drive.'
              : "Your photos and Nextcloud files onto the USB drive, with Immich's database copy 45 minutes before it."
          ),
      row(
        'Monthly check and updates',
        [pageInputs.monthly],
        'On the 1st: every backup copy checked, a snapshot of every app and a restore test; the weekly app updates start at this time too. It must start at night, before ' +
          nightEnd(page) +
          '.'
      ),
      ...counts,
      table
    );
  }

  function backupTimesProblem(page, values) {
    for (const key of ['daily', 'weekly', 'monthly'])
      if (!/^\d\d:\d\d$/.test(values[key])) return 'Every time needs hours and minutes, as HH:MM.';
    if (Number(values.monthly.slice(0, 2)) >= (page.night_end ?? 6))
      return 'The monthly check must start at night, before ' + nightEnd(page) + '.';
    for (const count of page.counts || []) {
      const kept = (values['keep_' + count.group] ?? '').trim();
      // Empty is an answer only where a count from before stays as it is.
      if (kept === '' && count.review) continue;
      if (!/^\d+$/.test(kept) || Number(kept) < count.least || Number(kept) > count.most)
        return 'Keep the latest ' + count.what + ': a whole number from ' + count.least + ' to ' + count.most + '.';
    }
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
    // The recommended ones, ticked with one press (a list of row numbers, when there is one).
    const recommended = (page.recommended || []).filter(n => n >= 1 && n <= pickInputs.length);
    const tickRecommended = el('button', {
      type: 'button',
      class: 'secondary',
      id: 'ask-pick-recommended',
      text: 'Tick the ' + recommended.length + ' recommended'
    });
    tickRecommended.hidden = !page.multiple || !recommended.length;
    tickRecommended.addEventListener('click', () => {
      for (const n of recommended) pickInputs[n - 1].checked = true;
      counted();
    });
    search.addEventListener('input', () => {
      const wanted = search.value.trim().toLowerCase();
      for (const label of list.children) label.hidden = !label.textContent.toLowerCase().includes(wanted);
    });
    list.addEventListener('change', counted);
    counted();
    pageInputs = { search };
    const advice = page.advice ? el('small', { class: 'muted', id: 'ask-pick-advice', text: page.advice }) : null;
    const top = el('div', { class: 'pick-top' }, search, tickRecommended, count);
    $('ask-page').replaceChildren(...[top, list, advice].filter(Boolean));
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

  /* A question's answers: a button for each choice (the first of a yes or no stands
     out), or one box, a password box for something private. */
  function buildAsk(page) {
    pageInputs = {};
    pickInputs = [];
    if (page.kind === 'text' || page.kind === 'secret') {
      const input = el('input', {
        type: page.kind === 'secret' ? 'password' : 'text',
        id: 'ask-page-answer',
        autocomplete: 'off',
        spellcheck: 'false',
        'aria-label': page.title
      });
      pageInputs = { answer: input };
      $('ask-page').replaceChildren(input);
      return;
    }
    const choices = el('div', { class: 'ask-choices', id: 'ask-choices', role: 'group', 'aria-label': page.title });
    page.choices.forEach((choice, index) => {
      const button = el('button', {
        type: 'button',
        class: page.kind === 'yesno' && index === 0 ? 'primary' : 'secondary',
        text: choice.label
      });
      button.addEventListener('click', () => {
        if (askedSeq != null) void answer(askedSeq, choice.answer);
      });
      pickInputs.push(button); // off with the other controls while the answer is on its way
      choices.append(button);
    });
    $('ask-page').replaceChildren(choices);
  }

  /* What the person marked on a part's page, kept for a reload of the window (only
     where the browser allows it; without it the switches simply start off). */
  function marked(page, step, value) {
    const key = 'neko-part:' + page.title + ':' + step.id;
    try {
      if (value === undefined) return sessionStorage.getItem(key) === 'yes';
      sessionStorage.setItem(key, value ? 'yes' : 'no');
    } catch {
      /* no storage here: nothing is remembered */
    }
    return value === true;
  }

  /* One card for each step of a part: its title, what to do, and its switch. The caption
     beside the switch has a fixed box (setup.css), so sliding it moves nothing. */
  let partSwitches = [];
  /* What a step of a part has besides its lines: an address to open on the phone, with
     its QR code, and "Show login" for the login it is signed in with. */
  function stepExtras(step) {
    const parts = [];
    if (step.address) {
      const address = el('div', { class: 'step-address' }, el('code', { text: step.address }));
      if (step.qr) {
        const code = el('img', { class: 'login-qr', alt: 'The address as a QR code', width: '120', height: '120' });
        code.src = step.qr;
        address.append(code);
      }
      parts.push(address);
    }
    if (step.login) {
      const { shown, button, note } = loginParts(step.login);
      parts.push(el('div', { class: 'row-inputs' }, button, note), shown);
    }
    return parts;
  }

  function buildPart(page) {
    pageInputs = {};
    pickInputs = [];
    partSwitches = [];
    const cards = page.steps.map((step, index) => {
      const input = el('input', {
        type: 'checkbox',
        role: 'switch',
        id: 'part-' + step.id,
        'aria-label': 'Done: ' + step.title
      });
      input.value = step.id;
      input.checked = step.done || marked(page, step);
      const caption = el('span', { class: 'caption', text: input.checked ? 'yes' : 'not yet' });
      input.addEventListener('change', () => {
        caption.textContent = input.checked ? 'yes' : 'not yet';
        marked(page, step, input.checked);
      });
      if (step.done) input.disabled = true;
      else pickInputs.push(input); // off with the other controls while the answer is on its way
      partSwitches.push(input);
      return el(
        'div',
        { class: 'retry part-step' },
        el('div', { class: 'row-title', text: index + 1 + '. ' + step.title + (step.done ? ' (done last time)' : '') }),
        step.done ? null : el('ul', {}, ...step.lines.map(line => el('li', { text: line }))),
        ...(step.done ? [] : stepExtras(step)),
        el('div', { class: 'choice' }, caption, el('label', { class: 'switch' }, input, el('span')))
      );
    });
    // What setup checked itself, above the steps: passed, not yet, or not known (never
    // shown as passed), each with its reason and what to do next.
    const marks = { pass: '✓', fail: '✕', unknown: '?' };
    const status = (page.status || []).map(line =>
      el(
        'div',
        { class: 'status-row ' + (marks[line.status] ? line.status : 'unknown') },
        el('span', { class: 'status-mark', text: marks[line.status] || '?' }),
        el('b', { text: line.title }),
        el('span', { text: line.reason }),
        line.next ? el('small', { text: line.next }) : null,
        line.shows ? el('small', { text: line.shows }) : null
      )
    );
    const checked = status.length ? [el('div', { class: 'status-rows', id: 'ask-page-status' }, ...status)] : [];
    $('ask-page').replaceChildren(...checked, ...cards);
  }

  /* Yes when every switch is on; the steps marked go along either way. */
  function partValues() {
    return {
      answer: partSwitches.every(input => input.checked) ? 'yes' : 'no',
      done: partSwitches
        .filter(input => input.checked)
        .map(input => input.value)
        .join(' ')
    };
  }

  /* One card for each drive: what it is, what is on it, the role it has already, and why
     it cannot be the one asked for (such a card cannot be chosen). */
  function buildDrives(page) {
    pickInputs = [];
    pageInputs = {};
    const list = el('div', { class: 'pick drives', id: 'ask-pick', role: 'group', 'aria-label': page.what });
    for (const drive of page.rows) {
      const input = el('input', { type: 'radio', name: 'ask-pick', value: String(drive.number) });
      input.disabled = !!drive.why;
      // Only the ones that can be chosen are switched off and on again around an answer.
      if (!drive.why) pickInputs.push(input);
      const facts = [drive.inside, drive.role, drive.why ? 'Not this one: ' + drive.why : '', drive.caution].filter(
        Boolean
      );
      list.append(
        el(
          'label',
          { class: drive.why ? 'pick-row drive off' : 'pick-row drive' },
          input,
          el('span', { class: 'pick-name', text: drive.number + ' · ' + drive.name + ' · ' + drive.what }),
          ...facts.map(text => el('small', { text }))
        )
      );
    }
    const parts = [page.needs ? el('p', { class: 'drives-needs', text: page.needs }) : null, list];
    $('ask-page').replaceChildren(...parts.filter(Boolean));
  }

  /* The accounts page. The choice is two radio buttons; under it a row for each app. With
     strong passwords made for you a row only says so, unless the app has data already:
     then its current login is typed in, as in every row with your own logins. */
  let accountRows = [];
  function buildAccounts(page) {
    pickInputs = [];
    pageInputs = {};
    accountRows = [];
    const choice = el('div', { class: 'ask-choices accounts-mode', role: 'radiogroup', 'aria-label': page.title });
    for (const [value, label] of page.choices) {
      const input = el('input', { type: 'radio', name: 'accounts-mode', value, id: 'ask-page-mode-' + value });
      input.checked = (page.fields.mode || '1') === value;
      input.addEventListener('change', showAccountRows);
      pickInputs.push(input);
      choice.append(el('label', { class: 'pick-row' }, input, el('span', { class: 'pick-name', text: label })));
    }
    const rows = el('div', { class: 'storage accounts', id: 'ask-page-accounts' });
    for (const app of page.apps) {
      const user = el('input', {
        type: app.email ? 'email' : 'text',
        id: 'ask-page-user:' + app.id,
        autocomplete: 'off',
        spellcheck: 'false',
        placeholder: app.email ? 'Email used to sign in to ' + app.name : 'User name',
        'aria-label': (app.email ? 'Email used to sign in to ' : 'User name for ') + app.name
      });
      user.value = page.fields['user:' + app.id] || '';
      const password = hiddenInput('pass:' + app.id, 'Password for ' + app.name);
      const again = hiddenInput('again:' + app.id, 'The same password again for ' + app.name);
      password.placeholder = app.pin ? 'PIN (6 to 8 digits)' : 'Password';
      again.placeholder = 'The same again';
      // A secret of the server itself (the dashboard PIN, the shared folders' password): no user name.
      if (app.secret_only) user.hidden = true;
      const madeWords = app.pin
        ? 'A PIN is made for you. It lets you restart apps from the dashboard; Show login shows it at the end.'
        : 'A strong password is made and saved for you.';
      const made = el('small', { text: madeWords });
      const typed = el('span', { class: 'row-inputs account-inputs' }, user, password, again);
      const node = el(
        'div',
        { class: 'row' },
        el('span', { class: 'row-title', text: app.name }),
        typed,
        made,
        app.existing ? el('small', { text: page.existing }) : null
      );
      accountRows.push({ app, user, password, again, typed, made });
      // Off with the other controls while the answer is on its way.
      Object.assign(pageInputs, { ['user:' + app.id]: user, ['pass:' + app.id]: password, ['again:' + app.id]: again });
      rows.append(node);
    }
    const problem = el('p', { class: 'drives-needs account-problem', text: page.fields.problem || '' });
    $('ask-page').replaceChildren(el('p', { class: 'drives-needs', text: page.hint }), choice, rows, problem);
    showAccountRows();
  }

  function accountsMode() {
    return (pickInputs.find(input => input.checked) || {}).value || '';
  }

  /* A row is typed in with your own logins, and always for an app that has data already. */
  function showAccountRows() {
    for (const row of accountRows) {
      const typed = accountsMode() === '2' || row.app.existing;
      row.typed.hidden = !typed;
      row.made.hidden = typed;
    }
  }

  function accountsValues() {
    const values = { mode: accountsMode() };
    for (const row of accountRows) {
      values['user:' + row.app.id] = row.user.value;
      values['pass:' + row.app.id] = row.password.value;
      values['again:' + row.app.id] = row.again.value;
    }
    return values;
  }

  function accountsProblem(page, values) {
    if (!values.mode) return 'Choose one of the two first.';
    for (const row of accountRows) {
      if (row.typed.hidden) continue;
      const name = row.app.name;
      if (!row.app.secret_only && !values['user:' + row.app.id].trim())
        return name + ': fill in the ' + (row.app.email ? 'email' : 'user name') + '.';
      // No PIN is a choice: phones can then only look at the dashboard.
      if (row.app.pin && !values['pass:' + row.app.id] && !values['again:' + row.app.id]) continue;
      if (!values['pass:' + row.app.id]) return name + ': fill in a ' + (row.app.pin ? 'PIN' : 'password') + '.';
      if (values['pass:' + row.app.id] !== values['again:' + row.app.id])
        return name + ': the two passwords are not the same.';
    }
    return '';
  }

  /* A list to choose from; an option that cannot be chosen says why and is off. */
  function chooser(key, label, options, value) {
    const select = el('select', { id: 'ask-page-' + key, 'aria-label': label });
    for (const option of options) {
      const node = el('option', { value: option.value, text: option.text });
      node.disabled = !!option.off;
      select.append(node);
    }
    select.value = value ?? '';
    pageInputs[key] = select;
    return select;
  }

  /* What the chosen drive holds now, under its line (it changes with the choice). */
  function driveFacts(role, select, facts) {
    const drive = role.rows.find(row => String(row.number) === select.value);
    facts.textContent = drive ? [drive.name, drive.inside, drive.caution].filter(Boolean).join(' · ') : '';
  }

  /* The storage page: every line is a list to choose from. A drive is named by what it
     is (its size, kind and model), with its number in the terminal's list; one that
     cannot have the role is in the list with the reason, and cannot be chosen. */
  function buildStorage(page) {
    pickInputs = [];
    pageInputs = {};
    const rows = [el('p', { class: 'drives-needs', text: page.hint })];
    for (const role of page.roles) {
      const options = role.rows.map(drive => ({
        value: String(drive.number),
        text:
          drive.number +
          ' · ' +
          drive.what +
          (drive.why ? ' (not this one: ' + drive.why.split('. ')[0] + ')' : '') +
          (String(drive.number) === role.recommended ? ' (recommended)' : ''),
        off: drive.why
      }));
      options.unshift({ value: '', text: role.optional ? 'None for now' : 'Choose a drive…' });
      // The recommended drive is chosen already: a person who changes nothing gets a sensible layout.
      const select = chooser(role.key, role.title, options, page.fields[role.key] || role.recommended || '');
      const facts = el('small', { class: 'drive-facts', id: 'ask-page-' + role.key + '-facts' });
      select.addEventListener('change', () => driveFacts(role, select, facts));
      driveFacts(role, select, facts);
      rows.push(row(role.title, [select], 'For ' + role.holds + '. ' + role.needs), facts);
    }
    for (const place of page.places) {
      const options = place.options.map(([value, text]) => ({ value, text }));
      const select = chooser(place.key, place.title, options, page.fields[place.key] || place.recommended || '');
      // Files kept on the backups' drive: what that means for their backup, said while it is chosen.
      const beside = el('small', { class: 'drive-facts', id: 'ask-page-' + place.key + '-facts' });
      const say = () => {
        beside.textContent = select.value === 'media' ? place.beside_backup || '' : '';
      };
      select.addEventListener('change', say);
      say();
      if (place.install) {
        // A row of the table: the drive the group's apps are installed on, and the drive its files are on.
        const options = place.install.options.map(([value, text]) => ({ value, text }));
        const installed = chooser(
          place.install.key,
          place.group + ': installed on',
          options,
          page.fields[place.install.key]
        );
        rows.push(
          row(
            place.group,
            [
              el('span', { class: 'pair-title', text: 'Installed on' }),
              installed,
              el('span', { class: 'pair-title', text: place.title + ' on' }),
              select
            ],
            ''
          ),
          beside
        );
      } else rows.push(row(place.title, [select], ''), beside);
    }
    rows.push(el('p', { class: 'drives-needs', text: page.network }));
    $('ask-page').replaceChildren(el('div', { class: 'storage' }, ...rows));
  }

  /* A role without a drive is said before anything is sent, and so is the offline backup on
     a drive of another line: it must be a drive of its own. The apps and the backups may
     share one drive (setup says what that means). */
  function storageProblem(page, values) {
    const chosen = {};
    for (const role of page.roles) {
      const value = values[role.key];
      if (!value && !role.optional) return 'Choose a drive for “' + role.title + '” first.';
      const shareable = role.key === 'media_hdd' && chosen[value] && chosen[value].key === 'data_ssd';
      if (value && chosen[value] && !shareable)
        return 'One drive cannot be both “' + chosen[value].title + '” and “' + role.title + '”.';
      if (value && !chosen[value]) chosen[value] = role;
    }
    // A drive of its own for an app's files cannot be the removable drive of the offline backup.
    for (const place of page.places) {
      const value = values[place.key] || '';
      if (value.startsWith('drive:') && values.usb && value.slice(6) === values.usb)
        return 'The drive for “' + place.title + '” cannot also be the offline backup.';
    }
    return '';
  }

  /* One card for each step: why it stopped, what usually fixes it, and its Retry button
     (the button under the cards leaves them as they are). */
  function buildRetry(page) {
    pageInputs = {};
    pickInputs = [];
    const cards = page.steps.map(step => {
      const button = el('button', { type: 'button', class: 'primary', text: 'Retry this step' });
      button.addEventListener('click', () => {
        if (askedSeq != null) void answer(askedSeq, step.answer);
      });
      pickInputs.push(button); // off with the other controls while an answer is on its way
      const fix = step.fix
        ? el(
            'div',
            { class: 'retry-fix' },
            el('b', { text: 'What usually fixes it: ' + step.fix.title }),
            el('ol', {}, ...step.fix.steps.map(line => el('li', { text: line })))
          )
        : null;
      return el(
        'div',
        { class: 'retry' },
        el('div', { class: 'row-title', text: step.title }),
        el('p', { class: 'retry-why', text: step.reason }),
        fix,
        button
      );
    });
    $('ask-page').replaceChildren(...cards);
  }

  /* "Show login" for one saved login: the button, where the login appears until Hide,
     and a line for when it cannot be fetched. */
  function loginParts(id) {
    const shown = el('div', { class: 'login-shown' });
    shown.hidden = true;
    const button = el('button', { type: 'button', class: 'secondary', text: 'Show login' });
    const note = el('small', { class: 'muted' });
    const hide = () => {
      shown.replaceChildren();
      shown.hidden = true;
      button.textContent = 'Show login';
    };
    button.addEventListener('click', async () => {
      if (!shown.hidden) return hide();
      note.textContent = '';
      let login = null;
      try {
        login = await post('api/login', { app: id });
      } catch {
        /* said below */
      }
      if (!login?.ok) {
        note.textContent = login?.reason || 'Could not fetch the login; the terminal can show it too.';
        return;
      }
      showLogin(shown, login);
      button.textContent = 'Hide';
    });
    return { shown, button, note };
  }

  /* One card for each saved login: its name and username, and "Show login", which asks
     setup's own server for the password (and a QR code of it) and shows it until Hide. */
  function buildLogins(page) {
    pageInputs = {};
    pickInputs = [];
    const cards = page.apps.map(app => {
      const { shown, button, note } = loginParts(app.id);
      return el(
        'div',
        { class: 'retry login' },
        el('div', { class: 'row-title', text: app.title }),
        el('p', { class: 'retry-why', text: 'Username: ' + app.username }),
        shown,
        button,
        note
      );
    });
    $('ask-page').replaceChildren(...cards);
  }

  /* The password as text you can copy, and as a QR code for the phone's camera. */
  function showLogin(target, login) {
    const field = el('input', { type: 'text', readonly: '', 'aria-label': 'Password', class: 'login-password' });
    field.value = login.password;
    const copy = el('button', { type: 'button', class: 'secondary', text: 'Copy' });
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(login.password);
        copy.textContent = 'Copied';
      } catch {
        field.select?.();
        copy.textContent = 'Select and copy it';
      }
    });
    const parts = [el('div', { class: 'row-inputs' }, field, copy)];
    if (login.qr) {
      const code = el('img', { class: 'login-qr', alt: 'The password as a QR code', width: '180', height: '180' });
      code.src = login.qr;
      parts.push(code, el('small', { text: 'Scan it with the phone camera: it shows the password as text to paste.' }));
    }
    target.replaceChildren(...parts);
    target.hidden = false;
  }

  /* One row for each value; a login's value is typed hidden. */
  function buildFields(page) {
    pageInputs = {};
    const rows = page.rows.map(field => {
      const input = el('input', {
        type: field.secret ? 'password' : 'text',
        id: 'ask-page-' + field.key,
        autocomplete: 'off',
        spellcheck: 'false',
        'aria-label': field.label
      });
      pageInputs[field.key] = input;
      return row(field.label, [input], field.secret ? 'Hidden, and written to no log.' : '');
    });
    // What to do first (the steps of a form), above its boxes.
    const steps = (page.lines || []).length
      ? [el('ul', { class: 'form-steps', id: 'ask-page-steps' }, ...page.lines.map(line => el('li', { text: line })))]
      : [];
    $('ask-page').replaceChildren(...steps, ...rows);
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
  function canSkip(q) {
    return q.kind === 'yesno' && q.page?.form === 'confirm_step';
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
    $('ask-page-send').hidden = kind.sends ? !kind.sends(page) : false;
    $('ask-page-skip').hidden = !(typeof kind.skip === 'function' ? kind.skip(page) : kind.skip);
    $('ask-page-skip').textContent = page.skip_label || 'Skip for now';
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
    // A list to pick from shows every row itself (the terminal's last lines are its tail),
    // and the retry cards say what the terminal said.
    // A question in its own words shows what came before it only when it is about that.
    const alone =
      ['pick', 'retry', 'logins', 'drives', 'part'].includes(page?.form) || (page?.form === 'ask' && !page.above);
    context.hidden = !q.context.length || alone;
    context.scrollTop = context.scrollHeight; // the last lines matter most
    let prompt = q.text;
    if (page) prompt = PAGES[page.form].prompt(page);
    else if (q.secret) prompt = q.text + ' (a hidden entry: type it here or in the terminal)';
    $('ask-prompt').textContent = prompt;
    const yesno = q.kind === 'yesno' && !page; // a page has its own buttons
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

  /* A menu of its own: its name, and what the terminal said last (the same words). */
  function showSession(session) {
    $('now-label').textContent = 'IN THE TERMINAL TOO';
    $('now-title').textContent = session.title;
    $('now-text').textContent = session.lines.join('\n');
    $('now-packages').replaceChildren();
    $('now-notes').replaceChildren();
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

  /* The switches follow setup's server (one switched in the terminal shows here too),
     except one pressed here that is still on its way. */
  function renderChoose(state) {
    badge('Ready when you are', '');
    if (!built) buildChoices(state.extras);
    for (const extra of state.extras) if (!(extra.id in chosen)) switches[extra.id] = extra.on;
    showCards();
    if (state.review) showReview(state.review);
    stage = state.stage === 'review' ? 'review' : 'apps';
    showStage();
  }

  function renderRunning(state) {
    progress(state);
    const waiting = state.question;
    badge(waiting ? 'Waiting for you' : 'Working…', waiting ? 'waiting' : '');
    // A menu of its own (the recovery menu, the uninstaller) has a name, no steps.
    const current = state.session ? { title: state.session.title } : state.steps.find(s => s.id === state.current);
    // Asked before anything is installed (the storage page): the page's own title is its heading.
    showQuestion(state.question, state.stage === 'storage' ? null : current);
    $('now').hidden = !!state.question;
    $('now-text').className = state.session ? 'said' : '';
    if (state.session) showSession(state.session);
    else if (current) showCurrent(state, current);
    else showBetween(state);
    $('running').className = state.session ? 'running alone' : 'running';
    $('steps').hidden = !!state.session;
    stepList($('steps'), state.steps, state.current);
  }

  /* How one thing stands: its label, a mark for its state, and the words for it. */
  function stateChip(found) {
    const marks = { done: '✓', failed: '✕', waiting: '…', not_tested: '?' };
    return el(
      'span',
      { class: 'finish-state ' + (marks[found.state] ? found.state : 'not_tested') },
      el('b', { text: (marks[found.state] || '?') + ' ' + found.label }),
      ': ' + found.words
    );
  }

  /* A test a person can try, with its three answers; "Test later" keeps nothing waiting. */
  function testRow(test) {
    const said = el('small', { class: 'muted', text: test.words });
    const answers = [
      ['seen', 'It worked'],
      ['not_working', 'It did not work'],
      ['later', 'Test later']
    ].map(([word, label]) => {
      const button = el('button', { type: 'button', class: 'secondary', text: label });
      button.addEventListener('click', async () => {
        let answer = null;
        try {
          answer = await post('api/test', { test: test.id, word });
        } catch {
          /* said below */
        }
        said.textContent = answer?.ok
          ? { seen: 'You said it worked.', not_working: 'You said it did not work.', later: 'Left for later.' }[word]
          : answer?.reason || 'That did not reach setup; the dashboard has the same tests.';
      });
      return button;
    });
    return el(
      'div',
      { class: 'finish-test' },
      el('span', { text: 'Try it: ' + test.title }),
      el('div', { class: 'row-inputs' }, ...answers),
      said
    );
  }

  /* The finishing page: a card for each app, the backups, what still waits, the dashboard. */
  function buildFinish(page) {
    const cards = page.apps.map(app => {
      const parts = [
        el('div', { class: 'row-title', text: app.name }),
        el('div', { class: 'finish-states' }, ...app.states.map(stateChip))
      ];
      const buttons = [];
      if (app.url) {
        parts.push(el('code', { text: app.url }));
        buttons.push(
          el('a', { class: 'secondary button', href: app.url, target: '_blank', rel: 'noopener', text: 'Open' })
        );
      }
      let shown = null;
      if (app.login) {
        const login = loginParts(app.login);
        buttons.push(login.button, login.note);
        shown = login.shown;
      }
      if (buttons.length) parts.push(el('div', { class: 'row-inputs' }, ...buttons));
      if (shown) parts.push(shown);
      if (app.test) parts.push(testRow(app.test));
      return el('div', { class: 'retry finish-app' }, ...parts);
    });
    const list = (title, lines) =>
      lines.length
        ? el(
            'div',
            { class: 'retry' },
            el('div', { class: 'row-title', text: title }),
            el('ul', {}, ...lines.map(line => el('li', { text: line })))
          )
        : null;
    const more = [
      list('Needs fixing', page.attention),
      list('Still waiting for you', page.waiting),
      el(
        'div',
        { class: 'retry' },
        el('div', { class: 'row-title', text: 'Backups' }),
        el('div', { class: 'finish-states' }, ...page.backups.map(stateChip))
      ),
      page.tests.length
        ? el(
            'div',
            { class: 'retry' },
            el('div', { class: 'row-title', text: 'More to try' }),
            ...page.tests.map(testRow)
          )
        : null
    ];
    if ((page.secrets || []).length) {
      const rows = page.secrets.map(secret => {
        const login = loginParts(secret.login);
        return el(
          'div',
          { class: 'finish-test' },
          el('span', { text: secret.name }),
          el('div', { class: 'row-inputs' }, login.button, login.note),
          login.shown
        );
      });
      more.push(
        el('div', { class: 'retry' }, el('div', { class: 'row-title', text: 'Your PIN and passwords' }), ...rows)
      );
    }
    if (page.dashboard) {
      more.push(
        el(
          'div',
          { class: 'retry' },
          el('div', { class: 'row-title', text: 'The dashboard' }),
          el('a', { href: page.dashboard.url, target: '_blank', rel: 'noopener', text: page.dashboard.url }),
          el('small', { text: 'It opens on this PC. ' + page.dashboard.pin })
        )
      );
    }
    $('finish-page').replaceChildren(...cards, ...more.filter(Boolean));
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
    // The finishing page, when setup made one: cards and buttons, no command lines.
    const page = state.finish_page;
    $('finish-page').hidden = !page;
    $('finish-lines').hidden = !!page;
    if (page && !finishBuilt) {
      $('finish-title').textContent = page.heading;
      buildFinish(page);
      finishBuilt = true;
    }
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
