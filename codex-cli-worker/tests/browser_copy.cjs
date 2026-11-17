// Run against web_fixture.py with Playwright available on NODE_PATH.
// Answers have no Copy response button (removed in 0.3.4). The thread menu's
// Copy thread ID still copies, through the same clipboard code the button used.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");

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
      permissions: ["clipboard-read", "clipboard-write"],
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

    // Copy thread ID puts the chat's native thread ID on the clipboard.
    const task = await page.evaluate(async () =>
      (await (await fetch("tasks/preview-04")).json()).task,
    );
    assert.ok(task.session_id, "the saved example has a native thread ID");
    await page.evaluate(() => navigator.clipboard.writeText(""));
    await page.locator("#thread-actions").click();
    await page.getByRole("menuitem", { name: "Copy thread ID" }).click();
    await page.waitForFunction(
      async (id) => (await navigator.clipboard.readText()) === id,
      task.session_id,
    );
    assert.deepEqual(errors, []);
    console.log(
      "PASS: answers have no Copy response button; Copy thread ID copies the thread ID.",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
