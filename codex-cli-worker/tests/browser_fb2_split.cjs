// Configuration-only values leave Settings; interactive controls stay on the page.
// Run against tests/web_fixture.py. The fixture uses temporary, synthetic data.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [400, 1280]) {
      for (const colorScheme of ['light', 'dark']) {
        const page = await browser.newPage({viewport: {width, height: 900}, colorScheme});
        try {
          const errors = [];
          page.on('pageerror', error => errors.push(error.message));
          await page.goto('http://127.0.0.1:9137/preview/');
          const conversations = page.getByRole('button', {name: 'Open conversations', exact: true});
          if (await conversations.isVisible()) await conversations.click();
          await page.getByRole('button', {name: 'Open settings', exact: true}).click();

          assert.equal(await page.locator('#appearance-section, #advanced-section').count(), 0,
            'empty Appearance and Advanced sections are removed');
          assert.equal(await page.locator('#diagnostic-retention, #save-diagnostic-retention').count(), 0,
            'the log limit is set on the Configuration tab only');
          assert.equal(await page.locator('#remote-control-name-form').count(), 0,
            'the remote-control name is set on the Configuration tab only');
          assert.equal(await page.locator('#workspace-section #workspace-open').count(), 1,
            'Workspace instructions contains the Workspace rules button');
          const configuration = page.getByRole('link', {name: 'Add-on configuration', exact: true});
          assert.equal(await configuration.count(), 1, 'Settings has one Configuration row');
          assert.match(await configuration.getAttribute('href'), /\/hassio\/addon\/[^/]+\/config$/);
          assert.equal(await configuration.getAttribute('target'), '_top');
          assert.equal(await page.locator('#export-interaction-logs').count(), 1);
          assert.equal(await page.locator('#pause').count(), 1);
          assert.equal(await page.locator('[data-permission-group]').count(), 5);
          assert.equal(await page.locator('#remote-control-start, #remote-control-stop, #remote-control-pair').count(), 3);
          assert.deepEqual(errors, []);
          console.log(`PASS Configuration split at ${width}px in ${colorScheme}, without a parent window`);
        } finally {
          await page.close();
        }
      }
    }
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
