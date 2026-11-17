// Check the chat flow of the 0.3.5 restyle against web_fixture.py: the "Worked for" line, the checks inside it,
// the failed-check line, answered permission requests as one closed line, and the column and bubble sizes.
// The page runs on its own here, with no Home Assistant parent window.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");

const base = "http://127.0.0.1:9137/preview/";

/** Return the top of an element in page coordinates. */
async function top(locator) {
  return (await locator.boundingBox()).y;
}

/** A finished live exchange: the closed "Worked for" line sits above the answer and opens the steps. */
async function checkLiveExchange(page) {
  await page.locator("#new-chat").click();
  await page.locator("#message").fill("Check the hallway lights");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await page.waitForFunction(() =>
    /^Worked for \d+s$/.test(document.querySelector("#activity .activity-toggle")?.textContent || ""),
  );
  const toggle = page.locator("#activity > .activity-toggle");
  assert.equal(await toggle.getAttribute("aria-expanded"), "false", "the line is closed when the exchange ends");
  assert.equal(await page.locator("#activity-steps").isVisible(), false);
  const answer = page.locator(".exchange").last().locator(".answer");
  assert.ok((await top(toggle)) < (await top(answer)), "the line is above the answer");
  assert.equal(await page.locator(".answer #activity").count(), 0, "the line is not inside the answer");
  assert.equal(await toggle.evaluate(node => getComputedStyle(node).fontSize), "13px");
  await toggle.click();
  assert.equal(await page.locator("#activity-steps").isVisible(), true);
  assert.ok((await page.locator("#activity .step").count()) > 0);
  // The old wording is gone.
  assert.equal(await page.getByText(/Show activity|Hide activity/).count(), 0);
}

/** The chat column, the prompt box and the owner's bubble use the desktop app's sizes. */
async function checkSizes(page) {
  const sizes = await page.evaluate(() => {
    const area = document.getElementById("messages");
    const style = getComputedStyle(area);
    const column = area.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    const compose = document.querySelector(".compose-area");
    const composeStyle = getComputedStyle(compose);
    const prompt = compose.clientWidth - parseFloat(composeStyle.paddingLeft) - parseFloat(composeStyle.paddingRight);
    const bubble = getComputedStyle(document.querySelector(".message.user")).maxWidth;
    return { column, prompt, bubble, scroll: document.documentElement.scrollWidth <= innerWidth };
  });
  assert.ok(Math.abs(sizes.column - 720) <= 1, `chat column ${sizes.column}`);
  assert.ok(Math.abs(sizes.prompt - sizes.column) <= 1, `prompt box ${sizes.prompt} vs column ${sizes.column}`);
  assert.equal(sizes.bubble, "70%");
  assert.equal(sizes.scroll, true);
}

