/* The dashboard in a real browser (Chromium, through Playwright), served by the runtime's
   own web server. argv[2]: the page's address; argv[3]: a JSON list of checks, each
   [name, argument]. The answers are printed as a JSON list in the same order. */
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.NEKO_PLAYWRIGHT || 'playwright');
const [address, wanted] = process.argv.slice(2);

/* Open `route` at `width` CSS pixels wide, with the readings in. */
async function open(browser, route, width = 1280) {
  const page = await browser.newPage({ viewport: { width, height: 900 }, serviceWorkers: 'block' });
  await page.goto(address + '#' + route);
  await page.waitForFunction(() => state.status !== null);
  await page.waitForTimeout(300);
  return page;
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
    for (let i = 0; i < 200 && (await page.evaluate(name)) !== label; i++) {
      await page.keyboard.press('Tab');
    }
    const before = await page.evaluate(name);
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
    const reach = async wanted => {
      for (let i = 0; i < 80 && (await focused()) !== wanted; i++) {
        await page.keyboard.press('Tab');
      }
      return focused();
    };
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
  }
};

const browser = await chromium.launch();
try {
  const answers = [];
  for (const [name, argument] of JSON.parse(wanted)) {
    answers.push(await CHECKS[name](browser, argument));
  }
  process.stdout.write(JSON.stringify(answers));
} finally {
  await browser.close();
}
