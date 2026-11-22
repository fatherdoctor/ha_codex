// The text messages Codex writes along the way (narration) stay out of the final answer (CX-BLEED-FIX).
// Called from browser_smoke.cjs against web_fixture.py. The chat's saved record and steps are replaced in the
// browser with invented transcripts shaped like four owner reports:
//   steps-between: narration, tool steps, narration, a step, then an answer of three paragraphs;
//   one-note: one narration line, a search, then an answer whose last paragraph reads like narration;
//   two-turns: a light turned off in an earlier exchange and read back in the latest one, both with narration;
//   leading-space: an answer whose streamed text starts with a space.
// At 320, 390, 700 and 1280 px:
// 1. While a turn runs, its narration shows outside the closed step list.
// 2. Once it is over, nothing shows between the closed "Worked for" line and the answer, and the answer is whole.
// 3. With the list open, the narration shows inside it, in order, between the step groups.
// 4. The first character of every narration and answer paragraph starts at the same x, list open or closed.
const assert = require("node:assert/strict");
const path = require("node:path");

const WIDTHS = [320, 390, 700, 1280];
const CHAT = "preview-00";
const EARLIER_TURN = "0123456789abcdef0123456789abcdef";
const PAGE = "http://127.0.0.1:9137/preview/";

const SHAPES = {
  "steps-between": {
    steps: [
      ["message", "I'll check the visitor counts against the official records, then build and check the workbook."],
      ["web_search", "Searched: sample visitor statistics"],
      ["command", "ls /config/www"],
      ["message", "The ranking holds. I will chart both totals and the changes."],
      ["file_change", "Edited /config/www/sample.xlsx"],
    ],
    summary: "**Park C grew fastest.** The workbook has both years, the change as formulas, and a chart.",
    details: "Sources: [sample statistics](https://example.com/stats).\n\nThe change uses the first year as its base.",
  },
  "one-note": {
    steps: [
      ["message", "I'll look for the research skill and search for a city that published crash counts."],
      ["web_search", "Searched: sample roundabout crash data"],
    ],
    summary: "**A sample city saw fewer reported crashes per year** after the change. [City figures](https://example.com/a)",
    details: "The city chose the change after a history of side crashes.\n\nThe research skill was not available, so I read the sources directly.",
  },
  "two-turns": {
    earlier: {
      steps: [
        ["message", "I will turn off the sample light."],
        ["tool_call", "home_assistant · call_service"],
      ],
      summary: "The sample light is off.",
    },
    steps: [
      ["message", "I will read the light's state back."],
      ["tool_call", "home_assistant · get_state"],
      ["message", "The state read back as off."],
    ],
    summary: "The sample light is still off.",
    details: "",
  },
  "leading-space": {
    steps: [
      ["message", "I'll use the docs skill to confirm the model name."],
      ["command", "cat /data/sample-model.txt"],
    ],
    summary: " I'm running on a sample model. I know only its name.",
    details: "",
  },
};

/** Return activity steps in the worker's shape, ending with the saved copy of the answer the worker keeps. */
function activitySteps(steps, summary) {
  const rows = steps.map(([kind, text], index) => ({
    index, seq: index + 1, id: `item_${index}`, kind, text, status: "done", duration_ms: null,
  }));
  if (summary) rows.push({ index: rows.length, seq: rows.length + 1, id: "saved-answer", kind: "message", text: summary, status: "done" });
  return rows;
}

/** Serve the chat as one shaped transcript: the record, the latest steps and, for two turns, the earlier steps. */
async function serveShape(page, name, running = false) {
  const shape = SHAPES[name];
  await page.unrouteAll({ behavior: "ignoreErrors" });
  const record = await (await page.request.get(`${PAGE}tasks/${CHAT}`)).json();
  const latest = record.task.turns.at(-1);
  Object.assign(latest, {
    summary: running ? "" : shape.summary, details: running ? "" : shape.details, question: "",
    status: running ? "running" : "completed",
    started_at: "2026-11-20T18:00:00+00:00", completed_at: running ? null : "2026-11-20T18:00:33+00:00",
  });
  record.task.turns = [latest];
  if (shape.earlier) {
    record.task.turns.unshift({
      ...latest, turn_id: EARLIER_TURN, message: "Turn off the sample light.", summary: shape.earlier.summary,
      details: "", status: "completed", verification: [], approvals: [], attachments: [],
      started_at: "2026-11-20T17:58:00+00:00", completed_at: "2026-11-20T17:58:12+00:00",
    });
  }
  Object.assign(record.task, { status: latest.status, summary: latest.summary, details: latest.details, question: "" });
  const steps = activitySteps(shape.steps, running ? "" : shape.summary);
  const earlier = shape.earlier ? activitySteps(shape.earlier.steps, shape.earlier.summary) : [];
  await page.route(new RegExp(`tasks/${CHAT}$`), (route) => route.fulfill({ json: record }));
  await page.route(new RegExp(`tasks/${CHAT}/activity\\?after=`), (route) =>
    route.fulfill({ json: { ok: true, running, seq: steps.length, steps, total: steps.length, turn_id: latest.turn_id } }));
  await page.route(new RegExp(`tasks/${CHAT}/activity\\?turn=`), (route) =>
    route.fulfill({ json: { ok: true, running: false, seq: earlier.length, steps: earlier, total: earlier.length, turn_id: EARLIER_TURN } }));
}

/** Open the chat afresh, so the page reads the shaped record and steps. */
async function openChat(page) {
  await page.goto(PAGE);
  await page.locator("#activity .activity-toggle").waitFor();
}

