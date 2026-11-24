// Every interim text message of a finished Codex turn shows in the chat, in order, inside its "Worked for" list,
// between the step groups (CX-04-SHOW-TEXT, moved into the list by CX-BLEED-FIX). Run with tests/web_fixture.py
// serving the page on port 9137.
// 1. The saved first chat has two interim messages and an activity copy of the final answer: with the list closed,
//    no interim message shows and the answer follows the closed line and shows once.
// 2. With the list open, the interim messages show in order between the step groups, never as step rows.
// 3. A Markdown link in an interim message renders as a link.
// 4. A check that could not confirm reads "not confirmed" (never "failed") and is not red; the rows and the
//    closed line use the same words (CX-CHECKS-TONE).
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

const base = 'http://127.0.0.1:9137/preview/';

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage']});
  try {
    for (const [width, height] of [[390, 844], [1280, 900]]) {
      const context = await browser.newContext({viewport: {width, height}});
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(base);
      await page.locator('[data-task-id="preview-00"]').evaluate(el => el.click());
      await page.locator('#activity .interim').first().waitFor({state: 'attached'});
      const label = `width ${width}`;
      assert.equal(await page.locator('#activity-steps').isVisible(), false, `${label}: Worked for stays closed`);
      assert.equal(await page.locator('#activity-text .interim').count(), 0, `${label}: no interim text outside the list`);
      assert.equal(await page.locator('.interim:visible').count(), 0, `${label}: no interim text shows while closed`);
      // Order in the page: the closed line, then the answer, which shows once.
      const order = await page.evaluate(() => {
        const nodes = [...document.querySelectorAll('#activity, .answer')];
        return nodes.map(node => node.id || node.className.split(' ')[0]);
      });
      assert.deepEqual(order.slice(-2), ['activity', 'answer'], `${label}: order`);
      const answer = await page.locator('.answer').last().innerText();
      assert.ok(answer.trim().length > 0, `${label}: the answer shows`);
      assert.equal(await page.locator('.answer .message:not(.details)').count(), 1, `${label}: one answer`);
      assert.equal(await page.locator('.verification').count(), 0, `${label}: no checks without evidence`);
      assert.ok(!answer.includes('reads fine') && !answer.includes("I'll read"), `${label}: interim text is not repeated in the answer`);
      await page.locator('#activity .activity-toggle').click();
      assert.ok((await page.locator('#activity .step').count()) > 0, `${label}: the steps open under Worked for`);
      const texts = await page.locator('#activity-steps > .interim').allTextContents();
      assert.equal(texts.length, 2, `${label}: two interim messages show in the open list`);
      assert.match(texts[0], /I'll read the automation/, `${label}: first message first`);
      assert.match(texts[1], /The file reads fine/, `${label}: second message second`);
      assert.equal(await page.locator('#activity .step-message').count(), 0, `${label}: no message row inside the steps`);
      assert.equal(await page.locator('#activity-steps').innerText().then(t => t.includes('evening routine looks good')), false, `${label}: the answer is not repeated in the list`);
      const link = page.locator('#activity-steps a', {hasText: 'Home Assistant docs'});
      assert.equal(await link.getAttribute('href'), 'https://www.home-assistant.io/docs/automation/', `${label}: link renders`);
      await page.locator('[data-task-id="preview-03"]').evaluate(el => el.click());
      const unconfirmed = page.getByText('1 check not confirmed', {exact: true});
      await unconfirmed.waitFor();
      assert.equal(await page.getByText('1 check failed').count(), 0, `${label}: not confirmed is not a failure`);
      const muted = await unconfirmed.evaluate(node => getComputedStyle(node.closest('button')).color);
      const error = await page.evaluate(() => { const probe = document.createElement('span'); probe.style.color = 'var(--error)';
        document.body.append(probe); const color = getComputedStyle(probe).color; probe.remove(); return color; });
      assert.notEqual(muted, error, `${label}: not confirmed is not in the error color`);
      await unconfirmed.click();
      await page.getByText('Not confirmed · entity', {exact: true}).waitFor();
      assert.match(await page.locator('.verification > summary').textContent(), /· 1 not confirmed$/, `${label}: closed line`);
      assert.equal(await page.locator('.verification > summary.has-failed').count(), 0, `${label}: closed line is not red`);
      assert.deepEqual(errors, [], `${label}: no page errors`);
      await context.close();
    }
  } finally {
    await browser.close();
  }
  console.log('show-text checks passed');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
