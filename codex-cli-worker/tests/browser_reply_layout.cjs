// Reply geometry for transcript-shaped examples, kept apart from the shared chat fixture (CX-REPLY-FIXTURES).
// Called from browser_smoke.cjs with its browser, against web_fixture.py, like browser_slash.cjs. By hand:
//   python tests/web_fixture.py            (in one terminal)
//   REPLY_LAYOUT_CAPTURE=/some/folder node tests/browser_reply_layout.cjs
// Environment variables, all optional: REPLY_LAYOUT_BASE (the preview URL, default http://127.0.0.1:9137/preview/),
// REPLY_LAYOUT_CAPTURE (a folder; one image of each fixture at each width is written there),
// REPLY_LAYOUT_MUTATE_GAP=1 (adds a 24 px gap under each narration block; the check must then fail).
// For each fixture at 320, 390, 700 and 1280 px, with the "Worked for" list open:
// 1. The number of narration blocks in the list equals the length of the fixture's `interim`.
// 2. Every narration block and the answer start at the column edge and end inside the window.
// 3. Narration blocks that follow each other, and the paragraphs of the answer, are one paragraph gap apart.
// 4. The completion line and the work line start at the column edge; the message column does not scroll sideways.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const fixtures = require("./fixtures/reply-layout/transcripts.json");

const WIDTHS = [320, 390, 700, 1280];
const BASE = process.env.REPLY_LAYOUT_BASE || "http://127.0.0.1:9137/preview/";
// Two blocks may differ from a paragraph gap by this much (rounding of the layout).
const GAP_TOLERANCE = 2;

/** Put the fixture in the open chat as a finished exchange with its narration inside the open step list. */
async function showFixture(page, fixture) {
  await page.evaluate((data) => {
    clearTimeout(activityTimer);
    activityTimer = null;
    const turn = (id, message, summary) => ({
      turn_id: id, message, summary, status: "completed",
      created_at: "2026-01-01T12:00:00Z", started_at: "2026-01-01T12:00:00Z", completed_at: "2026-01-01T12:00:09Z",
    });
    const turns = [];
    if (data.earlier) turns.push(turn("earlier", data.earlier.message, data.earlier.summary));
    turns.push(turn("fixture", data.prompt, data.answer));
    state.task = { ...state.task, status: "completed", turns };
    const steps = new Map([[0, { index: 0, kind: "reasoning", status: "done", text: "Checking the request" }]]);
    data.interim.forEach((text, n) =>
      steps.set(n + 1, { index: n + 1, kind: "message", status: "done", phase: "commentary", text }));
    state.activity = { id: state.id, turnId: "fixture", seq: steps.size, steps, running: false,
      startedAt: null, loaded: true, loading: false, expanded: true, open: new Set() };
    renderTask(true);
  }, fixture);
}

/** One normal paragraph gap in pixels, taken from the page's own Markdown rules. The one copy of this sample; the
 * smoke test reads it from here. */
async function paragraphGap(page) {
  return page.evaluate(() => {
    const sample = document.createElement("div");
    sample.className = "message markdown";
    sample.style.cssText = "position: fixed; visibility: hidden; top: 0; left: 0";
    sample.innerHTML = "<p>First paragraph.</p><p>Second paragraph.</p>";
    document.body.append(sample);
    const gap = sample.children[1].getBoundingClientRect().top - sample.children[0].getBoundingClientRect().bottom;
    sample.remove();
    return gap;
  });
}

/** In the page: the box of each narration block and of the answer, and the other parts the check reads. */
function measureExchange(node, referenceGap) {
  const area = node.closest("#messages");
  const rect = area.getBoundingClientRect();
  const firstX = (element) => {
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT, {
      acceptNode: (text) => (text.data.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
    });
    const text = walker.nextNode();
    const range = document.createRange();
    const start = text.data.search(/\S/);
    range.setStart(text, start);
    range.setEnd(text, start + 1);
    return range.getBoundingClientRect().left;
  };
  const box = (block) => {
    const bounds = block.getBoundingClientRect();
    return { left: firstX(block), top: bounds.top, bottom: bounds.bottom, right: bounds.right };
  };
  return {
    columnLeft: rect.left + parseFloat(getComputedStyle(area).paddingLeft),
    viewportRight: Math.min(rect.right, innerWidth),
    narration: [...node.querySelectorAll("#activity-steps > .message.interim")].map(box),
    answer: box(node.querySelector(".answer > .message")),
    // Only paragraphs that follow each other directly: a table or list between two is not a paragraph gap.
    paragraphGaps: [...node.querySelectorAll(".answer > .message > p + p")].map((p) =>
      p.getBoundingClientRect().top - p.previousElementSibling.getBoundingClientRect().bottom),
    referenceGap,
    metaLeft: firstX(node.querySelector(".turn-meta")),
    workedLeft: node.querySelector(".activity-toggle").getBoundingClientRect().left,
    exchanges: area.querySelectorAll(".exchange").length,
    scrolled: area.scrollLeft,
    wider: area.scrollWidth > area.clientWidth,
  };
}

