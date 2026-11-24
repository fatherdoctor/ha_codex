// Check slash-prefixed messages in the message box against web_fixture.py.
const assert = require("node:assert/strict");

const SCREENS = [
  ["phone", { width: 390, height: 844 }, true],
  ["small-phone", { width: 320, height: 568 }, true],
  ["ipad", { width: 820, height: 1180 }, true],
  ["desktop", { width: 1440, height: 950 }, false],
];

/** A slash is ordinary message text in both new and existing chats. */
async function testSlash(browser, errors) {
  for (const [name, viewport, touch] of SCREENS) {
    const context = await browser.newContext({
      viewport,
      hasTouch: touch,
      isMobile: touch && viewport.width < 800,
    });
    try {
      const page = await context.newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      await page.goto("http://127.0.0.1:9137/preview/");
      await page.locator(".chat-row").first().waitFor({ state: "attached" });
      await page.locator("#new-chat").evaluate((button) => button.click());
      if (await page.locator("#scrim").isVisible()) await page.locator("#scrim").click();
      const message = page.locator("#message");
      await message.pressSequentially("/");
      assert.equal(await page.locator("#slash-popover").isHidden(), true, name);
      assert.equal(await message.getAttribute("aria-autocomplete"), null);
      await message.pressSequentially("new");
      await message.press("ArrowDown");
      assert.equal(await message.getAttribute("aria-activedescendant"), null);
      const [created] = await Promise.all([
        page.waitForResponse((r) => r.request().method() === "POST" && r.url().endsWith("/tasks")),
        viewport.width <= 700 ? page.locator("#send").click() : message.press("Enter"),
      ]);
      assert.equal(created.request().postDataJSON().prompt, "/new");
      await page.locator("#messages .answer .turn-meta").last().waitFor();

      await message.fill("/usage");
      assert.equal(await page.locator("#slash-popover").isHidden(), true, name);
      const [continued] = await Promise.all([
        page.waitForResponse((r) => r.request().method() === "POST" && r.url().endsWith("/continue")),
        page.locator("#send").click(),
      ]);
      assert.equal(continued.request().postDataJSON().message, "/usage");
    } finally {
      await context.close();
    }
  }
  await testNoFastTier(browser);
}

/**
 * When the chat's model has no Fast tier (the worker sends fast: null), the
 * lightning toggle is disabled with a plain tooltip and the chip shows no bolt.
 */
async function testNoFastTier(browser) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 950 } });
  try {
    const page = await context.newPage();
    await page.route("**/chat-options", async (route) => {
      const response = await route.fetch();
      const catalog = await response.json();
      for (const model of catalog.models) model.fast = null;
      catalog.default_fast = null;
      await route.fulfill({ response, json: catalog });
    });
    await page.goto("http://127.0.0.1:9137/preview/");
    await page.locator(".chat-row").first().waitFor({ state: "attached" });
    await page.locator("#new-chat").evaluate((el) => el.click());
    await page.locator("#picker-button:not([disabled])").waitFor();
    await page.locator("#picker-button").click();
    await page.locator("#effort-popover").waitFor();
    assert.equal(await page.locator("#effort-fast").isDisabled(), true);
    assert.equal(
      await page.locator("#effort-fast").getAttribute("title"),
      "Fast mode is not available for this model",
    );
    assert.equal(await page.locator("#picker-fast").isHidden(), true);
  } finally {
    await context.close();
  }
}

module.exports = testSlash;

if (require.main === module) {
  const { chromium } = require("playwright");
  (async () => {
    const browser = await chromium.launch();
    const errors = [];
    try {
      await testSlash(browser, errors);
      assert.deepEqual(errors, []);
      console.log("browser_slash PASS");
    } finally {
      await browser.close();
    }
  })().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
