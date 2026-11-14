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
  /* How wide the page is at `width`: [its scroll width, the window's]. */
  async pageWidth(browser, { route, width }) {
    const page = await open(browser, route, width);
    return page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
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
