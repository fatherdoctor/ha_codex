// The message box at the bottom of the chat, on a phone and a desktop, in light and dark. Run with
// tests/web_fixture.py serving the page; every chat in the fixture is synthetic.
// 1. No hint line sits under the box ("Continue this conversation with its saved context." is gone).
// 2. The permissions button's chevron is an icon centered on its label, as on the reasoning-level button.
// 3. The send button's arrow is an icon centered in its circle, not a text glyph on the font's baseline.
// Screenshots go to COMPOSER_SHOTS (default: a folder in the system temporary directory).
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const base = 'http://127.0.0.1:9137/preview/';
const SHOTS = process.env.COMPOSER_SHOTS || path.join(os.tmpdir(), 'codex-composer-qa');
// Home Assistant's own arrow-up icon path (@mdi/js 7.4.47).
const MDI_ARROW_UP = 'M13,20H11V8L5.5,13.5L4.08,12.08L12,4.16L19.92,12.08L18.5,13.5L13,8V20Z';
// Half a pixel of rounding either way is allowed when two centers are compared.
const TOLERANCE = 1;

/** The vertical and horizontal center of an element, from its layout box. */
async function center(page, selector) {
  const box = await page.locator(selector).boundingBox();
  assert.ok(box, `${selector} has a layout box`);
  return {x: box.x + box.width / 2, y: box.y + box.height / 2};
}

/** Check the composer of the chat that is open now. */
async function checkComposer(page, label) {
  assert.equal(await page.locator('#compose-hint').count(), 0, `${label}: the hint line is gone`);
  const below = await page.locator('main').innerText();
  assert.ok(!below.includes('Continue this conversation'), `${label}: no "Continue this conversation" text`);
  assert.ok(!below.includes('A new chat starts a separate conversation'), `${label}: no new-chat hint`);

  const pill = page.locator('#permission-button');
  assert.equal(await pill.locator('svg.picker-chevron').count(), 1, `${label}: the pill has the chevron icon`);
  assert.ok(!(await pill.textContent()).includes('⌄'), `${label}: no text caret in the pill`);
  const labelCenter = await center(page, '#permission-label');
  const chevronCenter = await center(page, '#permission-button svg.picker-chevron');
  assert.ok(Math.abs(labelCenter.y - chevronCenter.y) <= TOLERANCE,
    `${label}: chevron centered on the label (${labelCenter.y} vs ${chevronCenter.y})`);
  assert.ok(chevronCenter.x > labelCenter.x, `${label}: chevron after the label`);

  // The plus is an icon centered on both axes in a round button.
  assert.equal((await page.locator('#attach').innerText()).trim(), '', `${label}: the attach button holds no text glyph`);
  const attachBox = await page.locator('#attach').boundingBox();
  assert.ok(Math.abs(attachBox.width - attachBox.height) <= 1, `${label}: the attach button is round (${attachBox.width} x ${attachBox.height})`);
  const attachCenter = await center(page, '#attach');
  const plusCenter = await center(page, '#attach svg');
  assert.ok(Math.abs(attachCenter.x - plusCenter.x) <= TOLERANCE && Math.abs(attachCenter.y - plusCenter.y) <= TOLERANCE,
    `${label}: plus centered in the attach button`);

  // The New chat icon is about 20 px, not the 142 px an unsized SVG fills on a desktop.
  const newChatIcon = await page.locator('#new-chat svg').boundingBox();
  assert.ok(newChatIcon && Math.abs(newChatIcon.width - 20) <= 2 && Math.abs(newChatIcon.height - 20) <= 2,
    `${label}: the New chat icon is about 20 px (${newChatIcon && newChatIcon.width} x ${newChatIcon && newChatIcon.height})`);

  const send = page.locator('#send');
  if (await send.isVisible()) {
    assert.equal((await send.innerText()).trim(), '', `${label}: the send button holds no text glyph`);
    const icon = await page.evaluate(() => getComputedStyle(document.querySelector('#send .mdi-arrow-up')).maskImage
      || getComputedStyle(document.querySelector('#send .mdi-arrow-up')).webkitMaskImage);
    assert.ok(decodeURIComponent(icon).includes(MDI_ARROW_UP), `${label}: the send arrow is the arrow-up icon`);
    const buttonCenter = await center(page, '#send');
    const arrowCenter = await center(page, '#send .mdi-arrow-up');
    assert.ok(Math.abs(buttonCenter.x - arrowCenter.x) <= TOLERANCE && Math.abs(buttonCenter.y - arrowCenter.y) <= TOLERANCE,
      `${label}: arrow centered in the send button`);
    // The pill and the send button share one middle line in the bottom row.
    const sendCenter = buttonCenter.y;
    const pillCenter = (await center(page, '#permission-button')).y;
    assert.ok(Math.abs(sendCenter - pillCenter) <= 2, `${label}: pill and send button on one line`);
  }
}

(async () => {
  fs.mkdirSync(SHOTS, {recursive: true});
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage']});
  try {
    for (const [width, height] of [[390, 844], [1280, 900]]) for (const theme of ['light', 'dark']) {
      const context = await browser.newContext({viewport: {width, height}, colorScheme: theme});
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(base);
      await page.locator('.chat-row').first().waitFor();
      const label = `${theme} ${width}`;

      await checkComposer(page, `${label} new chat`);
      await page.locator('#message').fill('Synthetic draft');
      await checkComposer(page, `${label} new chat with a draft`);
      await page.locator('#composer').screenshot({path: path.join(SHOTS, `composer-new-${theme}-${width}.png`)});

      // An existing chat is where the old hint said "Continue this conversation with its saved context."
      if (width <= 700) await page.locator('#phone-history').click();
      await page.locator('.chat-row').first().click();
      await page.waitForFunction(() => document.body.dataset.newChat === 'false');
      await checkComposer(page, `${label} saved chat`);
      await page.locator('#composer').screenshot({path: path.join(SHOTS, `composer-chat-${theme}-${width}.png`)});

      assert.deepEqual(errors, [], `${label}: no page errors`);
      await context.close();
    }
  } finally {
    await browser.close();
  }
  console.log(`composer checks passed; screenshots in ${SHOTS}`);
})().catch(error => {
  console.error(error);
  process.exit(1);
});
