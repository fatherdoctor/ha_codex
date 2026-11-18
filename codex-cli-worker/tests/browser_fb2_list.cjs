// CX-FB2-LIST: "Your chats" as one list card with flat one-line rows, and the visible, resizable side bar divider.
// Run with tests/web_fixture.py serving the page. Every title is synthetic. Screenshots go to FB2_LIST_SHOTS
// (default /tmp/codex-fb2-list-qa). The page runs standalone here: no parent window, so every Home Assistant
// variable falls back to the page's own palette.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const SHOTS = process.env.FB2_LIST_SHOTS || '/tmp/codex-fb2-list-qa';

/** Measure the list card, its rows and the divider in the open page. */
async function measure(page) {
  return page.evaluate(() => {
    const px = (el, prop) => parseFloat(getComputedStyle(el)[prop]);
    const card = document.querySelector('.chats-card');
    const rows = [...document.querySelectorAll('.chats-card .chat-row')];
    const first = rows[0];
    const previews = [...document.querySelectorAll('.chats-card .row-preview')];
    const divider = document.getElementById('divider');
    const line = getComputedStyle(divider, '::before');
    return {
      card: {border: getComputedStyle(card).borderTopWidth, radius: px(card, 'borderTopLeftRadius'), inSidebar: Boolean(card.closest('#sidebar'))},
      rowCount: rows.length,
      rowHeights: rows.map(row => Math.round(row.getBoundingClientRect().height)),
      rowRadius: px(first, 'borderTopLeftRadius'),
      previewsHidden: previews.every(el => getComputedStyle(el).display === 'none'),
      icons: rows.filter(row => row.querySelector('.row-title .row-icon')).length,
      titleOneLine: rows.every(row => px(row.querySelector('.row-title'), 'height') < 30),
      divider: {width: Math.round(divider.getBoundingClientRect().width), cursor: getComputedStyle(divider).cursor,
        lineWidth: line.width, lineColor: line.backgroundColor, min: divider.getAttribute('aria-valuemin'), max: divider.getAttribute('aria-valuemax')},
      sidebarWidth: Math.round(document.getElementById('sidebar').getBoundingClientRect().width),
    };
  });
}

(async () => {
  fs.mkdirSync(SHOTS, {recursive: true});
  const browser = await chromium.launch({headless: true});
  try {
    for (const colorScheme of ['light', 'dark']) {
      const context = await browser.newContext({viewport: {width: 1280, height: 860}, colorScheme});
      const page = await context.newPage();
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      await page.goto('http://127.0.0.1:9137/preview/');
      await page.locator('.chats-card .chat-row').first().waitFor();
      const m = await measure(page);
      assert.equal(m.card.border, '1px', 'the card has a 1 px border');
      assert.equal(m.card.radius, 12, 'the card has the 12 px Home Assistant corner');
      assert.ok(m.card.inSidebar && m.rowCount > 3, 'the card holds the chat rows in the side bar');
      assert.ok(m.rowHeights.every(h => h === 36), `desktop rows are 36 px: ${[...new Set(m.rowHeights)]}`);
      assert.equal(m.rowRadius, 0, 'rows are flat, one card holds them');
      assert.ok(m.previewsHidden && m.titleOneLine, 'one line for each chat');
      assert.equal(m.icons, m.rowCount, 'every row has an icon');
      // The heading is the card's header with the refresh button; "Archived chats" is a text row at the foot.
      assert.equal(await page.locator('.chats-card .list-heading #refresh').count(), 1);
      assert.equal(await page.locator('.chats-card #archive-list').count(), 1);
      // The open chat has the tinted selected look and no accent bar.
      await page.locator('.chats-card .row-main').first().click();
      const selected = await page.evaluate(() => {
        const row = document.querySelector('.chats-card .chat-row.current');
        const style = getComputedStyle(row);
        return {shadow: style.boxShadow, background: style.backgroundColor};
      });
      assert.equal(selected.shadow, 'none', 'no accent bar on the open chat');
      assert.notEqual(selected.background, 'rgba(0, 0, 0, 0)', 'the open chat has a tint');
      // The divider: 9 px grab area, a 1 px line, the left-right pointer, new limits, double-click for the default.
      assert.equal(m.divider.width, 9);
      assert.equal(m.divider.lineWidth, '1px');
      assert.equal(m.divider.cursor, 'ew-resize');
      assert.deepEqual([m.divider.min, m.divider.max], ['200', '480']);
      const box = await page.locator('#divider').boundingBox();
      await page.mouse.move(box.x + 4, box.y + 200);
      await page.mouse.down();
      await page.mouse.move(60, box.y + 200);
      await page.mouse.up();
      assert.equal((await measure(page)).sidebarWidth, 200, 'the side bar stops at 200 px');
      const box2 = await page.locator('#divider').boundingBox();
      await page.mouse.move(box2.x + 4, box2.y + 200);
      await page.mouse.down();
      await page.mouse.move(1200, box2.y + 200);
      await page.mouse.up();
      assert.equal((await measure(page)).sidebarWidth, 480, 'the side bar stops at 480 px');
      await page.locator('#divider').dblclick();
      assert.equal((await measure(page)).sidebarWidth, 260, 'a double-click gives back the default width');
      await page.screenshot({path: path.join(SHOTS, `list-${colorScheme}-1280.png`)});
      assert.deepEqual(errors, [], 'no page errors');
      await context.close();

      // Phone: the side bar is a drawer, rows are 48 px, there is no divider.
      const phoneContext = await browser.newContext({viewport: {width: 400, height: 860}, colorScheme, hasTouch: true});
      const phone = await phoneContext.newPage();
      await phone.goto('http://127.0.0.1:9137/preview/');
      // The drawer's own button lives in the phone row, which other checks cover; open the drawer as it does.
      await phone.evaluate(() => document.body.classList.add('sidebar-open'));
      await phone.locator('.chats-card .chat-row').first().waitFor();
      const p = await measure(phone);
      assert.ok(p.rowHeights.every(h => h === 48), `phone rows are 48 px: ${[...new Set(p.rowHeights)]}`);
      assert.equal(await phone.locator('#divider').isVisible(), false, 'a phone has no divider');
      await phone.screenshot({path: path.join(SHOTS, `list-${colorScheme}-400.png`)});
      await phoneContext.close();
    }
    console.log(`PASS: the chat list card, one-line flat rows (36 px desktop, 48 px phone), the selected look, and the divider (9 px grab area, 1 px line, 200-480 px limits, double-click default) in light and dark with no parent window. Screenshots: ${SHOTS}`);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
