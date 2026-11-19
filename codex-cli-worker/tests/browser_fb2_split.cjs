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

          assert.equal(await page.locator('#appearance-section').count(), 0, 'the empty Appearance section is removed');
          // Advanced is the last section, folded, and holds Browser permissions as a part of its own.
          const toggle = id => page.locator(`#${id} > :is(h3, h4) > .settings-toggle`);
          assert.equal(await page.locator('#settings > section').last().getAttribute('id'), 'advanced-section');
          assert.equal(await toggle('advanced-section').getAttribute('aria-expanded'), 'false', 'Advanced starts folded');
          assert.equal(await page.locator('#browser-permissions-section').isVisible(), false);
          await page.waitForFunction(() => document.querySelector('#advanced-section .settings-status').textContent !== '');
          assert.match(await page.locator('#advanced-section > h3 .settings-status').textContent(), /^Browser permissions: /);
          // One top-level section is open at a time. Opening a part opens its section and closes the others.
          const expanded = () => page.evaluate(() => [...document.querySelectorAll('#settings > section')]
            .filter(section => section.querySelector(':scope > :is(h3, h4) > .settings-toggle')?.getAttribute('aria-expanded') === 'true')
            .map(section => section.id));
          await toggle('accounts-section').click();
          await toggle('github-section').click();
          assert.deepEqual(await expanded(), ['accounts-section']);
          await toggle('scheduled-work').click();
          assert.deepEqual(await expanded(), ['scheduled-work'], 'opening Scheduled runs closes Accounts and connections');
          assert.equal(await page.locator('#accounts-section > .settings-body').isVisible(), false);
          await toggle('advanced-section').click();
          assert.deepEqual(await expanded(), ['advanced-section']);
          await toggle('browser-permissions-section').click();
          assert.equal(await page.locator('#browser-permissions').isVisible(), true, 'Browser permissions opens inside Advanced');
          await toggle('advanced-section').click();
          assert.deepEqual(await expanded(), [], 'a second tap closes the open section');
          // A part keeps its own state while its section is closed: GitHub is still open when Accounts opens again.
          await toggle('accounts-section').click();
          assert.equal(await toggle('github-section').getAttribute('aria-expanded'), 'true');
          assert.deepEqual(await expanded(), ['accounts-section']);
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
          assert.equal(await page.locator('#export-interaction-logs, #interaction-logs-section').count(), 0, 'Export logs is gone');
          assert.equal(await page.locator('#assistant-section, #pause, #paused-banner').count(), 0, 'Pause the assistant is gone');
          assert.equal(await page.locator('[data-permission-group]').count(), 0, 'the pre-approved action groups are removed');
          assert.equal(await page.locator('#remote-control-start, #remote-control-stop, #remote-control-pair').count(), 3);
          assert.deepEqual(errors, []);

          // When Supervisor names the slug, the row links to that app's Configuration tab and the usual text returns.
          await page.locator('#close-settings').click();
          await page.route('**/addon-configuration', route => route.fulfill({
            json: {ok: true, url: '/config/app/local_codex_cli_worker/config'},
          }));
          if (await conversations.isVisible()) await conversations.click();
          await page.getByRole('button', {name: 'Open settings', exact: true}).click();
          await page.locator('#settings[open]').waitFor();
          const configuration = page.getByRole('link', {name: 'Add-on configuration', exact: true});
          await configuration.waitFor();
          assert.equal(await configuration.getAttribute('href'), '/config/app/local_codex_cli_worker/config');
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
