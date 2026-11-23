// The five styles removed in 0.4.0 (eyebrow, footnote, suggestions, welcome-icon, text-button) name no element and
// no rule on the page: the welcome screen, a chat, Settings and the connection dialog, on a desktop and a phone.
// Run with tests/web_fixture.py serving the page. Every name is synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const {unpauseRemote} = require('./remote_unpaused.cjs');

const REMOVED = ['eyebrow', 'footnote', 'suggestions', 'welcome-icon', 'text-button'];

/** Return each removed class that an element on the page carries or a loaded style rule names. */
async function removedInUse(page) {
  return page.evaluate(removed => {
    const found = [];
    for (const name of removed) {
      if (document.getElementsByClassName(name).length) found.push(`element .${name}`);
    }
    const selectors = [];
    const collect = rules => {
      for (const rule of rules) {
        if (rule.selectorText) selectors.push(rule.selectorText);
        if (rule.cssRules) collect(rule.cssRules);
      }
    };
    for (const sheet of document.styleSheets) collect(sheet.cssRules);
    for (const name of removed) {
      const pattern = new RegExp(`\\.${name}(?![\\w-])`);
      if (selectors.some(selector => pattern.test(selector))) found.push(`rule .${name}`);
    }
    return found;
  }, REMOVED);
}

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const viewport of [{width: 1280, height: 860}, {width: 400, height: 860}]) {
      const context = await browser.newContext({viewport});
      const page = await context.newPage();
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      await unpauseRemote(page);
      await page.goto('http://127.0.0.1:9137/preview/');
      await page.locator('#greeting').waitFor({state: 'visible', timeout: 5000});
      assert.deepEqual(await removedInUse(page), [], `welcome screen at ${viewport.width} px`);

      // Open a saved chat rather than send one, so the fixture's chat list stays as the other checks expect it.
      await page.locator('.chat-row [data-task-id]').first().waitFor({state: 'attached'});
      await page.evaluate(() => selectChat(document.querySelector('.chat-row [data-task-id]').dataset.taskId));
      await page.locator('#messages .message').first().waitFor({state: 'visible', timeout: 10000});
      assert.deepEqual(await removedInUse(page), [], `a chat at ${viewport.width} px`);

      await page.evaluate(() => openSettingsAt("text-size-section"));
      await page.locator('#settings').waitFor({state: 'visible', timeout: 5000});
      assert.deepEqual(await removedInUse(page), [], `Settings at ${viewport.width} px`);
      await page.keyboard.press('Escape');

      await page.evaluate(() => openConnection());
      await page.locator('#connection-dialog').waitFor({state: 'visible', timeout: 5000});
      assert.deepEqual(await removedInUse(page), [], `connection dialog at ${viewport.width} px`);
      assert.deepEqual(errors, []);
      await context.close();
    }
    console.log('PASS: no element or style rule uses a removed class, on a desktop and a phone.');
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
