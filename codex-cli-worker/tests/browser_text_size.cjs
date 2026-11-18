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
  ["chat list", ".chat-row"],
  // studio.css styles these two, so they prove its sizes scale too.
  ["message box", "#message"],
  ["dialog title", "#status-title"],
];

/** Parts inside Settings: a section header, the sign-in text and the schedule form's two selects. */
const SETTINGS_PARTS = [
  ["settings header", "#text-size-section .settings-title"],
  ["sign-in text", "#login-status"],
  ["schedule mode", "#schedule-mode"],
  ["schedule kind", "#schedule-kind"],
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

/** The size's label as the page shows it: Normal, Larger or Largest. */
function label(size) {
  return size[0].toUpperCase() + size.slice(1);
}

/** Open Settings the way a person does; on a phone, Settings is in the chats drawer. */
async function openSettings(page) {
  const drawer = page.getByRole("button", { name: "Open conversations", exact: true });
  if (await drawer.first().isVisible()) await drawer.first().click();
  await page.locator("#settings-button").click();
  await page.locator("#settings").waitFor();
}

/** Pick a size the way a person does, on every screen: Settings, Text size, the choice. */
async function pickSize(page, size) {
  await openSettings(page);
  const toggle = page.locator("#text-size-section .settings-toggle");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await page.locator("#settings").getByLabel(label(size)).check();
  await page.locator("#close-settings").click();
}

/**
 * Check the three sizes, chosen in Settings on every screen: the title row
 * holds no text size button; every part scales together (messages, code,
 * buttons, the chat list, the message box, a dialog title, the activity list,
 * Settings with the sign-in text and the schedule selects), the choice is
 * kept per device and ticked in Settings, and the page fits a phone with no
 * sideways scroll at Largest.
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
    // The thin title row holds the title and the chat menu only.
    assert.equal(await page.locator(".chat-header #text-size-button").count(), 0, `${name}: no text size button`);
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
      // The page needs no sideways scroll.
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
    await openSettings(page);
    assert.equal(
      await page.locator("#settings").getByLabel("Largest").isChecked(),
      true,
    );
    // The Text size header names the size in use.
    assert.equal(
      await page.locator("#text-size-section .settings-status").textContent(),
      "Largest",
    );
    // Settings scales with the rest: a header, the sign-in text, the schedule selects.
    const largest = {};
    for (const [part, selector] of SETTINGS_PARTS)
      largest[part] = await fontSize(page, selector);
    const toggle = page.locator("#text-size-section .settings-toggle");
    if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
    // A choice in Settings applies at once and is saved.
    await page.locator("#settings").getByLabel("Larger").check();
    assert.equal(
      await page.evaluate(() => localStorage.getItem("codex-text-size")),
      "larger",
    );
    if (touch)
      await page.screenshot({
        path: path.join(output, `text-size-${name}-settings.png`),
        animations: "disabled",
      });
    await page.locator("#settings").getByLabel("Normal").check();
    for (const [part, selector] of SETTINGS_PARTS)
      assert(
        Math.abs(largest[part] - (await fontSize(page, selector)) * 1.3) < 0.6,
        `${name}: the ${part} does not scale`,
      );
    assert.equal(
      await page.evaluate(() => localStorage.getItem("codex-text-size")),
      null,
    );
    await page.locator("#close-settings").click();
    // Closing Settings keeps the size as chosen.
    await pickSize(page, "larger");
    assert.equal(
      await page.evaluate(() => document.documentElement.dataset.textSize),
      "larger",
    );
    await context.close();
  }
}

module.exports = testTextSize;
