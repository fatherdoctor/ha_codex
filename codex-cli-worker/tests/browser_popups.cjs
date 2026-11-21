// Pop-ups on the chat page, against web_fixture.py (synthetic chats only), at a phone and a PC width.
// 1. The permissions control opens a small pop-up directly above its button, inside the viewport.
// 2. A press outside closes it; so does Escape; pressing the button again toggles it.
// 3. A press outside closes the model and reasoning panel, the model list, the add menu and the Settings dialog.
// 4. A press on the dim area behind a dialog closes it, unless a field in it was changed (then it stays open).
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const base = 'http://127.0.0.1:9137/preview/';

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage']});
  try {
    for (const width of [390, 1280]) {
      const page = await browser.newPage({viewport: {width, height: width === 390 ? 800 : 800}});
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      await page.goto(base);
      await page.locator('.chat-row').first().waitFor();
      const label = `width ${width}`;
      const isOpen = id => page.evaluate(i => document.getElementById(i).open, id);
      const isShown = id => page.evaluate(i => !document.getElementById(i).hidden, id);
      const blank = {x: width - 4, y: 4};

      // 1. Anchored, small, in the viewport.
      await page.locator('#permission-button').click();
      assert.equal(await isOpen('permissions-dialog'), true, `${label}: permissions opens`);
      const button = await page.locator('#permission-button').boundingBox();
      const popup = await page.locator('#permissions-dialog').boundingBox();
      assert.ok(popup.y + popup.height <= button.y + 1, `${label}: pop-up sits above the button (${popup.y + popup.height} vs ${button.y})`);
      assert.ok(button.y - (popup.y + popup.height) <= 16, `${label}: pop-up is right above the button`);
      assert.ok(popup.x >= 0 && popup.x + popup.width <= width && popup.y >= 0, `${label}: pop-up inside the viewport`);
      assert.ok(popup.width <= 300 && popup.height < 480, `${label}: pop-up is small (${popup.width} x ${popup.height})`);
      assert.ok(Math.abs(popup.x - button.x) <= 8 || width === 390, `${label}: pop-up starts at the button`);
      // 2. Outside press, Escape, toggle.
      await page.mouse.click(blank.x, blank.y);
      assert.equal(await isOpen('permissions-dialog'), false, `${label}: outside press closes permissions`);
      await page.locator('#permission-button').click();
      await page.keyboard.press('Escape');
      assert.equal(await isOpen('permissions-dialog'), false, `${label}: Escape closes permissions`);
      await page.locator('#permission-button').click();
      await page.locator('#permission-button').click();
      assert.equal(await isOpen('permissions-dialog'), false, `${label}: the button toggles permissions`);

      // 3. The other pop-ups.
      await page.locator('#attach').click();
      assert.equal(await isShown('plus-menu'), true, `${label}: add menu opens`);
      await page.mouse.click(blank.x, blank.y);
      assert.equal(await isShown('plus-menu'), false, `${label}: outside press closes the add menu`);
      await page.locator('#picker-button').click();
      assert.equal(await isShown('effort-popover'), true, `${label}: model and reasoning panel opens`);
      await page.mouse.click(blank.x, blank.y);
      assert.equal(await isShown('effort-popover'), false, `${label}: outside press closes the model and reasoning panel`);
      await page.locator('#picker-button').click();
      await page.locator('#effort-model').click();
      assert.equal(await isShown('model-popover'), true, `${label}: model list opens`);
      await page.mouse.click(blank.x, blank.y);
      assert.equal(await isShown('model-popover'), false, `${label}: outside press closes the model list`);

      // 4. Dialogs: the dim area closes; a changed field keeps the dialog open.
      await page.evaluate(() => document.getElementById('settings-button').click());
      assert.equal(await isOpen('settings'), true, `${label}: settings opens`);
      await page.mouse.click(3, 3);
      assert.equal(await isOpen('settings'), false, `${label}: a press on the dim area closes settings`);
      await page.evaluate(() => document.getElementById('settings-button').click());
      await page.mouse.click(width / 2, 400);
      assert.equal(await isOpen('settings'), true, `${label}: a press inside settings keeps it open`);
      await page.keyboard.press('Escape');
      assert.equal(await isOpen('settings'), false, `${label}: Escape closes settings`);

      await page.evaluate(() => document.getElementById('rename-dialog').showModal());
      await page.locator('#rename-dialog input').first().fill('Typed words');
      await page.mouse.click(3, 3);
      assert.equal(await isOpen('rename-dialog'), true, `${label}: a changed field keeps the rename dialog open`);
      assert.equal(await page.locator('#rename-dialog input').first().inputValue(), 'Typed words', `${label}: the input is kept`);
      await page.evaluate(() => document.getElementById('rename-dialog').close());
      await page.evaluate(() => document.getElementById('delete-dialog').showModal());
      await page.mouse.click(3, 3);
      assert.equal(await isOpen('delete-dialog'), false, `${label}: a press on the dim area closes the delete dialog`);
      assert.deepEqual(errors, [], `${label}: no page errors`);
      await page.close();
    }
  } finally {
    await browser.close();
  }
  console.log('pop-up checks passed');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
