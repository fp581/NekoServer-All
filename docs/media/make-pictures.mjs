/* The browser half of make-pictures.py: Chromium (Playwright) opens the real pages and
   takes the pictures. argv[2] is one job as JSON:
     {kind: 'dashboard', address, live, ask, shots: [{route, look, theme, width, height, out, action}]}
     {kind: 'page', url, width, height, out, wait, full}
     {kind: 'pages', pages: [{url, out}], width, height, full}
   What it found is printed as JSON. */
import { createRequire } from 'node:module';
import { basename } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.NEKO_PLAYWRIGHT || 'playwright');
const job = JSON.parse(process.argv[2]);

/* Waits until `test` (run in the page) is true. Not waitForFunction: the dashboard's
   Content Security Policy refuses the eval it needs. */
async function until(page, test, tries = 200) {
  if (await page.evaluate(test)) {
    return;
  }
  if (tries <= 0) {
    throw new Error('waited in vain for ' + test);
  }
  await page.waitForTimeout(50);
  await until(page, test, tries - 1);
}

/* A dashboard tab in its own browser context: the look and theme saved as the Settings
   dialog saves them, the cat still (one frame, not half an animation), the live readings
   and Ask Neko's answer made up. */
async function dashboardPage(browser, { look, theme, width, height }) {
  const context = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 1,
    reducedMotion: 'reduce',
    serviceWorkers: 'block'
  });
  await context.addInitScript(
    ([lookChosen, themeChosen]) => {
      localStorage.setItem('neko-look', JSON.stringify(lookChosen));
      localStorage.setItem('neko-theme', JSON.stringify(themeChosen));
      localStorage.setItem('neko-motion', JSON.stringify('off'));
    },
    [look, theme]
  );
  const page = await context.newPage();
  page.errorsSeen = [];
  page.on('pageerror', error => page.errorsSeen.push(error.message));
  await page.route('**/api/live', route =>
    route.fulfill({ json: { ...job.live, generated_at: Date.now() / 1000 } })
  );
  await page.route('**/api/control/ask', route =>
    route.fulfill({ json: { ok: true, id: 'q1', model: 'qwen3:4b', question: route.request().postDataJSON().question } })
  );
  await page.route('**/api/ask/*', route => route.fulfill({ json: { text: job.ask.answer, done: true } }));
  return page;
}

async function openRoute(page, route) {
  await page.goto(job.address + '#' + route);
  await until(page, () => state.status !== null);
  await page.waitForTimeout(400);
  if (['home', 'apps', 'media', 'cards', 'updates', 'ask'].includes(route)) {
    // Signed in: those pages read the control service too.
    await until(page, () => state.control !== null).catch(() => undefined);
  }
  if (route === 'media') {
    await page.waitForSelector('#media-path', { timeout: 5000 }).catch(() => undefined);
  }
  await page.waitForTimeout(600);
  // The top of the page (the browser may have scrolled to an element named like the page).
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.waitForTimeout(150);
}

/* What could spoil a picture: a page error, late readings, an error line, an issue. */
async function spoilers(page) {
  const said = await page.evaluate(() => ({
    issues: state.issues.map(i => i.level + ': ' + i.title),
    stale: statusStale(),
    errors: [state.statusError, state.liveError, state.controlError].filter(Boolean)
  }));
  return { ...said, pageErrors: page.errorsSeen };
}

async function act(page, action) {
  if (!action) {
    return;
  }
  if (action.startsWith('scroll:')) {
    await page.locator(action.slice(7)).first().scrollIntoViewIfNeeded();
    await page.evaluate(sel => document.querySelector(sel).scrollIntoView({ block: 'start' }), action.slice(7));
    await page.evaluate(() => window.scrollBy(0, -90));
    await page.waitForTimeout(300);
  }
  if (action === 'ask') {
    await page.fill('#ask-input', job.ask.question);
    const answered = page.waitForResponse(r => r.url().includes('/api/ask/'), { timeout: 8000 });
    await page.click('#ask-send');
    await answered.catch(() => undefined);
    await page.waitForTimeout(900);
    await page.evaluate(() => document.activeElement?.blur());
  }
  if (action === 'settings') {
    await page.click('#preferences');
    await page.waitForTimeout(400);
    await page.evaluate(() => document.activeElement?.blur());
  }
}

const KINDS = {
  async dashboard(browser) {
    const found = [];
    for (const shot of job.shots) {
      const page = await dashboardPage(browser, shot);
      await openRoute(page, shot.route);
      await act(page, shot.action);
      await page.screenshot({ path: shot.out });
      found.push({ out: basename(shot.out), ...(await spoilers(page)) });
      await page.context().close();
    }
    return found;
  },
  async page(browser) {
    const context = await browser.newContext({
      viewport: { width: job.width, height: job.height },
      deviceScaleFactor: 1,
      reducedMotion: 'reduce'
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(job.url);
    await page.waitForTimeout(job.wait || 500);
    await page.screenshot({ path: job.out, fullPage: !!job.full });
    return { out: basename(job.out), pageErrors: errors };
  },
  /* Several local pages (frames put together), one after another: job.pages [{url, out}]. */
  async pages(browser) {
    const page = await browser.newPage({ viewport: { width: job.width, height: job.height }, deviceScaleFactor: 1 });
    for (const { url, out } of job.pages) {
      await page.goto(url);
      await page.waitForLoadState('load');
      await page.screenshot({ path: out, fullPage: !!job.full });
    }
    return job.pages.map(p => basename(p.out));
  }
};

const browser = await chromium.launch();
try {
  process.stdout.write(JSON.stringify(await KINDS[job.kind](browser)));
} finally {
  await browser.close();
}
