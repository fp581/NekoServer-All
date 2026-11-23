/* The dashboard in a real browser (Chromium, through Playwright), served by the runtime's
   own web server. argv[2]: the page's address; argv[3]: a JSON list of checks, each
   [name, argument]. The answers are printed as a JSON list in the same order. */
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.NEKO_PLAYWRIGHT || 'playwright');
const [address, wanted] = process.argv.slice(2);

/* Open `route` at `width` CSS pixels wide, with the readings in. `missing`: built-ins taken
   out before the page loads ("Array.prototype.findLast"), as in an older browser. What went
   wrong in the page is kept in page.errorsSeen. */
async function open(browser, route, width = 1280, missing = []) {
  const page = await browser.newPage({ viewport: { width, height: 900 }, serviceWorkers: 'block' });
  page.errorsSeen = [];
  page.on('pageerror', error => page.errorsSeen.push(error.message));
  if (missing.length) {
    await page.addInitScript(names => {
      for (const name of names) {
        const parts = name.split('.');
        const owner = parts.slice(0, -1).reduce((o, key) => o?.[key], globalThis);
        if (owner) {
          delete owner[parts.at(-1)];
        }
      }
    }, missing);
  }
  await page.goto(address + '#' + route);
  await until(page, () => state.status !== null);
  await page.waitForTimeout(300);
  return page;
}

/* Waits until `test` (run in the page) is true, 5 seconds at most. Not waitForFunction: a
   test that is not true at once is run again through eval there, which the page's own
   Content Security Policy refuses. */
async function until(page, test, tries = 100) {
  if (await page.evaluate(test)) {
    return;
  }
  if (tries <= 0) {
    throw new Error('waited 5 s in vain for ' + test);
  }
  await page.waitForTimeout(50);
  await until(page, test, tries - 1);
}

/* Tab pressed until `focused()` names `wanted`, `tries` times at most; then what has focus. */
async function tabTo(page, focused, wanted, tries) {
  const now = await focused();
  if (now === wanted || tries <= 0) {
    return now;
  }
  await page.keyboard.press('Tab');
  return tabTo(page, focused, wanted, tries - 1);
}

