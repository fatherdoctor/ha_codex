// The live "Thinking:" line shows Codex's newest reasoning summary while a turn runs (CX-THINK-LINE).
// Called from browser_smoke.cjs against web_fixture.py. The chat's live steps are replaced in the browser with
// invented reasoning and command steps. At 320 and 1280 px:
// 1. A running reasoning step with text shows as one line, its line breaks folded into spaces.
// 2. An update of that step replaces the line; a newer reasoning step takes its place.
// 3. The steps line keeps its count and its elapsed time.
// 4. The line sits under the step list, so with the list open the newest thought is next to the newest step.
// 5. The line is gone once the turn ends, and absent while no reasoning step has text.
// 6. While the turn runs, the steps line and the Thinking line have vertical room; a finished list keeps its own.
const assert = require("node:assert/strict");

const WIDTHS = [320, 1280];
const CHAT = "preview-00";
const PAGE = "http://127.0.0.1:9137/preview/";

/** Replace the chat's live steps with the given ones and draw the activity block again. */
async function showSteps(page, steps, running = true) {
  await page.evaluate(
    ({ steps, running }) => {
      clearTimeout(activityTimer);
      activityTimer = null;
      state.task.status = running ? "running" : "completed";
      state.latestChecks = null;
      const activity = state.activity?.id === state.id ? state.activity : null;
      state.activity = {
        id: state.id,
        turnId: "example",
        seq: steps.length,
        steps: new Map(steps.map((step) => [step.index, step])),
        running,
        startedAt: activity?.startedAt ?? Date.now() - 3000,
        expanded: activity?.expanded ?? false,
        open: new Set(),
      };
      renderActivity();
    },
    { steps, running },
  );
}

/** Return one invented step in the shape the worker sends. */
function step(index, kind, text, status = "running") {
  return { index, kind, text, status };
}

/** Return the text of the Thinking line, or null when the page shows none. */
async function thinkingText(page) {
  const line = page.locator(".activity-thinking");
  return (await line.count()) ? line.textContent() : null;
}

async function checkWidth(page, label) {
  await page.goto(PAGE);
  await page.waitForFunction((id) => state.id === id && state.task, CHAT);
  await page.locator("#activity").waitFor({ state: "attached" });

  await showSteps(page, [step(1, "reasoning", "Reviewing the\ninput")]);
  assert.equal(await thinkingText(page), "Thinking: Reviewing the input", `${label}: line breaks fold into spaces`);
  assert.match(await page.locator(".activity-toggle").textContent(), /1 step.*3s/, `${label}: count and time stay`);
  const pad = (selector) => page.locator(selector).evaluate((el) => getComputedStyle(el).paddingBottom);
  assert.equal(await pad(".activity-toggle"), "8px", `${label}: the running steps line has room`);
  assert.equal(await pad(".activity-thinking"), "10px", `${label}: the Thinking line has room under it`);

  await showSteps(page, [step(1, "reasoning", "Checking the results")]);
  assert.equal(await thinkingText(page), "Thinking: Checking the results", `${label}: an update replaces the line`);

  await showSteps(page, [step(1, "reasoning", "Checking the results", "done"), step(2, "reasoning", "Latest summary")]);
  assert.equal(await thinkingText(page), "Thinking: Latest summary", `${label}: the newest summary wins`);
  assert.equal(await page.locator(".activity-thinking").count(), 1, `${label}: one line only`);
  assert.match(await page.locator(".activity-toggle").textContent(), /2 steps/, `${label}: the count follows`);

  await page.locator(".activity-toggle").click();
  assert.equal(await page.locator("#activity-steps").isVisible(), true, `${label}: the list opens`);
  assert.ok(
    await page.evaluate(() => document.querySelector("#activity-steps").nextElementSibling?.classList.contains("activity-thinking")),
    `${label}: the line sits under the open step list`,
  );

  await showSteps(page, [step(1, "reasoning", "Latest summary", "done")], false);
  assert.equal(await thinkingText(page), null, `${label}: gone once the turn ends`);
  assert.equal(await pad(".activity-toggle"), "4px", `${label}: a finished list keeps its spacing`);

  await showSteps(page, [step(3, "command", "Checking")]);
  assert.equal(await thinkingText(page), null, `${label}: absent without a summary`);
}

module.exports = async function checkThinking(browser, output, errors) {
  for (const width of WIDTHS) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript((id) => localStorage.setItem("codex-last-chat", id), CHAT);
    await checkWidth(page, `thinking line ${width} px`);
    await page.close();
  }
};
