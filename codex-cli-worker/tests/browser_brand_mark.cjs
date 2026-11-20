// The header mark follows the page's theme (data-theme, set from Home Assistant), not the system's light or dark
// setting: four cases (system light or dark x data-theme light or dark), on a desktop and a phone.
// Run with tests/web_fixture.py serving the page.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

/** Return which header mark is shown: "light", "dark", "none" or "both". */
async function shownMark(page) {
  return page.evaluate(() => {
    const visible = selector => {
      const el = document.querySelector(selector);
      if (!el) return false;
      const box = el.getBoundingClientRect();
      return getComputedStyle(el).display !== 'none' && box.width > 0 && box.height > 0;
    };
    const light = visible('.brand-icon .brand-mark-light');
    const dark = visible('.brand-icon .brand-mark-dark');
    return light && dark ? 'both' : light ? 'light' : dark ? 'dark' : 'none';
  });
}

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const [name, viewport] of [['desktop', {width: 1280, height: 860}], ['phone', {width: 390, height: 844}]]) {
      for (const system of ['light', 'dark']) {
        for (const theme of ['light', 'dark']) {
          const context = await browser.newContext({viewport, colorScheme: system});
          const page = await context.newPage();
          const errors = []; page.on('pageerror', e => errors.push(e.message));
          await page.goto('http://127.0.0.1:9137/preview/');
          await page.locator('.chat-row').first().waitFor({state: 'attached'});
          await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
          if (name === 'phone') {
            await page.getByRole('button', {name: 'Open conversations', exact: true}).first().click();
          }
          assert.equal(await shownMark(page), theme, `${name}: system ${system}, data-theme ${theme}`);
          // The tab icon keeps following the system setting.
          const tab = await page.evaluate(() => [...document.querySelectorAll('link[rel=icon]')]
            .map(l => `${l.media}|${l.getAttribute('href')}`));
          assert.deepEqual(tab, [
            '(prefers-color-scheme: light)|assets/brand/icon.svg',
            '(prefers-color-scheme: dark)|assets/brand/dark_icon.svg',
          ]);
          assert.deepEqual(errors, []);
          await context.close();
        }
      }
    }
    console.log('brand mark follows data-theme in 8 of 8 cases');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
