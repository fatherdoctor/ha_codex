// Run against web_fixture.py with Playwright available on NODE_PATH.
// Answers have no Copy response button (removed in 0.3.4). A chat's menu lists
// Rename, Pin, Share, Schedule, Archive and Delete (Schedule since 0.3.6), the
// conversation header has no menu of its own, and Share downloads the chat as
// Markdown, whole or its last lines, with the steps Codex printed.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const base = "http://127.0.0.1:9137/preview/";

/** Open the saved example chat, loading older chats until its row shows. */
async function openSavedChat(page) {
  await page.goto(base);
  await page.locator(".chat-row").first().waitFor();
  // The smoke suite can leave many newer chats ahead of this saved example.
  while (!(await page.locator('[data-task-id="preview-04"]').count())) {
    const count = await page.locator(".chat-row").count();
    await page.getByRole("button", { name: "Load older chats" }).click();
    await page.waitForFunction(
      (count) => document.querySelectorAll(".chat-row").length > count,
      count,
    );
  }
  await page.locator('[data-task-id="preview-04"]').click();
  await page.locator(".answer").first().waitFor();
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({
      acceptDownloads: true,
      viewport: { width: 1440, height: 1000 },
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await openSavedChat(page);

    // No answer offers to copy itself, and the manual copy dialog is gone.
    assert.equal(
      await page.getByRole("button", { name: "Copy response" }).count(),
      0,
    );
    assert.equal(await page.locator(".response-actions").count(), 0);
    assert.equal(await page.locator("#copy-dialog").count(), 0);

    // The conversation header has no menu; the chat's menu opens from its row.
    assert.equal(await page.locator("#thread-actions, #phone-thread-actions").count(), 0);
    await page.locator('[data-menu-for="preview-04"]').click();
    const items = await page.locator("#chat-menu [role=menuitem]").evaluateAll((nodes) =>
      nodes.filter((node) => node.offsetParent !== null).map((node) => node.textContent.trim()),
    );
    // Schedule is in the menu only while scheduled runs are on (the fixture starts with them off).
    const scheduled = process.env.CODEX_FIXTURE_SCHEDULED_RUNS === "1" ? ["Schedule"] : [];
    assert.deepEqual(items, ["Rename", "Pin chat", "Share", ...scheduled, "Archive", "Delete"]);
    for (const gone of ["New chat", "Conversations", "Copy thread ID", "Status"])
      assert.equal(await page.locator("#chat-menu").getByRole("menuitem", { name: gone, exact: true }).count(), 0, gone);

    // Share, whole chat: a Markdown file with the message, the steps and the answer.
    await page.getByRole("menuitem", { name: "Share", exact: true }).click();
    assert.equal(await page.locator("#share-dialog").evaluate((node) => node.open), true);
    let download = page.waitForEvent("download");
    await page.locator("#share-download").click();
    let file = await download;
    assert.equal(file.suggestedFilename(), "codex-chat-preview-04.md");
    const whole = fs.readFileSync(await file.path(), "utf8");
    assert.match(whole, /^# /);
    assert.match(whole, /## You/);
    assert.match(whole, /## Codex/);
    assert.equal(await page.locator("#share-dialog").evaluate((node) => node.open), false);

    // Share, last lines: typing a number picks that choice, and the file is the end of the whole one.
    await page.locator('[data-menu-for="preview-04"]').click();
    await page.getByRole("menuitem", { name: "Share", exact: true }).click();
    await page.locator("#share-lines").fill("3");
    assert.equal(await page.locator('input[name="share-range"][value="last"]').isChecked(), true);
    download = page.waitForEvent("download");
    await page.locator("#share-download").click();
    file = await download;
    const last = fs.readFileSync(await file.path(), "utf8");
    assert.equal(last, whole.trimEnd().split("\n").slice(-3).join("\n") + "\n");

    // A bad number stays in the dialog with a reason and downloads nothing.
    await page.locator('[data-menu-for="preview-04"]').click();
    await page.getByRole("menuitem", { name: "Share", exact: true }).click();
    await page.locator("#share-lines").fill("0");
    await page.locator("#share-download").click();
    assert.equal(await page.locator("#share-error").textContent(), "Enter a number of lines from 1 to 100000.");
    await page.locator("#share-cancel").click();
    assert.deepEqual(errors, []);
    console.log(
      "PASS: answers have no Copy response button; the chat menu is Rename, Pin, Share, Schedule, Archive, Delete; Share downloads the chat.",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
