// Check that a download link saves through the share sheet inside the Home Assistant app (CX-MOBILE-DL), against
// web_fixture.py. The app's web view ignores downloads, so a tap there shares the file; a browser still downloads.
const assert = require("node:assert/strict");
const path = require("node:path");

// An invented user agent in the shape of the Home Assistant app on an iPad.
const APP_AGENT =
  "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 " +
  "Home Assistant/2026.11 (io.example.HomeAssistant; build:1; iOS 18.0.0) Mobile/HomeAssistant, like Safari";

/**
 * Stand in for the web view's share sheet. `window.shareMode` picks what share() does: "ok" records the files,
 * "refuse-once" throws NotAllowedError (a tap too old) the first time, "cannot" makes canShare say no.
 */
function stubShare() {
  window.shareMode = "ok";
  window.shared = [];
  navigator.canShare = (data) => window.shareMode !== "cannot" && Array.isArray(data?.files);
  navigator.share = async ({ files }) => {
    if (window.shareMode === "refuse-once") {
      window.shareMode = "ok";
      throw new DOMException("The request is not allowed.", "NotAllowedError");
    }
    for (const file of files)
      window.shared.push({ name: file.name, type: file.type, head: Array.from(new Uint8Array(await file.arrayBuffer()).slice(0, 4)) });
  };
}

/** Return the Download link of the fixture's generated image (other steps of the smoke run add more images). */
function imageLink(page, text) {
  return page.locator("#messages figure.attachment", { hasText: "codex-image-5f1d3c9e.png" })
    .locator("figcaption a", text ? { hasText: text } : {});
}

/** Open a new page in the given context, at the chat page, and wait for the chat list. */
async function openPage(context, errors) {
  const page = await context.newPage();
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("http://127.0.0.1:9137/preview/");
  await page.locator(".chat-row").first().waitFor({ state: "attached" });
  return page;
}

/** In the app: the image's Download link and a chat file card share the file with its name and a real type. */
async function testInApp(browser, output, errors) {
  const context = await browser.newContext({
    viewport: { width: 820, height: 1180 }, hasTouch: true, isMobile: true, userAgent: APP_AGENT,
  });
  await context.addInitScript(stubShare);
  const page = await openPage(context, errors);
  await page.locator('[data-task-id="preview-02"]').click();
  const download = imageLink(page);
  assert.equal(await download.textContent(), "Download");
  await download.click();
  await page.waitForFunction(() => window.shared.length === 1);
  assert.deepEqual(await page.evaluate(() => window.shared[0]),
    { name: "codex-image-5f1d3c9e.png", type: "image/png", head: [0x89, 0x50, 0x4e, 0x47] });
  assert.equal(page.url(), "http://127.0.0.1:9137/preview/"); // The tap did not leave the chat.

  // A tap that the web view finds too old: the link asks for a second tap, which shares the fetched file.
  await page.evaluate(() => { window.shareMode = "refuse-once"; });
  await download.click();
  await imageLink(page, "Tap again to save").waitFor();
  await download.click();
  await page.waitForFunction(() => window.shared.length === 2);
  assert.equal(await download.textContent(), "Download");
  assert.equal(await page.locator("#error").isHidden(), true);

  // A chat file: the files route answers application/octet-stream; the shared file is typed by its name or kept
  // as octet-stream, never refused.
  await page.getByRole("button", { name: "New chat", exact: false }).click();
  await page.locator("#message").fill("Make a spreadsheet of park visits.");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  const card = page.locator("#messages .answer .document-attachment");
  await card.waitFor();
  await card.click();
  await page.waitForFunction(() => window.shared.length === 3);
  assert.deepEqual(await page.evaluate(() => window.shared[2]),
    { name: "park visits.xlsx", type: "application/octet-stream", head: [0x50, 0x4b, 0x03, 0x04] });

  // No share sheet for this file: the page says what to do instead of failing in silence.
  await page.evaluate(() => { window.shareMode = "cannot"; });
  await card.click();
  await page.locator("#error", { hasText: "Open Home Assistant in a web browser" }).waitFor();
  await page.screenshot({ path: path.join(output, "app-save-ipad.png"), animations: "disabled" });
  await context.close();
}

/** In a desktop browser that also has a share sheet, the Download link still downloads and never shares. */
async function testInBrowser(browser, errors) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 950 }, acceptDownloads: true });
  await context.addInitScript(stubShare);
  const page = await openPage(context, errors);
  await page.locator('[data-task-id="preview-02"]').click();
  const link = imageLink(page, "Download");
  const [file] = await Promise.all([page.waitForEvent("download"), link.click()]);
  assert.equal(file.suggestedFilename(), "codex-image-5f1d3c9e.png");
  assert.deepEqual(await page.evaluate(() => window.shared), []);
  await context.close();
}

/** Run both checks. */
async function testAppSave(browser, output, errors) {
  await testInApp(browser, output, errors);
  await testInBrowser(browser, errors);
}

module.exports = testAppSave;

if (require.main === module) {
  const { chromium } = require("playwright");
  (async () => {
    const browser = await chromium.launch();
    const errors = [];
    const output = process.env.CODEX_CHAT_QA_DIR || require("node:os").tmpdir();
    try {
      await testAppSave(browser, output, errors);
      assert.deepEqual(errors, []);
      console.log("browser_app_save PASS");
    } finally {
      await browser.close();
    }
  })().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
