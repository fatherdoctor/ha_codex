// Exercise the slash commands in the message box against web_fixture.py.
const assert = require("node:assert/strict");
const path = require("node:path");

const SCREENS = [
  ["phone", { width: 390, height: 844 }, true],
  ["small-phone", { width: 320, height: 568 }, true],
  ["ipad", { width: 820, height: 1180 }, true],
  ["desktop", { width: 1440, height: 950 }, false],
];

/** Return the command names the open list shows, in order. */
function listed(page) {
  return page.locator("#slash-popover .slash-name").allTextContents();
}

/** Type into an empty message box the way a person does, one key at a time. */
async function typeMessage(page, text) {
  await page.locator("#message").fill("");
  await page.locator("#message").pressSequentially(text);
}

/** True when the element lies wholly inside the window. */
function onScreen(page, selector) {
  return page.locator(selector).evaluate((el) => {
    const box = el.getBoundingClientRect();
    return (
      box.left >= 0 &&
      box.top >= 0 &&
      box.right <= innerWidth + 0.5 &&
      box.bottom <= innerHeight + 0.5
    );
  });
}

/**
 * Check the command list: "/" opens it, typing filters it, a tap and Enter
 * run the picked command and clear the box, Escape closes it, commands the
 * page cannot run are left out, a message that is exactly a command is run
 * and not sent, other text that starts with "/" is left alone, and the list
 * fits the screen at every text size.
 */
