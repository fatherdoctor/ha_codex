// A scheduled run in a chat moves the chat to the top of the list with an unread mark; opening the chat clears it.
// Runs against the disposable web fixture (python codex-cli-worker/tests/web_fixture.py), on phone and desktop widths.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    for (const width of [320, 390, 1280]) {
      const page = await browser.newPage({viewport:{width, height:844}});
      const errors = []; page.on('pageerror', (error) => errors.push(error.message));
      let unreadId = null, read = [];
      // The fixture's chats carry no mark; the list answer gives its oldest chat a scheduled run that just ended.
      await page.route(/\/preview\/tasks\?summary=true/, async (route) => {
        const response = await route.fetch(), data = await response.json();
        const target = data.tasks.find((chat) => chat.task_id === unreadId) ||
          [...data.tasks].filter((chat) => !chat.pinned).sort((a, b) => a.updated_at.localeCompare(b.updated_at))[0];
        unreadId = target.task_id;
        if (!read.includes(unreadId)) Object.assign(target, {unread:true, updated_at:'2099-01-01T00:00:00+00:00'});
        return route.fulfill({response, json:data});
      });
      await page.route(/\/preview\/tasks\/[^/]+\/read$/, (route) => {
        read.push(decodeURIComponent(new URL(route.request().url()).pathname.split('/').at(-2)));
        return route.fulfill({json:{ok:true, unread:false}});
      });
      await page.goto('http://127.0.0.1:9137/preview/');
      await page.waitForFunction(() => typeof state !== 'undefined' && document.querySelector('.chat-row.unread'));
      const conversations = page.getByRole('button', {name:'Open conversations', exact:true});
      if (await conversations.isVisible()) await conversations.click();
      // The chat is first after the pinned chats, and only it has the mark.
      const order = await page.$$eval('.chat-row .row-main', (rows) => rows.map((row) => row.dataset.taskId));
      const pinned = await page.evaluate(() => state.chats.filter((chat) => chat.pinned).length);
      assert.equal(order[pinned], unreadId, `width ${width}: the chat with a new scheduled run leads the recent chats`);
      assert.equal(await page.locator('.chat-row.unread').count(), 1);
      const row = page.locator(`.chat-row:has([data-task-id="${unreadId}"])`);
      const dot = row.locator('.unread-dot');
      assert.equal(await dot.getAttribute('aria-label'), 'New scheduled run');
      // The mark sits inside the row at every width, beside a title that may be cut short.
      const [dotBox, rowBox] = [await dot.boundingBox(), await row.boundingBox()];
      assert.ok(dotBox.width >= 6 && dotBox.x >= rowBox.x && dotBox.x + dotBox.width <= rowBox.x + rowBox.width,
        `width ${width}: the unread mark stays inside its row`);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      // Opening the chat tells the worker and clears the mark at once.
      await row.locator('.row-main').click();
      await page.waitForFunction((id) => !document.querySelector(`.chat-row.unread [data-task-id="${id}"]`), unreadId);
      assert.deepEqual(read, [unreadId]);
      assert.equal(await page.locator('.unread-dot').count(), 0);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('browser_sched_unread: PASS (320, 390, 1280)');
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });
