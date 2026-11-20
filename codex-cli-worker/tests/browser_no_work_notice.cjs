// A chat that is working shows no "Codex is working" line above the message box (CX-04-NO-WORKNOTE).
// Run with tests/web_fixture.py serving the page; every chat in the fixture is synthetic.
// 1. While the chat works (Stop task visible), the #notice line is hidden and empty.
// 2. The in-chat "Working on your request..." line is still shown.
// 3. The other notices stay: a second chat opened while one works shows "Another chat is working."
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

const base = 'http://127.0.0.1:9137/preview/';
// The fixture holds one working chat at a time and keeps it, so run this once per width against a fresh
// fixture: `node browser_no_work_notice.cjs phone`, then restart the fixture and run it with `desktop`.
const SIZES = process.argv[2] === 'desktop' ? [[1280, 900]] : [[390, 844]];

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage']});
  try {
    for (const [width, height] of SIZES) {
      const context = await browser.newContext({viewport: {width, height}});
      const page = await context.newPage();
      const errors = [];
      // Release the held chat when this pass ends, pass or fail, so the fixture is clean.
      const release = () => page.request.post('http://127.0.0.1:9137/fixture/release').catch(() => {});
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(base);
      await page.locator('.chat-row').first().waitFor();
      const label = `width ${width}`;

      await page.locator('#message').fill('Take your time and review every automation that uses the porch motion sensor.');
      await page.getByRole('button', {name: /Send message|Add message to queue/}).click();
      await page.getByRole('button', {name: 'Stop task'}).waitFor();
      assert.equal(await page.locator('#notice').isHidden(), true, `${label}: no notice while the chat works (shows: ${await page.locator('#notice').textContent()})`);
      assert.equal((await page.locator('#notice').textContent()).trim(), '', `${label}: the notice line is empty`);
      assert.ok(!(await page.locator('body').innerText()).includes('Codex is working'), `${label}: no "Codex is working" text`);
      await page.waitForFunction(() => document.body.innerText.includes('Working on your request'), null, {timeout: 10000})
        .catch(() => assert.fail(`${label}: the in-chat working line stays`));

      // Another notice still shows: a new chat while this one works.
      // (On a phone New chat sits in the history drawer, so this pass is on the desktop width.)
      if (width > 700) {
        await page.getByRole('button', {name: 'New chat', exact: false}).click();
        await page.locator('#notice').filter({hasText: /Another chat is working\./}).waitFor();
      }

      await release();
      await page.getByRole('button', {name: 'Stop task'}).waitFor({state: 'hidden', timeout: 15000}).catch(() => {});
      assert.deepEqual(errors, [], `${label}: no page errors`);
      await context.close();
    }
  } finally {
    await browser.close();
  }
  console.log('no-work-notice checks passed');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
