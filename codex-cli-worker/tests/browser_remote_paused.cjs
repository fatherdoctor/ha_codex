// Remote Control is hidden in this version: the fixture's real worker marks the page (nothing is mocked). No Remote
// row, no ChatGPT app pairing part, no remote connection window or slash command, and no remote-control request
// while the page stays open, Settings included. Phone and desktop width.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [390, 1280]) {
      const page = await browser.newPage({viewport: {width, height: 844}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let reads = 0;
      page.on('request', request => { if (new URL(request.url()).pathname.includes('/remote-control')) reads += 1; });
      await page.goto('http://127.0.0.1:9137/preview/');
      await page.waitForFunction(() => document.documentElement.hasAttribute('data-remote-paused'));
      const conversations = page.getByRole('button', {name: 'Open conversations', exact: true});
      if (await conversations.isVisible()) await conversations.click();
      assert.equal(await page.locator('#remote-status-row').isVisible(), false, 'Remote row');
      assert.equal(await page.locator('#github-status-row').isVisible(), true, 'the other rows stay');
      await page.getByRole('button', {name: 'Open settings', exact: true}).click();
      const accounts = page.locator('#accounts-section > h3 .settings-toggle');
      if (await accounts.getAttribute('aria-expanded') !== 'true') await accounts.click();
      assert.equal(await page.locator('#github-section').isVisible(), true);
      assert.equal(await page.locator('#app-pairing-section').isVisible(), false, 'pairing part');
      assert.equal(await page.getByText('ChatGPT app pairing').isVisible(), false);
      assert.doesNotMatch(await page.locator('#accounts-section > h3').innerText(), /\bapp\b/i, 'header join');
      assert.doesNotMatch(await page.locator('#addon-configuration-description').innerText(), /remote/i);
      // The window and its slash command: openConnection does nothing, and /connection is not offered.
      await page.evaluate(() => openConnection());
      assert.equal(await page.locator('#connection-dialog').isVisible(), false, 'remote connection window');
      // Not opened while hidden either: a hidden modal window would still block the page.
      assert.equal(await page.locator('#connection-dialog').evaluate(dialog => dialog.open), false, 'window not open');
      assert.equal(await page.locator('#settings').evaluate(dialog => dialog.open), true, 'Settings stays open');
      assert.equal(await page.evaluate(() => SLASH_COMMANDS.find(c => c.name === 'connection').available()), false);
      await page.waitForTimeout(16000);
      assert.equal(reads, 0, 'no remote-control request');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('PASS: Remote Control hidden: no Remote row, no pairing part, no remote window or slash command, no remote-control request in 16 s, phone and desktop.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