/** On a phone the bubble keeps its wider limit and nothing scrolls sideways. */
async function checkPhone(browser) {
  const phone = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await phone.emulateMedia({ colorScheme: "dark" });
  await phone.goto(base);
  await phone.locator(".chat-row").first().waitFor();
  await phone.locator('[data-task-id="preview-00"]').evaluate(node => node.click());
  await phone.locator(".message.user").first().waitFor();
  assert.equal(await phone.locator(".message.user").first().evaluate(node => getComputedStyle(node).maxWidth), "94%");
  assert.equal(await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await phone.close();
}

/** An allowed request shrinks to one closed line that opens to the same details. */
async function checkAnsweredApproval(page, output) {
  await page.locator("#new-chat").click();
  await page.locator("#message").fill("Request permission to read sensor history");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await page.getByRole("button", { name: "Allow once", exact: true }).waitFor();
  const pending = page.locator(".permission-card");
  assert.equal(await pending.evaluate(node => node.tagName), "SECTION", "a waiting request is the full card");
  assert.equal(await page.locator(".permission-plain").first().isVisible(), true);
  await page.getByRole("button", { name: "Allow once", exact: true }).click();
  const card = page.locator("details.permission-card.is-answered");
  await card.waitFor();
  assert.equal(await card.evaluate(node => node.open), false);
  const summary = card.locator("summary");
  assert.match(await summary.textContent(), /^Allowed once: \S/);
  assert.ok((await summary.boundingBox()).height <= 28, "one line");
  assert.equal(await card.locator(".permission-plain").first().isVisible(), false);
  assert.equal(await card.locator(".permission-command").isVisible(), false);
  await summary.click();
  assert.equal(await card.locator(".permission-plain").first().isVisible(), true);
  assert.equal(await card.locator(".permission-command").isVisible(), true);
  await page.screenshot({ path: path.join(output, "fb2-flow-approval.png"), fullPage: true });
}

/**
 * An earlier answer gets its own "Worked for" line with its checks inside, and a failed check keeps its line
 * under that answer. The chat is invented and drawn straight from the page's state.
 */
async function checkEarlierAnswer(page) {
  await page.evaluate(() => {
    const failed = { check_id: "c1", status: "failed", entity_id: "sensor.sample_power", message: "Sample failure" };
    const passed = { check_id: "c2", status: "passed", entity_id: "sensor.sample_energy" };
    state.task = {
      ...state.task,
      title: "Invented chat",
      status: "completed",
      queued_message: null,
      turns: [
        {
          turn_id: "t1", message: "First question", status: "completed", summary: "First answer",
          created_at: "2026-01-01T10:00:00+00:00", started_at: "2026-01-01T10:00:05+00:00",
          completed_at: "2026-01-01T10:01:26+00:00", verification: [failed, passed],
          approvals: [{ approval_id: "a1", status: "denied", command: "rm /config/sample.yaml\nsecond line", scope: "Sample scope" }],
        },
        { turn_id: "t2", message: "Second question", status: "completed", summary: "Second answer",
          started_at: "2026-01-01T10:02:00+00:00", completed_at: "2026-01-01T10:02:09+00:00" },
        { turn_id: "t3", message: "Third question", status: "completed", summary: "Third answer" },
        // The latest exchange draws its line from the open chat's steps, which is checked above.
        { turn_id: "t4", message: "Fourth question", status: "completed", summary: "Fourth answer" },
      ],
    };
    renderTask(true);
  });
  const first = page.locator(".exchange").nth(0);
  const worked = first.locator(".activity.worked > .activity-toggle");
  assert.equal(await worked.textContent(), "Worked for 1m 21s");
  assert.equal(await worked.getAttribute("aria-expanded"), "false");
  assert.ok((await top(worked)) < (await top(first.locator(".answer"))));
  assert.equal(await first.locator(".verification").isVisible(), false);
  // The denied request is one closed line with the command's first line only.
  assert.equal(await first.locator("details.permission-card summary").textContent(), "Denied: rm /config/sample.yaml");
  const failedLine = first.locator(".answer .check-failed-line");
  assert.equal(await failedLine.textContent(), "1 check failed");
  await failedLine.click();
  assert.equal(await worked.getAttribute("aria-expanded"), "true");
  assert.equal(await first.locator(".verification").evaluate(node => node.open), true);
  assert.equal(await first.locator(".verification-result").first().isVisible(), true);
  // The owner's choice stays when the chat is drawn again.
  await page.evaluate(() => renderTask(true));
  assert.equal(await first.locator(".activity.worked > .activity-toggle").getAttribute("aria-expanded"), "true");
  await first.locator(".activity.worked > .activity-toggle").click();
  assert.equal(await first.locator(".activity.worked .activity-steps").isVisible(), false);
  // An answer with no checks has the time as plain text; with no times either, nothing.
  assert.equal(await page.locator(".exchange").nth(1).locator(".worked-line").textContent(), "Worked for 9s");
  assert.equal(await page.locator(".exchange").nth(1).locator(".check-failed-line").count(), 0);
  assert.equal(await page.locator(".exchange").nth(2).locator(".worked-line, .activity-toggle").count(), 0);
}

/** Run the checks in a light desktop page, then a dark phone page. */
async function main() {
  const output = process.env.CODEX_CHAT_QA_DIR || path.join(os.tmpdir(), "codex-chat-qa");
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CODEX_CHAT_BROWSER ? { channel: process.env.CODEX_CHAT_BROWSER } : {}),
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 950 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(base);
    await page.locator(".chat-row").first().waitFor();
    await checkLiveExchange(page);
    await checkSizes(page);
    await page.screenshot({ path: path.join(output, "fb2-flow-desktop.png"), fullPage: true });
    await checkAnsweredApproval(page, output);
    await checkEarlierAnswer(page);
    await checkPhone(browser);
    assert.deepEqual(errors, []);
    console.log(
      "PASS: Worked for line above the answer, checks inside it, failed-check line, answered requests as one closed line, 720 px column and 70 % bubble, phone in dark mode.",
    );
  } finally {
    await browser.close();
  }
}
main().catch(error => {
  console.error(error);
  process.exit(1);
});