/** The failures of one measured exchange, as sentences; an empty list means it passes. */
function judge(measured, fixture) {
  const failures = [];
  const blocks = [...measured.narration, measured.answer];
  if (measured.narration.length !== fixture.interim.length)
    failures.push(`${measured.narration.length} narration blocks, the fixture has ${fixture.interim.length}`);
  blocks.forEach((block, i) => {
    if (Math.abs(block.left - measured.columnLeft) > 1)
      failures.push(`block ${i} starts ${Math.round(block.left - measured.columnLeft)}px from the column edge`);
    if (block.right > measured.viewportRight + 1) failures.push(`block ${i} extends past the viewport`);
  });
  // Step groups can sit between narration blocks; only blocks with nothing between them are compared. The fixtures
  // have no step after their first one, so every neighbor pair in the list is such a pair.
  measured.narration.slice(1).forEach((block, i) => {
    const gap = block.top - measured.narration[i].bottom;
    if (Math.abs(gap - measured.referenceGap) > GAP_TOLERANCE)
      failures.push(`gap before narration block ${i + 1} is ${Math.round(gap)}px; paragraph gap is ${Math.round(measured.referenceGap)}px`);
  });
  measured.paragraphGaps.forEach((gap, i) => {
    if (Math.abs(gap - measured.referenceGap) > GAP_TOLERANCE)
      failures.push(`answer paragraph gap ${i + 1} is ${Math.round(gap)}px; paragraph gap is ${Math.round(measured.referenceGap)}px`);
  });
  if (Math.abs(measured.metaLeft - measured.columnLeft) > 1) failures.push("completion line is indented");
  if (Math.abs(measured.workedLeft - measured.columnLeft) > 1) failures.push("work line is indented");
  if (fixture.earlier && measured.exchanges !== 2) failures.push("the earlier exchange is missing");
  if (measured.scrolled || measured.wider) failures.push("message column scrolls sideways");
  return failures;
}

/** Check every fixture at every width. Throws one error that lists all failures. */
module.exports = async function checkReplyLayout(browser, output, errors = [], base = BASE) {
  const failures = [];
  for (const width of WIDTHS) {
    const page = await browser.newPage({ viewport: { width, height: 2000 } });
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript((id) => localStorage.setItem("codex-last-chat", id), "preview-00");
    await page.goto(base);
    if (process.env.REPLY_LAYOUT_MUTATE_GAP === "1")
      await page.addStyleTag({ content: "#activity-steps > .message.interim { margin-bottom: 24px !important; }" });
    await page.locator("#activity .activity-toggle").waitFor();
    const referenceGap = await paragraphGap(page);
    for (const fixture of fixtures) {
      await showFixture(page, fixture);
      const exchange = page.locator("#messages .exchange").last();
      const measured = await exchange.evaluate(measureExchange, referenceGap);
      for (const failure of judge(measured, fixture)) failures.push(`item ${fixture.id} at ${width}px: ${failure}`);
      if (output) {
        fs.mkdirSync(output, { recursive: true });
        await exchange.screenshot({ path: path.join(output, `${fixture.id}-${width}.png`), animations: "disabled" });
      }
    }
    await page.close();
  }
  assert.deepEqual(failures, [], failures.join("\n"));
  assert.equal(fixtures.length, 5);
};

module.exports.paragraphGap = paragraphGap;

if (require.main === module) {
  const { chromium } = require("playwright");
  (async () => {
    const executablePath = process.env.REPLY_LAYOUT_CHROMIUM;
    const browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
    try {
      await module.exports(browser, process.env.REPLY_LAYOUT_CAPTURE);
      console.log("browser_reply_layout PASS: five reply fixtures at 320, 390, 700 and 1280px.");
    } finally {
      await browser.close();
    }
  })().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