const CHECKS = {
  /* Keyboard focus on the control named `label` (Tab pressed until it is reached), then
     the page's readings come in again (what it does every few seconds): where is focus? */
  async focusAfterRefresh(browser, { route, label, reload }) {
    const page = await open(browser, route);
    const name = () => {
      const n = document.activeElement;
      return n && n !== document.body ? (n.getAttribute('aria-label') || n.textContent).trim() : '(no control)';
    };
    const before = await tabTo(page, () => page.evaluate(name), label, 200);
    await page.evaluate(kind => load(kind), reload);
    return { before, after: await page.evaluate(name) };
  },
  /* The Media folders page with the keyboard alone: a folder picked from the list the
     server gives (Enter on each), then typed over, "New downloads go there" ticked with
     Space and the form sent with Enter. Returns the folder box after the pick, and the
     name of the control that had focus at each step. */
  async mediaKeyboard(browser, { width }) {
    const page = await open(browser, 'media', width);
    await page.waitForSelector('#media-path');
    const focused = () =>
      page.evaluate(() => {
        const n = document.activeElement;
        return n && n !== document.body ? n.id || (n.getAttribute('aria-label') || n.textContent).trim() : '';
      });
    const reach = wanted => tabTo(page, focused, wanted, 80);
    const steps = [await reach('media-pick')];
    await page.keyboard.press('Enter'); // the drives and shares
    await page.waitForSelector('.media-list button');
    steps.push(await focused());
    await page.keyboard.press('Enter'); // into the first one
    await page.waitForSelector('#media-use');
    steps.push(await focused());
    await page.keyboard.press('Enter'); // "Use /mnt/nas": back in the box
    const picked = await page.inputValue('#media-path');
    steps.push(await focused());
    await page.keyboard.press('End');
    await page.keyboard.type('/new films');
    steps.push(await reach('media-downloads'));
    await page.keyboard.press('Space');
    steps.push(await reach('Connect folder'));
    await page.keyboard.press('Enter');
    await page.waitForTimeout(500);
    return { picked, steps };
  },
  /* How wide the page is at `width`: [its scroll width, the window's, whether the page's
     title and the buttons beside it overlap]. */
  async pageWidth(browser, { route, width }) {
    const page = await open(browser, route, width);
    if (route === 'media') {
      // Signed in, the folders and the form come with the server's answer.
      await page.waitForSelector('#media-path', { timeout: 3000 }).catch(() => undefined);
    }
    return page.evaluate(() => {
      const words = document.createRange(); // the title's words, not its box
      words.selectNodeContents(document.getElementById('page-title'));
      const title = words.getBoundingClientRect();
      const actions = document.querySelector('.top-actions').getBoundingClientRect();
      const overlap = // by more than a pixel (rounding)
        title.right > actions.left + 1 &&
        title.left + 1 < actions.right &&
        title.bottom > actions.top + 1 &&
        title.top + 1 < actions.bottom;
      return [document.documentElement.scrollWidth, document.documentElement.clientWidth, overlap];
    });
  },
  /* Ask Neko in a browser without the built-ins `missing`: two questions typed and sent
     (the second goes with the first answer). The questions the page sent, and its errors. */
  async askSent(browser, { missing }) {
    const page = await open(browser, 'ask', 1280, missing);
    const sent = [];
    await page.route('**/api/control/ask', route => {
      const asked = route.request().postDataJSON();
      sent.push(asked);
      return route.fulfill({ json: { ok: true, id: 'q' + sent.length, question: asked.question } });
    });
    await page.route('**/api/ask/*', route => route.fulfill({ json: { text: 'An answer.', done: true } }));
    const ask = async question => {
      await page.fill('#ask-input', question);
      // Its answer is asked for a second after it was sent (none when it was not sent).
      const answered = page.waitForResponse(r => r.url().includes('/api/ask/'), { timeout: 4000 });
      await page.click('#ask-send');
      await answered.catch(() => undefined);
      await page.waitForTimeout(200);
    };
    await ask('Is my server fine?');
    await ask('And how do I check?');
    return { sent, errors: page.errorsSeen };
  },
  /* Every page in `routes` opened in a browser without the built-ins `missing`: the errors
     each page had ("never loaded" when its readings never came in). */
  async pageErrors(browser, { routes, missing }) {
    const errorsOf = async route => {
      const page = await open(browser, route, 1280, missing).catch(() => null);
      const seen = page ? page.errorsSeen : ['never loaded'];
      await page?.close();
      return [route, seen];
    };
    // One page after another, as the checks run.
    const pages = await routes.reduce(
      async (earlier, route) => [...(await earlier), await errorsOf(route)],
      Promise.resolve([])
    );
    return Object.fromEntries(pages);
  },
  /* The words of the element `id` on page `route`. */
  async pageText(browser, { route, id }) {
    const page = await open(browser, route);
    return page.evaluate(n => document.getElementById(n).innerText, id);
  },
  /* The Backup cards page against a stand-in control service: whether it is in the menu,
     what it says of the set, the drives it finds when asked, and an enrolment through its
     dialog (the privacy tick, then the button): the dialog's title and the message after. */
  async cardsPage(browser, { width }) {
    const page = await open(browser, 'cards', width);
    const nav = await page.evaluate(() => !document.getElementById('nav-cards').hidden);
    const set = await page.evaluate(() => document.getElementById('cards-set').innerText);
    await page.getByRole('button', { name: 'Look for cards' }).click();
    await page.waitForSelector('#cards-drives article');
    const drives = await page.evaluate(() =>
      [...document.querySelectorAll('#cards-drives article')].map(a => a.innerText.replace(/\s+/g, ' ').trim())
    );
    await page.getByRole('button', { name: 'Use this as card 2 of the set' }).click();
    const dialog = await page.evaluate(() => document.getElementById('dialog-title').textContent);
    await page.locator('#cards-ack').check();
    await page.getByRole('button', { name: 'Use this as card 2', exact: true }).click();
    await until(page, () => !document.getElementById('toast').hidden);
    const toast = await page.evaluate(() => document.getElementById('toast').textContent);
    const widths = await page.evaluate(() => [
      document.documentElement.scrollWidth,
      document.documentElement.clientWidth
    ]);
    return { nav, set, drives, dialog, toast, widths };
  }
};

const browser = await chromium.launch();
try {
  // One check after another (never two at once), the answers in their order.
  const answers = await JSON.parse(wanted).reduce(
    async (earlier, [name, argument]) => [...(await earlier), await CHECKS[name](browser, argument)],
    Promise.resolve([])
  );
  process.stdout.write(JSON.stringify(answers));
} finally {
  await browser.close();
}