/**
 * In one exchange, return each shown narration and answer paragraph: its first character's left x, whether it is
 * narration, and its first words. Leading spaces do not count as the first character.
 */
function paragraphs(exchange) {
  const blocks = [...exchange.querySelectorAll(".message.interim p, .answer > .message p")];
  return blocks.filter((block) => block.getClientRects().length).map((block) => {
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, {
      acceptNode: (text) => (text.data.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
    });
    const text = walker.nextNode();
    const start = text.data.search(/\S/);
    const range = document.createRange();
    range.setStart(text, start);
    range.setEnd(text, start + 1);
    return { left: range.getBoundingClientRect().left, narration: Boolean(block.closest(".interim")), words: text.data.trim().slice(0, 24) };
  });
}

/** Assert that every paragraph of the list starts at the same x as the first one, within half a pixel. */
function assertAligned(list, label) {
  for (const item of list)
    assert.ok(Math.abs(item.left - list[0].left) <= 0.5,
      `${label}: "${item.words}" starts at ${item.left} px, "${list[0].words}" at ${list[0].left} px`);
}

/** The latest exchange of a finished shaped chat, with its "Worked for" list closed and then open. */
async function checkFinished(page, name, label) {
  const shape = SHAPES[name];
  await serveShape(page, name);
  await openChat(page);
  const exchange = page.locator("#messages .exchange").last();
  const narration = shape.steps.filter(([kind]) => kind === "message").map(([, text]) => text);
  assert.equal(await page.locator("#activity .activity-toggle").getAttribute("aria-expanded"), "false", `${label}: the list starts closed`);
  assert.equal(await page.locator("#activity-text .interim").count(), 0, `${label}: no narration outside the list`);
  const closed = await exchange.evaluate(paragraphs);
  assert.ok(closed.every((item) => !item.narration), `${label}: no narration shows with the list closed`);
  // The answer starts right under the closed line: the element after the work log is the answer.
  assert.equal(await page.evaluate(() => {
    let next = document.getElementById("activity").nextElementSibling;
    while (next && !next.getClientRects().length) next = next.nextElementSibling;
    return next?.className;
  }), "answer", `${label}: the answer follows the closed line`);
  const answer = await exchange.locator(".answer").innerText();
  assert.ok(answer.includes(shape.summary.trim().replace(/\*\*/g, "").slice(0, 20)), `${label}: the answer shows its first paragraph`);
  for (const text of narration) assert.ok(!answer.includes(text.slice(0, 20)), `${label}: narration is not part of the answer`);
  assertAligned(closed, `${label} closed`);
  await page.locator("#activity .activity-toggle").click();
  const inside = await page.locator("#activity-steps > .message.interim").allInnerTexts();
  assert.deepEqual(inside.map((text) => text.trim()), narration, `${label}: the narration shows inside the list, in order`);
  assert.equal(await page.locator("#activity-steps > .step-group > .step").count(),
    shape.steps.filter(([kind]) => kind !== "message").length, `${label}: every step shows in a group`);
  const open = await exchange.evaluate(paragraphs);
  assert.equal(open.filter((item) => item.narration).length, narration.length, `${label}: open list, narration shows`);
  assertAligned(open, `${label} open`);
  return exchange;
}

/** The earlier exchange of the two-turn chat: its own narration opens inside its own list, aligned the same way. */
async function checkEarlier(page, label) {
  const earlier = page.locator("#messages .exchange").first();
  assert.equal(await earlier.locator(".interim").count(), 0, `${label}: the earlier list starts closed and empty`);
  await earlier.locator(".activity-toggle").click();
  await earlier.locator(".activity-steps > .message.interim").first().waitFor();
  assert.deepEqual((await earlier.locator(".activity-steps > .message.interim").allInnerTexts()).map((text) => text.trim()),
    [SHAPES["two-turns"].earlier.steps[0][1]], `${label}: the earlier narration shows in its list`);
  assert.ok(!(await earlier.locator(".activity-steps").innerText()).includes(SHAPES["two-turns"].earlier.summary),
    `${label}: the earlier answer is not repeated in its list`);
  assertAligned(await earlier.evaluate(paragraphs), `${label} earlier open`);
}

/** While the turn runs, the narration so far shows outside the closed list, so the owner can follow along. */
async function checkRunning(page, label) {
  await serveShape(page, "steps-between", true);
  await openChat(page);
  await page.locator("#activity-text .interim").nth(1).waitFor();
  assert.equal(await page.locator("#activity-steps").isVisible(), false, `${label}: the running list is closed`);
  assert.deepEqual((await page.locator("#activity-text .interim").allInnerTexts()).map((text) => text.trim()),
    SHAPES["steps-between"].steps.filter(([kind]) => kind === "message").map(([, text]) => text),
    `${label}: running narration shows outside the list`);
}

module.exports = async function checkAnswerBleed(browser, output, errors) {
  for (const width of WIDTHS) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript((id) => localStorage.setItem("codex-last-chat", id), CHAT);
    const label = `answer bleed ${width} px`;
    await checkRunning(page, label);
    for (const name of Object.keys(SHAPES)) {
      const exchange = await checkFinished(page, name, `${label} ${name}`);
      if (name === "two-turns") await checkEarlier(page, `${label} ${name}`);
      if (name === "steps-between") await exchange.screenshot({ path: path.join(output, `answer-bleed-open-${width}.png`) });
    }
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await page.close();
  }
};
