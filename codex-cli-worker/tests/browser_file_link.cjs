// Check that a link to a file Codex saved in the chat's files folder downloads it (CX-FILE-LINK), against
// web_fixture.py. The fixture answers "Make a spreadsheet ..." with such a link and links that must stay text.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// The desktop asks for the workbook; the phone then reopens that chat as the last one used.
const SCREENS = [
  ["desktop", { width: 1440, height: 950 }, false],
  ["phone", { width: 390, height: 844 }, true],
];

/** Send one message in a new chat and wait for the fixture's answer with its file card. */
async function askForSpreadsheet(page) {
  await page.getByRole("button", { name: "New chat", exact: false }).click();
  await page.locator("#message").fill("Make a spreadsheet of park visits.");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await page.locator("#messages .answer .document-attachment").waitFor();
}

/**
 * The linked workbook shows as one card with its name and size and downloads its bytes; links to a file outside
 * the folder, a hidden file, a missing file and a path that climbs out keep their label only; the worker refuses
 * those paths and any other chat's folder when asked directly.
 */
async function testFileLink(browser, output, errors) {
  let chatId = "";
  for (const [name, viewport, touch] of SCREENS) {
    const context = await browser.newContext({
      viewport, hasTouch: touch, isMobile: touch, acceptDownloads: true,
    });
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    if (chatId) await page.addInitScript((id) => localStorage.setItem("codex-last-chat", id), chatId);
    await page.goto("http://127.0.0.1:9137/preview/");
    await page.locator(".chat-row").first().waitFor({ state: "attached" });
    if (chatId) await page.locator("#messages .answer .document-attachment").waitFor();
    else await askForSpreadsheet(page);
    const answer = page.locator("#messages .answer").last();
    const cards = answer.locator(".document-attachment");
    assert.equal(await cards.count(), 1);
    assert.equal(await cards.textContent(), "park visits.xlsx · 1 KB ↓");
    assert.equal(await cards.getAttribute("download"), "park visits.xlsx");
    const href = await cards.getAttribute("href");
    assert.match(href, /^tasks\/[^/]+\/files\/2025\/park%20visits\.xlsx$/);
    // The other four links are text, not links.
    assert.equal(await answer.locator("a").count(), 1);
    assert.match(await answer.textContent(), /Not offered: secrets, hidden, missing and outside\./);
    const [download] = await Promise.all([page.waitForEvent("download"), cards.click()]);
    assert.equal(download.suggestedFilename(), "park visits.xlsx");
    assert.equal(fs.readFileSync(await download.path(), "latin1"), "PK\x03\x04 invented workbook");
    const taskPath = href.replace(/\/files\/.*$/, "");
    chatId = decodeURIComponent(taskPath.split("/")[1]);
    const statuses = await page.evaluate(async (base) => {
      const status = async (url) => (await fetch(url)).status;
      return [
        await status(`${base}/files/2025/park%20visits.xlsx`),
        await status(`${base}/files/.notes.txt`),
        await status(`${base}/files/missing.xlsx`),
        await status(`${base}/files/2025/..%2F..%2Ftask.json`),
        await status(`${base}/files/%2E%2E/task.json`),
        await status("tasks/preview-01/files/2025/park%20visits.xlsx"),
      ];
    }, taskPath);
    assert.deepEqual(statuses, [200, 404, 404, 404, 404, 404]);
    const headers = await page.evaluate(async (url) => {
      const response = await fetch(url);
      return [response.headers.get("content-type"), response.headers.get("content-disposition"),
        response.headers.get("x-content-type-options")];
    }, href);
    assert.equal(headers[0], "application/octet-stream");
    assert.match(headers[1], /^attachment;/);
    assert.equal(headers[2], "nosniff");
    await page.screenshot({ path: path.join(output, `file-link-${name}.png`), animations: "disabled" });
    await context.close();
  }
}

module.exports = testFileLink;

if (require.main === module) {
  const { chromium } = require("playwright");
  (async () => {
    const browser = await chromium.launch();
    const errors = [];
    const output = process.env.CODEX_CHAT_QA_DIR || require("node:os").tmpdir();
    try {
      await testFileLink(browser, output, errors);
      assert.deepEqual(errors, []);
      console.log("browser_file_link PASS");
    } finally {
      await browser.close();
    }
  })().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}
