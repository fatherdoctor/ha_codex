// The sidebar header: "for" over "Home Assistant" sits to the right of the Codex word, centered on it, with nothing
// below, on a desktop and a phone, in light and dark. Run with tests/web_fixture.py serving the page.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    let cases = 0;
    for (const [name, viewport] of [['desktop', {width: 1280, height: 860}], ['phone', {width: 390, height: 844}]]) {
      for (const theme of ['light', 'dark']) {
        const context = await browser.newContext({viewport});
        const page = await context.newPage();
        const errors = []; page.on('pageerror', e => errors.push(e.message));
        await page.goto('http://127.0.0.1:9137/preview/');
        await page.locator('.chat-row').first().waitFor({state: 'attached'});
        await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
        if (name === 'phone') await page.getByRole('button', {name: 'Open conversations', exact: true}).first().click();
        const box = await page.evaluate(() => {
          const rect = selector => document.querySelector(selector).getBoundingClientRect().toJSON();
          const lines = [...document.querySelectorAll('.brand .tag > span')].map(s => ({text: s.textContent, ...s.getBoundingClientRect().toJSON()}));
          return {word: rect('.brand strong'), tag: rect('.brand .tag'), brand: rect('.brand'), lines,
            tagText: document.querySelector('.brand .tag').textContent};
        });
        const where = `${name}, ${theme}`;
        assert.deepEqual(box.lines.map(l => l.text), ['for', 'Home Assistant'], where);
        assert.ok(box.lines[1].top >= box.lines[0].bottom - 0.5, `${where}: two stacked lines`);
        assert.ok(box.tag.left >= box.word.right, `${where}: right of the word`);
        const centerGap = Math.abs((box.tag.top + box.tag.bottom) / 2 - (box.word.top + box.word.bottom) / 2);
        assert.ok(centerGap <= 3, `${where}: centered on the word (off by ${centerGap}px)`);
        assert.ok(box.tag.bottom <= box.brand.bottom, `${where}: nothing below the word's row`);
        assert.ok(box.tag.right <= box.brand.right, `${where}: inside the header`);
        assert.deepEqual(errors, []);
        cases += 1;
        await context.close();
      }
    }
    console.log(`header lockup holds in ${cases} of 4 cases`);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
