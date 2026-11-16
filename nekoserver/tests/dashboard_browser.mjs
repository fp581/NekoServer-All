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
  /* How wide the page is at `width`: [its scroll width, the window's, whether the page's
     title and the buttons beside it overlap]. */
  async pageWidth(browser, { route, width }) {
    const page = await open(browser, route, width);
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
    await page.waitForFunction(() => !document.getElementById('toast').hidden);
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
  const answers = [];
  for (const [name, argument] of JSON.parse(wanted)) {
    answers.push(await CHECKS[name](browser, argument));
  }
  process.stdout.write(JSON.stringify(answers));
} finally {
  await browser.close();
}
