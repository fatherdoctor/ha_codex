// Exercise the chat page text size setting against web_fixture.py.
const assert = require("node:assert/strict");
const path = require("node:path");

const SIZES = { normal: 1, larger: 1.15, largest: 1.3 };
const SCREENS = [
  ["phone", { width: 390, height: 844 }, true],
  ["small-phone", { width: 320, height: 568 }, true],
  ["ipad", { width: 820, height: 1180 }, true],
  ["desktop", { width: 1440, height: 950 }, false],
];

/** Return the font size in pixels of the first element a selector matches. */
function fontSize(page, selector) {
  return page
    .locator(selector)
    .first()
    .evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
}

const PARTS = [
  ["message", "#messages .message"],
  ["code", "#messages pre code"],
  ["send", "#send"],
  ["title", "#chat-title"],
];

/**
 * Open a saved chat by its id the way a returning visit does: the page
 * reopens the last chat, so this works however far down the list it is.
 */
async function openChat(page, id) {
  await page.evaluate(
    (task) => localStorage.setItem("codex-last-chat", task),
    id,
  );
  await page.reload();
  await page.locator(".chat-row").first().waitFor({ state: "attached" });
  await page.locator("#messages .message").first().waitFor();
}

/** True when the page needs no sideways scroll at its current width. */
function fits(page) {
  return page.evaluate(
    () => document.documentElement.scrollWidth <= innerWidth,
  );
}

/** Pick a size in the Aa pop-up the way a person does: tap Aa, tap the choice. */
async function pickSize(page, size) {
  await page.getByRole("button", { name: "Text size", exact: true }).click();
  await page.locator("#text-size-popover").waitFor();
  await page
    .locator("#text-size-popover")
    .getByLabel(size[0].toUpperCase() + size.slice(1))
    .check();
}

/**
 * Check the three sizes: one tap from the chat, every part scales together
 * (messages, code, buttons, the activity list, the sign-in text), the choice
 * is kept per device and matches Settings, Escape and an outside tap close
 * the pop-up, and the page fits a phone with no sideways scroll at Largest.
 * Screenshots of each size on a phone and an iPad go to the output folder.
 */
async function testTextSize(browser, output, errors) {
  for (const [name, viewport, touch] of SCREENS) {
    const context = await browser.newContext({
      viewport,
      hasTouch: touch,
      isMobile: touch && viewport.width < 800,
    });
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("http://127.0.0.1:9137/preview/");
    await page.locator(".chat-row").first().waitFor({ state: "attached" });
    assert.equal(
      await page.evaluate(() => document.documentElement.dataset.textSize),
      undefined,
    );
    // The steps of the last run, in the first chat, at the normal size.
    await openChat(page, "preview-00");
    await page.locator("#messages .activity-toggle").first().waitFor();
    const activity = await fontSize(page, "#messages .activity-toggle");
    // The chat with Markdown, code and a table.
    await openChat(page, "preview-04");
    await page.locator("#messages pre code").first().waitFor();
    const base = {};
    for (const [part, selector] of PARTS)
      base[part] = await fontSize(page, selector);
    for (const [size, scale] of Object.entries(SIZES)) {
      await pickSize(page, size);
      assert.equal(
        await page.evaluate(
          () => document.documentElement.dataset.textSize || "normal",
        ),
        size,
      );
      for (const [part, selector] of PARTS)
        assert(
          Math.abs((await fontSize(page, selector)) - base[part] * scale) < 0.6,
          `${name} ${size}: ${part} is not ${scale}x`,
        );
      // The pop-up stays on screen and the page needs no sideways scroll.
      const box = await page.locator("#text-size-popover").boundingBox();
      assert(
        box.x >= 0 && box.x + box.width <= viewport.width,
        `${name} ${size} pop-up`,
      );
      if (touch)
        await page.screenshot({
          path: path.join(output, `text-size-${name}-${size}-picker.png`),
          animations: "disabled",
        });
      await page.keyboard.press("Escape");
      assert.equal(await page.locator("#text-size-popover").isHidden(), true);
      assert.equal(await fits(page), true, `${name} ${size} scrolls sideways`);
      if (touch)
        await page.screenshot({
          path: path.join(output, `text-size-${name}-${size}.png`),
          animations: "disabled",
        });
    }
    // The activity list scales too, and still fits.
    await openChat(page, "preview-00");
    await page.locator("#messages .activity-toggle").first().waitFor();
    assert(
      Math.abs(
        (await fontSize(page, "#messages .activity-toggle")) - activity * 1.3,
      ) < 0.6,
      `${name}: the activity list does not scale`,
    );
    assert.equal(await fits(page), true, `${name} activity scrolls sideways`);
    // Largest is kept on this device and ticked in Settings too.
    await page.reload();
    await page.locator(".chat-row").first().waitFor({ state: "attached" });
    assert.equal(
      await page.evaluate(() => document.documentElement.dataset.textSize),
      "largest",
    );
    assert.equal(await fits(page), true, `${name} reload scrolls sideways`);
    if (viewport.width < 800)
      await page
        .getByRole("button", { name: "Open conversations", exact: true })
        .click();
    await page.locator("#settings-button").click();
    await page.locator("#settings").waitFor();
    assert.equal(
      await page.locator("#settings").getByLabel("Largest").isChecked(),
      true,
    );
    // The sign-in text scales with the rest.
    const signInLargest = await fontSize(page, "#login-status");
    // A choice in Settings applies at once and ticks the pop-up too.
    await page.locator("#settings").getByLabel("Larger").check();
    assert.equal(
      await page.evaluate(() => localStorage.getItem("codex-text-size")),
      "larger",
    );
    assert.equal(
      await page.locator("#text-size-popover").getByLabel("Larger").isChecked(),
      true,
    );
    if (touch)
      await page.screenshot({
        path: path.join(output, `text-size-${name}-settings.png`),
        animations: "disabled",
      });
    await page.locator("#settings").getByLabel("Normal").check();
    assert(
      Math.abs(signInLargest - (await fontSize(page, "#login-status")) * 1.3) <
        0.6,
      `${name}: the sign-in text does not scale`,
    );
    assert.equal(
      await page.evaluate(() => localStorage.getItem("codex-text-size")),
      null,
    );
    await page.locator("#close-settings").click();
    // An outside tap closes the pop-up and leaves the size as chosen.
    await pickSize(page, "larger");
    await page.locator("#messages").click({ position: { x: 5, y: 5 } });
    assert.equal(await page.locator("#text-size-popover").isHidden(), true);
    assert.equal(
      await page.evaluate(() => document.documentElement.dataset.textSize),
      "larger",
    );
    await context.close();
  }
}

module.exports = testTextSize;