async function testSlash(browser, output, errors) {
  for (const [name, viewport, touch] of SCREENS) {
    const context = await browser.newContext({
      viewport,
      hasTouch: touch,
      isMobile: touch && viewport.width < 800,
    });
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    const sent = [];
    page.on("request", (request) => {
      if (
        request.method() === "POST" &&
        /\/tasks(\/[^/]+\/continue)?$/.test(request.url())
      )
        sent.push(request.url());
    });
    await page.goto("http://127.0.0.1:9137/preview/");
    await page.locator(".chat-row").first().waitFor({ state: "attached" });
    if (await page.locator("#scrim").isVisible())
      await page.locator("#scrim").click();
    await page.locator("#new-chat").evaluate((el) => el.click());
    await page.locator("#message").waitFor();

    // A new chat: no chat to rename and nothing working to stop.
    await typeMessage(page, "/");
    await page.locator("#slash-popover").waitFor();
    const all = await listed(page);
    assert.deepEqual(all, [
      "/new",
      "/model",
      "/reasoning",
      "/usage",
      "/size",
      "/connection",
      "/settings",
    ]);
    assert.equal(
      await page.locator("#message").getAttribute("aria-expanded"),
      "true",
    );
    // Typing filters; an alias finds its command.
    await page.locator("#message").pressSequentially("s");
    assert.deepEqual(await listed(page), ["/usage", "/size", "/settings"]);
    await page.locator("#message").pressSequentially("i");
    assert.deepEqual(await listed(page), ["/size"]);
    await typeMessage(page, "/stat");
    assert.deepEqual(await listed(page), ["/usage"]);
    // /connection opens the Remote connection dialog.
    await typeMessage(page, "/conn");
    assert.deepEqual(await listed(page), ["/connection"]);
    await page.locator("#message").press("Enter");
    await page.locator("#connection-dialog[open]").waitFor();
    await page.keyboard.press("Escape");
    // No match, or a space after the word, closes the list.
    await typeMessage(page, "/zz");
    assert.equal(await page.locator("#slash-popover").isHidden(), true);
    await typeMessage(page, "/size please");
    assert.equal(await page.locator("#slash-popover").isHidden(), true);
    await typeMessage(page, "please /size");
    assert.equal(await page.locator("#slash-popover").isHidden(), true);

    // Escape closes the list until the text changes.
    await typeMessage(page, "/");
    await page.locator("#message").press("Escape");
    assert.equal(await page.locator("#slash-popover").isHidden(), true);
    assert.equal(await page.locator("#message").inputValue(), "/");
    await page.locator("#message").pressSequentially("se");
    assert.deepEqual(await listed(page), ["/settings"]);
    // The typed letters are marked, and every row has an icon.
    assert.equal(await page.locator("#slash-popover .slash-match").textContent(), "se");
    assert.notEqual(await page.locator("#slash-popover .slash-icon").textContent(), "");
    assert.equal(await page.locator("#slash-popover .slash-icon").getAttribute("aria-hidden"), "true");

    // The tap path: tap /size, the box empties and Settings opens at Text size, on every screen.
    await typeMessage(page, "/si");
    if (touch) await page.locator("#slash-size").tap();
    else await page.locator("#slash-size").click();
    assert.equal(await page.locator("#message").inputValue(), "");
    assert.equal(await page.locator("#slash-popover").isHidden(), true);
    await page.locator("#settings").waitFor();
    const choice = page.locator("#text-size-section").getByLabel("Largest");
    await choice.waitFor();
    await choice.check();
    await page.locator("#close-settings").click();

    // At Largest the list still fits the screen with no sideways scroll.
    await typeMessage(page, "/");
    await page.locator("#slash-popover").waitFor();
    assert(
      await onScreen(page, "#slash-popover"),
      `${name}: list off screen at Largest`,
    );
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      `${name}: sideways scroll with the list open`,
    );
    const rowHeight = await page
      .locator(".slash-option")
      .first()
      .evaluate((el) => el.getBoundingClientRect().height);
    assert(rowHeight >= 44, `${name}: command rows are too small to tap`);
    if (touch)
      await page.screenshot({
        path: path.join(output, `slash-${name}-largest.png`),
        animations: "disabled",
      });

    // The keyboard path: arrows move the highlight, Enter runs it.
    await page.locator("#message").press("ArrowDown");
    assert.equal(
      await page.locator("#message").getAttribute("aria-activedescendant"),
      "slash-model",
    );
    await page.locator("#message").press("ArrowUp");
    await page.locator("#message").press("ArrowUp");
    assert.equal(
      await page.locator('.slash-option[aria-selected="true"] .slash-name').textContent(),
      "/settings",
    );
    await page.locator("#message").press("Enter");
    await page.locator("#settings").waitFor();
    assert.equal(await page.locator("#message").inputValue(), "");
    // Settings has no Commands section: the list lives only in the message box.
    assert.equal(await page.locator("#settings-commands, #command-list").count(), 0);
    assert.equal(await page.locator("#settings").getByText("Commands", { exact: true }).count(), 0);
    const sizeToggle = page.locator("#text-size-section .settings-toggle");
    if ((await sizeToggle.getAttribute("aria-expanded")) !== "true") await sizeToggle.click();
    await page.locator("#settings").getByLabel("Normal").check();
    await page.locator("#close-settings").click();

    // Tab also runs the highlighted command.
    await typeMessage(page, "/mod");
    await page.locator("#message").press("Tab");
    await page.locator("#model-popover").waitFor();
    await page.locator("#chat-picker").press("Escape");

    // A message that is exactly a command is run, not sent to Codex.
    await page.locator("#message").fill("/reasoning");
    await page.locator("#message").press("Escape");
    await page.locator("#send").click();
    await page.locator("#effort-popover").waitFor();
    await page.locator("#chat-picker").press("Escape");
    assert.equal(await page.locator("#message").inputValue(), "");
    assert.deepEqual(sent, [], `${name}: a command was sent as a message`);

    // With a chat open, /rename is listed and opens the rename dialog.
    // Open the first chat in the list: chats that earlier checks in the same
    // fixture run start push older ones, such as preview-01, off the first page,
    // and the page lists /rename only for a chat it has in its list.
    await page.locator(".chat-row .row-main").first().evaluate((el) => el.click());
    await page.locator("#messages .message").first().waitFor();
    await typeMessage(page, "/re");
    assert.deepEqual(await listed(page), ["/reasoning", "/rename"]);
    await page.locator("#message").press("ArrowDown");
    await page.locator("#message").press("Enter");
    await page.locator("#rename-dialog").waitFor();
    await page.locator("#rename-cancel").click();

    // The chat commands are listed with a chat open, and a press outside closes the list.
    await typeMessage(page, "/");
    assert.deepEqual(await listed(page), [
      "/new",
      "/model",
      "/reasoning",
      "/rename",
      "/usage",
      "/size",
      "/connection",
      "/settings",
      "/pin",
      "/share",
      "/archive",
      "/delete",
    ]);
    await typeMessage(page, "/sh");
    assert.deepEqual(await listed(page), ["/share"]);
    assert.equal(await page.locator("#slash-popover .slash-match").textContent(), "sh");
    await page.locator("#messages").click({ position: { x: 5, y: 5 }, force: true });
    assert.equal(await page.locator("#slash-popover").isHidden(), true);
    await page.locator("#message").fill("");

    // While a chat works, /stop is listed and stops it.
    if (name === "phone" || name === "desktop") {
      await page.locator("#new-chat").evaluate((el) => el.click());
      await page.locator("#message").fill("Take your time with this one");
      await page.locator("#send").click();
      await page.locator("#cancel").waitFor();
      sent.length = 0;
      await typeMessage(page, "/st");
      assert.deepEqual(await listed(page), ["/stop", "/usage"]);
      await page.locator("#message").press("Enter");
      await page.locator("#cancel").waitFor({ state: "hidden" });
      // The fixture holds the stopped run's thread until it is released.
      await page.evaluate(() => fetch("fixture/release", { method: "POST" }));
      await typeMessage(page, "/st");
      assert.deepEqual(await listed(page), ["/usage"]);
      assert.deepEqual(sent, [], `${name}: /stop was sent as a message`);
    }
    await context.close();
  }
}

module.exports = testSlash;

if (require.main === module) {
  const { chromium } = require("playwright");
  (async () => {
    const browser = await chromium.launch();
    const errors = [];
    const output = process.env.CODEX_CHAT_QA_DIR || require("node:os").tmpdir();
    try {
      await testSlash(browser, output, errors);
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
