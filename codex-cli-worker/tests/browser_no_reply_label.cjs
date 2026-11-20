// No "Codex" label above a reply (CX-04-FB-LABEL). Run with tests/web_fixture.py serving the page on port 9137.
// 1. A chat with replies shows no .answer-heading and no command symbol above any reply.
// 2. Each reply keeps an accessible name ("Codex reply") and its time/status line.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

const base = 'http://127.0.0.1:9137/preview/';
const SIZES = [[390, 844], [1280, 900]];

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage']});
  try {
    for (const [width, height] of SIZES) {
      const context = await browser.newContext({viewport: {width, height}});
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(base);
      await page.locator('.chat-row').first().waitFor();
      // The saved example chat holds replies; newer chats may sit ahead of it.
      while (!(await page.locator('[data-task-id="preview-04"]').count())) {
        const n = await page.locator('.chat-row').count();
        await page.getByRole('button', {name: 'Load older chats'}).evaluate(el => el.click());
        await page.waitForFunction(n => document.querySelectorAll('.chat-row').length > n, n);
      }
      await page.locator('[data-task-id="preview-04"]').evaluate(el => el.click());
      await page.locator('.answer').first().waitFor();
      const answers = await page.locator('.answer').count();
      const label = `width ${width}`;
      assert.ok(answers > 0, `${label}: the fixture has a chat with a reply`);
      assert.equal(await page.locator('.answer-heading').count(), 0, `${label}: no reply heading`);
      assert.ok(!(await page.locator('.answer').first().innerText()).includes('⌘'), `${label}: no command symbol`);
      assert.equal(await page.getByRole('group', {name: 'Codex reply'}).count(), answers, `${label}: each reply keeps its accessible name`);
      assert.ok((await page.locator('.answer .turn-meta').first().innerText()).trim().length > 0, `${label}: the status line stays`);
      assert.deepEqual(errors, [], `${label}: no page errors`);
      await context.close();
    }
  } finally {
    await browser.close();
  }
  console.log('no-reply-label checks passed');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
