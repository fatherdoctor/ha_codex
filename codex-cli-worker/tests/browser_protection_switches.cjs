// Owner protection choices through the real local fixture; every account and file is synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless: true});
  const shots = process.env.SETTINGS_SHOTS || '/tmp/protection-switches-qa';
  fs.mkdirSync(shots, {recursive: true});
  try {
    for (const colorScheme of ['light', 'dark']) {
      const page = await browser.newPage({viewport: {width: 400, height: 860}, colorScheme});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto('http://127.0.0.1:9137/preview/');
      await page.getByRole('button', {name: 'Open conversations', exact: true}).click();
      await page.getByRole('button', {name: 'Open settings', exact: true}).click();
      const commands = page.locator('#command-checks');
      const self = page.locator('#self-protection');
      await page.waitForFunction(() => !document.querySelector('[data-protection=shell_commands]').disabled);
      assert.equal(await commands.locator('.settings-status').textContent(), colorScheme === 'light' ? 'All on' : 'Restart needed');
      for (const input of await page.locator('[data-protection]').all()) assert.equal(await input.isChecked(), true);
      assert.equal(await commands.locator('.settings-toggle').getAttribute('aria-expanded'), 'false');
      assert.equal(await self.locator('.settings-toggle').getAttribute('aria-expanded'), 'false');
      await commands.locator('.settings-toggle').click();
      assert.equal(await commands.locator('[role=switch]').count(), 5);
      const shell = commands.locator('[data-protection=shell_commands]');
      await page.waitForFunction(() => !document.querySelector('[data-protection=shell_commands]').disabled);
      await shell.locator('..').click();
      await page.waitForFunction(() => document.querySelector('#protection-notice').textContent.includes('Shell commands'));
      assert.equal(await page.locator('#protection-notice').isVisible(), true);
      assert.equal(await commands.locator('.settings-status').textContent(), '1 off');
      for (const input of await commands.locator('[role=switch]').all()) {
        if (await input.getAttribute('data-protection') !== 'shell_commands') assert.equal(await input.isChecked(), true);
      }
      await self.locator('.settings-toggle').click();
      const full = self.locator('[data-protection=full_access]');
      page.once('dialog', dialog => dialog.dismiss());
      await full.locator('..').click();
      assert.equal(await full.isChecked(), true, 'cancel does not change the saved choice');
      page.once('dialog', dialog => {
        assert.match(dialog.message(), /container/);
        return dialog.accept();
      });
      await full.locator('..').click();
      await page.waitForFunction(() => document.querySelector('#protection-notice').textContent.includes('Full access'));
      await page.waitForFunction(() => !document.querySelector('[data-protection=full_access]').checked && !document.querySelector('[data-protection=full_access]').disabled);
      await page.screenshot({path: path.join(shots, `protection-switches-${colorScheme}-400.png`), animations: 'disabled'});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.locator('#close-settings').click();
      if (!await page.locator('body').evaluate(body => body.classList.contains('sidebar-open'))) {
        await page.getByRole('button', {name: 'Open conversations', exact: true}).click();
      }
      await page.locator('.chat-row').first().click();
      await page.locator('#phone-thread-actions').click();
      await page.getByRole('menuitem', {name: 'Status', exact: true}).click();
      await page.locator('#status-details').getByText(/Protections selected off:.*Shell commands.*Full access/).waitFor();
      await page.getByRole('button', {name: 'Close status', exact: true}).click();
      await page.getByRole('button', {name: 'Open conversations', exact: true}).click();
      await page.getByRole('button', {name: 'Open settings', exact: true}).click();
      await page.waitForFunction(() => !document.querySelector('[data-protection=full_access]').disabled);
      assert.equal(await full.isChecked(), false, 'the saved choice survives closing the view');
      for (const section of [commands, self]) {
        const toggle = section.locator('.settings-toggle');
        if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
      }
      await full.locator('..').click();
      await page.waitForFunction(() => document.querySelector('[data-protection=full_access]').checked && !document.querySelector('[data-protection=full_access]').disabled);
      await page.waitForFunction(() => !document.querySelector('[data-protection=shell_commands]').disabled);
      await shell.locator('..').click();
      await page.waitForFunction(() => document.querySelector('#command-checks .settings-status').textContent === 'Restart needed');
      assert.match(await page.locator('#protection-notice').textContent(), /Restart the app/);
      assert.deepEqual(errors, []);
      await page.close();
      console.log(`PASS protection choices: ${colorScheme}, 400 px, grouped switches, confirmation, persistence, chat and Status notices`);
    }
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error);process.exitCode = 1;});
