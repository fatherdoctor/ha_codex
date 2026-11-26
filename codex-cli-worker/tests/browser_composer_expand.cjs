// Check the button that expands the message box to the chat's full height (CX-COMPOSER-EXPAND), against
// web_fixture.py, on a desktop, an iPad and a phone. Every chat in the fixture is synthetic.
const assert = require("node:assert/strict");
const path = require("node:path");

const SCREENS = [
  ["desktop", { viewport: { width: 1440, height: 950 } }],
  ["ipad", { viewport: { width: 820, height: 1180 }, hasTouch: true, isMobile: true }],
  ["phone", { viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true }],
];

/** Return the layout boxes of the message box, the composer and the transcript. */
async function boxes(page) {
  return page.evaluate(() => {
    const box = (id) => document.getElementById(id).getBoundingClientRect();
    return { message: box("message"), composer: box("composer"), messages: box("messages"), height: innerHeight };
  });
}

/** Return the expand button's state as the page states it to assistive technology. */
async function buttonState(button) {
  return [await button.getAttribute("aria-expanded"), await button.getAttribute("aria-label")];
}

/**
 * Expanded: taller than the 180 px cap, the composer inside the screen, a strip of the transcript still in view, the
 * message box focused. Collapsed again: back under the cap, with the transcript at the line it showed before.
 */
async function checkScreen(browser, name, options, output, errors) {
  const context = await browser.newContext(options);
  const page = await context.newPage();
  page.on("pageerror", (error) => errors.push(error.message));
  // The fixture's longest chat, opened as the last one used: its transcript scrolls even on the desktop.
  await page.addInitScript(() => localStorage.setItem("codex-last-chat", "preview-04"));
  await page.goto("http://127.0.0.1:9137/preview/");
  await page.waitForFunction(() => {
    const area = document.getElementById("messages");
    return area.scrollHeight - area.clientHeight > 150;
  });
  const button = page.locator("#expand-message");
  assert.deepEqual(await buttonState(button), ["false", "Expand message box"]);
  await page.locator("#message").fill("A draft that stays while the box grows.");
  const before = await boxes(page);
  assert.ok(before.message.height <= 180, `${name}: collapsed box is ${before.message.height} px`);

  // A transcript scrolled away from its end keeps its line through expand and collapse.
  const top = await page.locator("#messages").evaluate((area) => { area.scrollTop = 40; return area.scrollTop; });
  await button.click();
  assert.deepEqual(await buttonState(button), ["true", "Collapse message box"]);
  assert.equal(await page.evaluate(() => document.activeElement.id), "message");
  const open = await boxes(page);
  assert.ok(open.message.height > 180, `${name}: expanded box is ${open.message.height} px`);
  assert.ok(open.composer.bottom <= open.height + 1, `${name}: the composer ends inside the screen`);
  assert.ok(open.composer.top - open.messages.top >= 40, `${name}: a strip of the transcript stays in view`);
  assert.equal(await page.locator("#messages").evaluate((area) => area.scrollTop), top);
  assert.equal(await page.locator("#message").inputValue(), "A draft that stays while the box grows.");
  await page.screenshot({ path: path.join(output, `composer-expanded-${name}.png`), animations: "disabled" });

  // Typing more keeps the expanded height; a window resize recomputes it and keeps the composer on screen.
  await page.locator("#message").press("End");
  await page.keyboard.type(" More text.");
  assert.equal((await boxes(page)).message.height, open.message.height);
  await page.setViewportSize({ width: options.viewport.width, height: options.viewport.height - 200 });
  await page.waitForFunction((height) => document.getElementById("message").offsetHeight < height, open.message.height);
  const shorter = await boxes(page);
  assert.ok(shorter.message.height < open.message.height, `${name}: a shorter window gives a shorter box`);
  assert.ok(shorter.composer.bottom <= shorter.height + 1, `${name}: still inside the shorter screen`);
  await page.setViewportSize(options.viewport);
  await page.waitForFunction((height) => document.getElementById("message").offsetHeight === height, open.message.height);

  // The keyboard works the button too: Enter collapses it.
  await button.focus();
  await page.keyboard.press("Enter");
  assert.deepEqual(await buttonState(button), ["false", "Expand message box"]);
  assert.ok((await boxes(page)).message.height <= 180, `${name}: collapsed again`);
  assert.equal(await page.locator("#messages").evaluate((area) => area.scrollTop), top);

  // At the end of the transcript, expanding keeps it at the end.
  await page.locator("#messages").evaluate((area) => { area.scrollTop = area.scrollHeight; });
  await button.click();
  assert.ok(await page.locator("#messages").evaluate(
    (area) => area.scrollHeight - area.scrollTop - area.clientHeight < 2), `${name}: still at the end`);

  // Send collapses the box.
  await page.locator("#message").fill(`Expanded send on ${name}.`);
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await page.locator("#messages").getByText(`Preview response: Expanded send on ${name}.`, { exact: true }).waitFor();
  assert.deepEqual(await buttonState(button), ["false", "Expand message box"]);
  assert.ok((await boxes(page)).message.height <= 180, `${name}: collapsed after Send`);
  await page.screenshot({ path: path.join(output, `composer-collapsed-${name}.png`), animations: "disabled" });
  await context.close();
}

/** Run the check on each screen. */
async function testComposerExpand(browser, output, errors) {
  for (const [name, options] of SCREENS) await checkScreen(browser, name, options, output, errors);
}

module.exports = testComposerExpand;

if (require.main === module) {
  const { chromium } = require("playwright");
  (async () => {
    const browser = await chromium.launch();
    const errors = [];
    const output = process.env.CODEX_CHAT_QA_DIR || require("node:os").tmpdir();
    try {
      await testComposerExpand(browser, output, errors);
      assert.deepEqual(errors, []);
      console.log("browser_composer_expand PASS");
    } finally {
      await browser.close();
    }
  })().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
