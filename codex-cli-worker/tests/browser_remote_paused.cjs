// Remote Control is paused in this version: the fixture's real worker answers {paused: true}; nothing is mocked.
// The settings part and the remote connection window show the one line and no controls, the Remote row says
// Paused with no warning, and the page stops asking for the state. Phone and desktop width.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const PAUSED = 'Remote Control is paused while we fix it. It will come back in a later version.';
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [390, 1280]) {
      const page = await browser.newPage({viewport: {width, height: 844}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let reads = 0;
      page.on('request', request => { if (new URL(request.url()).pathname.endsWith('/remote-control')) reads += 1; });
      await page.goto('http://127.0.0.1:9137/preview/');
      await page.waitForFunction(() => document.querySelector('#remote-status-summary').textContent === 'Paused');
      assert.equal(await page.locator('#remote-status-row').getAttribute('data-state'), 'off');
      assert.equal(await page.evaluate(() => document.body.dataset.attention || ''), '');
      // The worker's state cannot change while paused: one read at load, none while the page stays open.
      await page.waitForTimeout(16000);
      assert.equal(reads, 1, 'the page stops asking once it reads the pause');
      const conversations = page.getByRole('button', {name: 'Open conversations', exact: true});
      if (await conversations.isVisible()) await conversations.click();
      await page.getByRole('button', {name: 'Open settings', exact: true}).click();
      const accounts = page.locator('#accounts-section > h3 .settings-toggle');
      if (await accounts.getAttribute('aria-expanded') !== 'true') await accounts.click();
      const pairing = page.locator('#app-pairing-section .settings-toggle');
      if (await pairing.getAttribute('aria-expanded') !== 'true') await pairing.click();
      await page.waitForFunction(line => document.querySelector('#app-pairing-status').textContent === line, PAUSED);
      assert.equal(await page.locator('#app-pairing-section .settings-status').textContent(), 'Paused');
      for (const id of ['app-pairing-new', 'app-pairing-off', 'remote-connection-open', 'app-pairing-help'])
        assert.equal(await page.locator('#' + id).isVisible(), false, id);
      // Settings opening reads once more and then stops too.
      const afterOpen = reads;
      await page.waitForTimeout(6000);
      assert.equal(reads, afterOpen);
      // The remote connection window (the connection slash command) shows the same line and no controls.
      await page.evaluate(() => openConnection());
      await page.waitForFunction(line => document.querySelector('#remote-control-status').textContent === line, PAUSED);
      assert.match(await page.locator('#connection-title').textContent(), /Paused/);
      for (const id of ['remote-control-start', 'remote-control-stop', 'remote-control-pair', 'remote-control-name-form',
        'remote-control-more', 'remote-control-help'])
        assert.equal(await page.locator('#' + id).isVisible(), false, id);
      assert.equal(await page.locator('#remote-control-card').innerHTML(), '');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('PASS: Remote Control paused: one line and no controls in Settings and the remote connection window, Remote row Paused with no warning, no further state reads, phone and desktop.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
