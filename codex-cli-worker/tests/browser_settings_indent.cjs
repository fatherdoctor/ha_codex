// Every Settings section's body (description, buttons, fields) starts at the same left edge as the section's title
// text, after the chevron, on a desktop and a phone. Run with tests/web_fixture.py serving the page.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const {unpauseRemote} = require('./remote_unpaused.cjs');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    let checked = 0;
    for (const [name, viewport] of [['desktop', {width: 1280, height: 860}], ['phone', {width: 400, height: 860}]]) {
      const context = await browser.newContext({viewport});
      const page = await context.newPage();
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      await unpauseRemote(page); await page.goto('http://127.0.0.1:9137/preview/');
      await page.locator('.chat-row').first().waitFor({state: 'attached'});
      const conversations = page.getByRole('button', {name: 'Open conversations', exact: true});
      if (await conversations.isVisible()) await conversations.click();
      await page.getByRole('button', {name: 'Open settings', exact: true}).click();
      await page.waitForFunction(() => document.querySelectorAll('#settings .settings-toggle').length >= 5);
      // Settings data arrives after the dialog opens. Opening one section closes the others, so open each top-level
      // section (with its parts) in turn and measure what is open.
      await page.waitForFunction(() => [...document.querySelectorAll('#settings .settings-status')].every(s => s.textContent !== ''));
      const groups = [['accounts-section', 'chatgpt-section', 'github-section', 'app-pairing-section'], ['full-access-section'],
        ['text-size-section'], ['scheduled-work'], ['workspace-section'], ['advanced-section', 'browser-permissions-section']];
      const seen = new Set();
      for (const group of groups) {
        for (const id of group) await page.locator(`#${id} > :is(h3, h4) .settings-toggle`).click();
        const rows = await page.evaluate(() => [...document.querySelectorAll('#settings section')].flatMap(section => {
          const toggle = section.querySelector(':scope > :is(h3, h4) > .settings-toggle');
          const body = section.querySelector(':scope > .settings-body');
          if (!toggle || !body || toggle.getAttribute('aria-expanded') !== 'true' || !body.offsetParent) return [];
          const title = toggle.querySelector('.settings-title');
          const first = [...title.childNodes].find(n => n.nodeType === 3 && n.textContent.trim());
          const text = document.createRange();
          text.setStart(first, 0); text.setEnd(first, 1);
          const kids = [...body.children].filter(c => c.offsetParent && c.getBoundingClientRect().width > 0);
          return [{id: section.id, titleLeft: text.getBoundingClientRect().left,
            kids: kids.map(k => ({tag: k.tagName + (k.id ? '#' + k.id : ''), left: k.getBoundingClientRect().left}))}];
        }));
        for (const row of rows) {
          seen.add(row.id);
          // A part of Accounts or Advanced is a section of its own: it is checked as a row itself, and its box starts
          // at the group title's text.
          for (const kid of row.kids) {
            assert.ok(Math.abs(kid.left - row.titleLeft) <= 1.5,
              `${name}: ${row.id} ${kid.tag} starts at ${kid.left}, its title text at ${row.titleLeft}`);
            checked += 1;
          }
        }
      }
      assert.equal(seen.size, 10, `${name}: every section was measured, got ${[...seen]}`);
      assert.deepEqual(errors, []);
      await context.close();
    }
    console.log(`settings bodies start at their title text: ${checked} parts in 2 widths`);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
