// The remote-control name field and the missing AGENTS.md hint, at 400 px in light and dark. Run with
// tests/web_fixture.py serving the page. Every name is synthetic. Screenshots go to REMOTE_NAME_SHOTS
// (default /tmp/codex-remote-name-qa).
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const SHOTS = process.env.REMOTE_NAME_SHOTS || '/tmp/codex-remote-name-qa';
const off = {enabled: false, available: true, state: 'off', configured_name: 'Home Assistant', log: []};

(async () => {
  fs.mkdirSync(SHOTS, {recursive: true});
  const browser = await chromium.launch({headless: true});
  try {
    for (const colorScheme of ['light', 'dark']) {
      const context = await browser.newContext({viewport: {width: 400, height: 860}, colorScheme});
      const page = await context.newPage();
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      let remote = off;
      const renames = [];
      await page.route('**/agents', route => route.fulfill({json: {ok: true, exists: false, content: ''}}));
      await page.route('**/remote-control', route => route.fulfill({json: {ok: true, remote_control: remote}}));
      await page.route('**/remote-control/name', route => {
        renames.push(route.request().postDataJSON());
        remote = {...off, configured_name: 'Lake house'};
        return route.fulfill({json: {ok: true, remote_control: remote}});
      });
      await page.goto('http://127.0.0.1:9137/preview/');

      // A missing AGENTS.md is a hint above the chat, not a chat message; Dismiss keeps it away in this browser.
      const hint = page.locator('#agents-hint');
      await hint.waitFor({state: 'visible', timeout: 5000});
      assert.equal(await page.locator('#messages').getByText('AGENTS.md').count(), 0, 'the hint is not in the chat');
      assert.equal(await page.evaluate(() => {
        const hint = document.getElementById('agents-hint').getBoundingClientRect();
        const list = document.getElementById('messages').getBoundingClientRect();
        return hint.top >= list.bottom - 1;
      }), true, 'the hint sits below the message list, so the integration notice stays next to the list');
      await hint.screenshot({path: path.join(SHOTS, `agents-hint-${colorScheme}-400.png`)});
      await hint.getByRole('button', {name: 'Dismiss', exact: true}).click();
      assert.equal(await hint.isVisible(), false);
      await page.reload();
      await page.waitForTimeout(1000);
      assert.equal(await hint.isVisible(), false, 'a dismissed hint stays away after a reload');

      // The name field shows the configured name, saves a new one, and fits the phone.
      await page.evaluate(() => document.querySelector('#connection-manage').click());
      const input = page.locator('#remote-control-name');
      await page.waitForFunction(() => document.querySelector('#remote-control-name').value === 'Home Assistant');
      assert.equal(await input.isDisabled(), false);
      await input.fill('Lake house');
      await page.locator('#remote-control-name-save').click();
      await page.waitForFunction(() => /Saved/.test(document.querySelector('#remote-control-name-note').textContent));
      assert.deepEqual(renames, [{name: 'Lake house'}]);
      assert.equal(await input.inputValue(), 'Lake house');
      const fits = await page.evaluate(() => {
        const dialog = document.querySelector('#connection-dialog');
        const form = document.querySelector('#remote-control-name-form').getBoundingClientRect();
        return dialog.scrollWidth <= dialog.clientWidth && form.right <= dialog.getBoundingClientRect().right + 0.5;
      });
      assert.equal(fits, true, 'the name field fits the dialog at 400 px');
      await page.locator('#remote-control-name-form').screenshot({path: path.join(SHOTS, `remote-name-${colorScheme}-400.png`)});

      // While remote control is on, the name is locked and the page says why.
      remote = {...off, enabled: true, state: 'ready', configured_name: 'Lake house'};
      await page.waitForFunction(() => document.querySelector('#remote-control-name').disabled, null, {timeout: 20000});
      assert.equal(await page.locator('#remote-control-name-note').textContent(), 'Turn remote control off to change the name.');
      assert.deepEqual(errors, []);
      await context.close();
    }
    console.log(`PASS: remote-control name field and AGENTS.md hint at 400 px in light and dark. Screenshots: ${SHOTS}`);
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
