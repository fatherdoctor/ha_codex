// Every code block in a chat message has a Copy button at its top left (CX-CODE-COPY). Run with
// tests/web_fixture.py serving the page on port 9137.
// 1. At phone and desktop width, each code block of the saved Markdown chat has one Copy button, reachable by
//    keyboard and named "Copy code"; it sits at the left of the bar above the code and covers none of the code.
// 2. With a clipboard, a click puts exactly the code on the clipboard, shows "Copied", then shows "Copy" again.
// 3. Where the clipboard is missing or refuses, the code is selected instead and the button says to copy it by hand.
// 4. The chat list preview and the code's own text do not include the button's label.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

const base = 'http://127.0.0.1:9137/preview/';

/** Open the saved Markdown example chat, loading older chats until its row shows. */
async function openMarkdownChat(page) {
  await page.goto(base);
  await page.locator('.chat-row').first().waitFor();
  while (!(await page.locator('[data-task-id="preview-04"]').count())) {
    const count = await page.locator('.chat-row').count();
    // At phone width the button can sit below the fold of the list; a click by the DOM reaches it there.
    await page.getByRole('button', {name: 'Load older chats'}).evaluate(el => el.click());
    await page.waitForFunction(count => document.querySelectorAll('.chat-row').length > count, count);
  }
  await page.locator('[data-task-id="preview-04"]').evaluate(el => el.click());
  await page.locator('#messages .md-code').first().waitFor();
}

/** Check where each Copy button is: top left of its block, above the code, not over it. */
async function checkPlacement(page, label) {
  const blocks = page.locator('#messages .md-code');
  const count = await blocks.count();
  assert.ok(count >= 2, `${label}: the sent message and the answer each have a code block`);
  for (let i = 0; i < count; i++) {
    const block = blocks.nth(i);
    assert.equal(await block.locator('.md-copy').count(), 1, `${label}: block ${i} has one Copy button`);
    const box = await block.boundingBox();
    const button = await block.getByRole('button', {name: 'Copy code'}).boundingBox();
    const code = await block.locator('pre').boundingBox();
    assert.ok(button.x - box.x < 20, `${label}: block ${i} button is at the left`);
    assert.ok(button.y - box.y < 12, `${label}: block ${i} button is at the top`);
    assert.ok(button.y + button.height <= code.y + 0.5, `${label}: block ${i} button does not cover the code`);
    assert.ok(button.x + button.width <= box.x + box.width, `${label}: block ${i} button fits the block`);
  }
}

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage']});
  try {
    for (const [width, height] of [[390, 844], [1280, 900]]) {
      const label = `width ${width}`;
      const context = await browser.newContext({viewport: {width, height}});
      await context.grantPermissions(['clipboard-read', 'clipboard-write'], {origin: 'http://127.0.0.1:9137'});
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await openMarkdownChat(page);
      await checkPlacement(page, label);

      // The code's own text and the chat preview carry no button label.
      const sent = page.locator('#messages .message.user .md-code');
      assert.equal(await sent.locator('pre code').textContent(), 'trigger:\n  - platform: sun\n    event: sunset');
      const preview = await page.locator('[data-task-id="preview-04"]').textContent();
      assert.doesNotMatch(preview, /\bCopy\b/, `${label}: the preview has no Copy label`);

      // Keyboard: Tab reaches the button, Enter copies.
      const button = sent.locator('.md-copy');
      await button.focus();
      assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), 'Copy code');
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.querySelector('#messages .message.user .md-copy').textContent === 'Copied');
      assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'trigger:\n  - platform: sun\n    event: sunset');
      // The label returns after a short time (2 seconds).
      await page.waitForFunction(
        () => document.querySelector('#messages .message.user .md-copy')?.textContent === 'Copy', null, {timeout: 5000});

      // The answer's block copies its own code, the whole of it.
      const answerCode = await page.locator('#messages .answer .md-code pre code').textContent();
      await page.locator('#messages .answer .md-copy').click();
      await page.waitForFunction(() => document.querySelector('#messages .answer .md-copy').textContent === 'Copied');
      assert.equal(await page.evaluate(() => navigator.clipboard.readText()), answerCode);
      assert.deepEqual(errors, [], `${label}: no page errors`);
      await context.close();
    }

    // A web view that refuses the clipboard: the code is selected for a copy by hand.
    for (const refuse of ['missing', 'refused']) {
      const context = await browser.newContext({viewport: {width: 390, height: 844}});
      await context.addInitScript(kind => {
        if (kind === 'missing') Object.defineProperty(navigator, 'clipboard', {value: undefined});
        else navigator.clipboard.writeText = () => Promise.reject(new DOMException('Denied', 'NotAllowedError'));
      }, refuse);
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await openMarkdownChat(page);
      await page.locator('#messages .message.user .md-copy').click();
      await page.waitForFunction(
        () => document.querySelector('#messages .message.user .md-copy').textContent === 'Selected, copy it');
      assert.equal(await page.evaluate(() => window.getSelection().toString()),
        'trigger:\n  - platform: sun\n    event: sunset', `clipboard ${refuse}: the code is selected`);
      assert.deepEqual(errors, [], `clipboard ${refuse}: no page errors`);
      await context.close();
    }
    console.log('browser_code_copy: ok');
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exit(1);
});
