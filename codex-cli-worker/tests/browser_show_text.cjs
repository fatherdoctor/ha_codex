// Every interim text message of a Codex turn shows in the chat, in order, outside the closed "Worked for" list
// (CX-04-SHOW-TEXT). Run with tests/web_fixture.py serving the page on port 9137.
// 1. The saved first chat has two interim messages and a final answer: both interim messages show without
//    opening the list, in the order Codex wrote them, before the answer, and the answer is not shown twice.
// 2. "Worked for" still holds the steps and thinking, and none of the interim text is inside it.
// 3. A Markdown link in an interim message renders as a link.
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
      await page.locator('#activity-text .interim').first().waitFor();
      const label = `width ${width}`;
      const texts = await page.locator('#activity-text .interim').allTextContents();
      assert.equal(texts.length, 2, `${label}: two interim messages show`);
      assert.match(texts[0], /I'll read the automation/, `${label}: first message first`);
      assert.match(texts[1], /The file reads fine/, `${label}: second message second`);
      assert.equal(await page.locator('#activity-steps').isVisible(), false, `${label}: Worked for stays closed`);
      assert.equal(await page.locator('#activity-text .interim').first().isVisible(), true, `${label}: text shows while closed`);
      assert.equal(await page.locator('#activity .step-message').count(), 0, `${label}: no message row inside the steps`);
      const link = page.locator('#activity-text a', {hasText: 'Home Assistant docs'});
      assert.equal(await link.getAttribute('href'), 'https://www.home-assistant.io/docs/automation/', `${label}: link renders`);
      // Order in the page: the closed line, then the interim text, then the answer, which shows once.
      const order = await page.evaluate(() => {
        const nodes = [...document.querySelectorAll('#activity, #activity-text, .answer')];
        return nodes.map(node => node.id || node.className.split(' ')[0]);
      });
      assert.deepEqual(order.slice(-3), ['activity', 'activity-text', 'answer'], `${label}: order`);
      const answer = await page.locator('.answer').last().innerText();
      assert.ok(answer.trim().length > 0, `${label}: the answer shows`);
      assert.ok(!answer.includes('reads fine') && !answer.includes("I'll read"), `${label}: interim text is not repeated in the answer`);
      await page.locator('#activity .activity-toggle').click();
      assert.ok((await page.locator('#activity .step').count()) > 0, `${label}: the steps open under Worked for`);
      assert.equal(await page.locator('#activity-steps').innerText().then(t => t.includes('reads fine')), false, `${label}: text not duplicated in the list`);
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
