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
          // The click handler awaits a request before it opens the dialog; wait for the dialog, never a fixed time.
          await page.locator('#settings[open]').waitFor();

          assert.equal(await page.locator('#appearance-section, #advanced-section').count(), 0,
            'empty Appearance and Advanced sections are removed');
          assert.equal(await page.locator('#diagnostic-retention, #save-diagnostic-retention').count(), 0,
            'the log limit is set on the Configuration tab only');
          assert.equal(await page.locator('#remote-control-name-form').count(), 0,
            'the remote-control name is set on the Configuration tab only');
          assert.equal(await page.locator('#workspace-section #workspace-open').count(), 1,
            'Workspace instructions contains the Workspace rules button');
          // The fixture has no Supervisor, so the lookup answers null: the row is not a link and says where to go.
          const row = page.locator('#addon-configuration');
          assert.equal(await row.count(), 1, 'Settings has one Configuration row');
          await page.getByText("Home Assistant did not give this app's address.", {exact: false}).waitFor();
          assert.equal(await row.getAttribute('href'), null, 'no guessed link when the lookup fails');
          assert.equal(await page.getByRole('link', {name: 'Add-on configuration', exact: true}).count(), 0);
          assert.equal(await row.getAttribute('target'), '_top');
          assert.equal(await page.locator('#export-interaction-logs').count(), 1);
          assert.equal(await page.locator('#pause').count(), 1);
          assert.equal(await page.locator('[data-permission-group]').count(), 5);
          assert.equal(await page.locator('#remote-control-start, #remote-control-stop, #remote-control-pair').count(), 3);
          assert.deepEqual(errors, []);

          // When Supervisor names the slug, the row links to that app's Configuration tab and the usual text returns.
          await page.locator('#close-settings').click();
          await page.route('**/addon-configuration', route => route.fulfill({
            json: {ok: true, url: '/hassio/addon/local_codex_cli_worker/config'},
          }));
          if (await conversations.isVisible()) await conversations.click();
          await page.getByRole('button', {name: 'Open settings', exact: true}).click();
          await page.locator('#settings[open]').waitFor();
          const configuration = page.getByRole('link', {name: 'Add-on configuration', exact: true});
          await configuration.waitFor();
          assert.equal(await configuration.getAttribute('href'), '/hassio/addon/local_codex_cli_worker/config');
          assert.match(await page.locator('#addon-configuration-description').textContent(), /^Model, sandbox/);
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
