// Run against web_fixture.py with Playwright available on NODE_PATH.
// Start a fresh fixture for each browser suite; suites change its conversation state.
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const zlib = require("node:zlib");

/** Build a tiny valid PNG so the upload checks need no binary fixture. */
function pngBuffer(width = 8, height = 6) {
  const table = [...Array(256)].map((_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  /** Return the CRC-32 checksum that a PNG chunk carries. */
  const crc = (buf) => {
    let c = 0xffffffff;
    for (const b of buf) c = table[(c ^ b) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  /** Build one PNG chunk: length, tag, data, and checksum. */
  const chunk = (tag, data) => {
    const len = Buffer.alloc(4);
    len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(tag), data]);
    const sum = Buffer.alloc(4);
    sum.writeUInt32BE(crc(body));
    return Buffer.concat([len, body, sum]);
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 2;
  const row = Buffer.concat([Buffer.from([0]), Buffer.alloc(width * 3, 0x80)]);
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", zlib.deflateSync(Buffer.concat(Array(height).fill(row)))),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/** Open the model and reasoning panel from the chip (CX-PICKER-PARITY). */
async function openPicker(page) {
  await page.locator("#picker-button").click();
  await page.locator("#effort-popover").waitFor();
}
/** The model row in the picker's model list whose name is label. */
function modelRow(page, label) {
  return page
    .locator("#model-options .model-option")
    .filter({ has: page.getByText(label, { exact: true }) });
}
/**
 * Choose a model the way the desktop app does: the chip opens the panel, the
 * model name in the panel opens the list, and a model returns to the panel.
 * Returns once the choice is saved.
 */
async function chooseModel(page, label) {
  if (await page.locator("#effort-popover").isHidden()) await openPicker(page);
  await page.locator("#effort-model").click();
  await page.locator("#model-popover").waitFor();
  await modelRow(page, label).click();
  await page.locator("#effort-popover").waitFor();
  await page.waitForFunction(
    (label) =>
      document.querySelector("#picker-model").textContent === label &&
      !document.querySelector("#picker-button").disabled,
    label,
  );
}
/** Wait until the chip shows the reasoning level label and the picker is ready again. */
async function effortSaved(page, label) {
  await page.waitForFunction(
    (label) =>
      document.querySelector("#effort-label").textContent === label &&
      !document.querySelector("#picker-button").disabled,
    label,
  );
}

(async () => {
  const output =
    process.env.CODEX_CHAT_QA_DIR || path.join(os.tmpdir(), "codex-chat-qa");
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CODEX_CHAT_BROWSER
      ? { channel: process.env.CODEX_CHAT_BROWSER }
      : {}),
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 950 },
    });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const requested = [];
    page.on("request", (request) => requested.push(request.url()));
    await page.goto("http://127.0.0.1:9137/preview/");
    await page.locator(".chat-row").first().waitFor();
    assert.equal(await page.locator(".chat-row").count(), 20);
    // The empty message box is one line of text above the control row.
    assert.equal(await page.locator("#message").getAttribute("rows"), "1");
    const composerHeight = await page.locator("#composer").evaluate((node) => node.getBoundingClientRect().height);
    assert.ok(composerHeight < 120, `empty composer is ${composerHeight} px high`);
    await page.getByRole("button", { name: "Load older chats" }).click();
    await page.waitForFunction(
      () => document.querySelectorAll(".chat-row").length === 25,
    );
    // The quota left is a bar next to its figure: green, and red below 5%.
    await page.waitForFunction(
      () => document.querySelector("#usage-weekly").textContent === "3% left",
    );
    assert.deepEqual(
      await page.evaluate(() =>
        ["usage-five-hour", "usage-weekly"].map((id) => {
          const fill = document.getElementById(`${id}-bar`);
          return [
            document.getElementById(id).textContent,
            fill.style.width,
            fill.parentElement.className,
            getComputedStyle(fill).backgroundColor,
            fill.parentElement.title,
          ];
        }),
      ),
      [
        ["64% left", "64%", "usage-bar", "rgb(12, 163, 12)", "Resets 19:20"],
        [
          "3% left",
          "3%",
          "usage-bar low",
          "rgb(208, 59, 59)",
          "Resets 12:00 on 8 Oct",
        ],
      ],
    );
    // An account can lack either limit, or both. In a good read, a limit the
    // account does not report reads Unknown; a bad value or a failed read reads
    // Unavailable. Both show an empty outline, and the other limit keeps its bar.
    /**
     * Render a quota payload in the page and return, for each limit, the text
     * shown, the bar width, and the bar's classes.
     */
    const quotaRows = (usage) =>
      page.evaluate((value) => {
        renderUsage(value);
        return ["usage-five-hour", "usage-weekly"].map((id) => {
          const fill = document.getElementById(`${id}-bar`);
          return [
            document.getElementById(id).textContent,
            fill.style.width,
            fill.parentElement.className,
          ];
        });
      }, usage);
    const missing = ["Unavailable", "0px", "usage-bar unknown"];
    const unreported = ["Unknown", "0px", "usage-bar unknown"];
    assert.deepEqual(await quotaRows({ status: "ok", weekly_percent: "87" }), [
      unreported,
      ["87% left", "87%", "usage-bar"],
    ]);
    assert.equal(await page.locator("#usage-note").isHidden(), true);
    assert.deepEqual(
      await quotaRows({ status: "ok", five_hour_percent: "2", weekly_percent: "" }),
      [["2% left", "2%", "usage-bar low"], unreported],
    );
    // Neither limit, values that are not percentages, and no quota data at all.
    for (const usage of [
      {},
      { status: "error", five_hour_percent: "n/a", weekly_percent: 140 },
      { status: "ok", five_hour_percent: null, weekly_percent: -1 },
      null,
    ]) {
      assert.deepEqual(await quotaRows(usage), [
        usage?.status === "ok" ? unreported : missing,
        missing,
      ]);
      assert.equal(
        await page.locator("#usage-note").textContent(),
        usage?.status === "ok" ? "" : "Quota is currently unavailable.",
      );
    }
    await page.evaluate(() => loadUsage());
    await page.waitForFunction(
      () => document.querySelector("#usage-weekly").textContent === "3% left",
    );
    // The line about the Codex integration is hidden while the integration is
    // connected, and says in one sentence what to do in every other state.
    const notice = page.locator("#integration-notice");
    assert.equal(await notice.isHidden(), true);
    const minimum = await page.evaluate(
      async () => (await api("status")).integration.minimum_version,
    );
    assert.match(minimum, /^\d+(\.\d+)+$/);
    /**
     * Have the fixture report the given state of the integration, read the
     * status again, and wait until the line shows the given text. An empty
     * text waits for the line to be hidden.
     */
    const integrationLine = async (target, state, text) => {
      await target.evaluate(async (value) => {
        await fetch("fixture/integration", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ state: value }),
        });
        // A read that is already under way may still carry the previous state.
        while (usageLoading) await new Promise((done) => setTimeout(done, 20));
        await loadUsage();
      }, state);
      await target.waitForFunction((expected) => {
        const line = document.getElementById("integration-notice");
        return expected
          ? !line.hidden && line.textContent === expected
          : line.hidden && line.textContent === "";
      }, text);
    };
    const integrationLines = {
      not_installed:
        "Some features are not available because the Codex integration is not installed. How to install it",
      outdated: `Some features are not available because the Codex integration is out of date. Update it to ${minimum} or newer, then restart Home Assistant.`,
      not_connected:
        "Some features are not available because the Codex integration is installed but not connected. Restart Home Assistant, then add Codex under Settings > Devices & services.",
    };
    for (const [state, text] of Object.entries(integrationLines)) {
      await integrationLine(page, state, text);
      // Only a missing integration gets the link to the installation steps.
      assert.equal(
        await notice.locator("a").count(),
        state === "not_installed" ? 1 : 0,
      );
    }
    // A worker that has just started, and a state this page does not know, show nothing.
    await integrationLine(page, "starting", "");
    await integrationLine(page, "not_installed", integrationLines.not_installed);
    await integrationLine(page, "something_new", "");
    await integrationLine(page, "not_installed", integrationLines.not_installed);
    assert.deepEqual(
      await notice
        .locator("a")
        .evaluate((link) => [link.href, link.target, link.rel]),
      [
        "https://github.com/fatherdoctor/ha_codex#installation",
        "_blank",
        "noreferrer noopener",
      ],
    );
    // The same state read again leaves the line, and its link, as they are.
    assert.equal(
      await page.evaluate((version) => {
        const line = document.getElementById("integration-notice");
        const link = line.querySelector("a");
        renderIntegration({ state: "not_installed", minimum_version: version });
        return line.querySelector("a") === link;
      }, minimum),
      true,
    );
    /**
     * Scroll the messages to the end and check that the line still fills the
     * row between the chat header and the messages, without widening the page.
     */
    const integrationLineStaysOnTop = (target) =>
      target.evaluate(() => {
        const messages = document.getElementById("messages");
        messages.scrollTop = messages.scrollHeight;
        // The chat column; a phone hides the title row and shows its slim row instead.
        const header = document.querySelector("main").getBoundingClientRect();
        const line = document
          .getElementById("integration-notice")
          .getBoundingClientRect();
        const list = messages.getBoundingClientRect();
        return (
          Math.abs(line.top - (document.querySelector(".phone-thread-tools").offsetHeight ? document.querySelector(".phone-thread-tools") : document.querySelector(".chat-header")).getBoundingClientRect().bottom) < 1 &&
          Math.abs(line.bottom - list.top) < 1 &&
          Math.abs(line.left - header.left) < 1 &&
          Math.abs(line.width - header.width) < 1 &&
          line.height > 0 &&
          document.documentElement.scrollWidth <= innerWidth
        );
      });
    assert.equal(await integrationLineStaysOnTop(page), true);
    await page.screenshot({
      path: path.join(output, "integration-notice.png"),
      animations: "disabled",
    });
    await integrationLine(page, "ok", "");
    // The answer to an older chat options request does not replace a newer one.
    assert.equal(
      await page.evaluate(async () => {
        const request = api;
        const answers = [];
        api = (path, ...rest) =>
          path === "chat-options"
            ? new Promise((resolve) => answers.push(resolve))
            : request(path, ...rest);
        const older = loadChatOptions(true);
        const newer = loadChatOptions(true);
        answers[1]({ ...state.catalog, default_model: "newer" });
        answers[0]({ ...state.catalog, default_model: "older" });
        await Promise.all([older, newer]);
        const kept = state.catalog.default_model;
        api = request;
        await loadChatOptions(true);
        return kept;
      }),
      "newer",
    );
    // Chat actions: hovering a row reveals its menu button; pin, rename, delete.
    await page.locator('.chat-row:has([data-task-id="preview-05"])').hover();
    await page.locator('[data-menu-for="preview-05"]').click();
    await page.locator("#chat-menu").waitFor({ state: "visible" });
    assert.equal(
      await page.locator('#chat-menu [data-action="close"]').isVisible(),
      false,
    );
    await page.getByRole("menuitem", { name: "Pin chat" }).click();
    await page.waitForFunction(
      () =>
        document.querySelector(".list-group")?.textContent === "Pinned" &&
        document.querySelector(".row-main").dataset.taskId === "preview-05",
    );
    assert.equal(await page.locator(".pin-icon").count(), 1);
    // Keyboard: Shift+F10 opens the menu with its first item, Rename, focused;
    // arrows move, Enter activates.
    const focusedAction = () =>
      page.evaluate(() => document.activeElement.dataset.action);
    await page.locator('[data-task-id="preview-06"]').focus();
    await page.keyboard.press("Shift+F10");
    await page.locator("#chat-menu").waitFor({ state: "visible" });
    assert.equal(await focusedAction(), "rename");
    await page.keyboard.press("ArrowDown");
    assert.equal(await focusedAction(), "pin");
    await page.keyboard.press("ArrowUp");
    assert.equal(await focusedAction(), "rename");
    await page.keyboard.press("Enter");
    await page.locator("#rename-dialog").waitFor({ state: "visible" });
    await page.locator("#rename-input").fill("  Porch   lights ");
    await page.keyboard.press("Enter");
    await page.waitForFunction(
      () =>
        document.querySelector('[data-task-id="preview-06"] .row-title')
          .textContent === "Porch lights",
    );
    // Right-click opens the same menu; Delete asks for confirmation first.
    await page
      .locator('[data-task-id="preview-07"]')
      .click({ button: "right" });
    await page.getByRole("menuitem", { name: "Delete" }).click();
    await page.locator("#delete-dialog").waitFor({ state: "visible" });
    await page.locator("#delete-cancel").click();
    assert.equal(await page.locator(".chat-row").count(), 25);
    await page
      .locator('[data-task-id="preview-07"]')
      .click({ button: "right" });
    await page.getByRole("menuitem", { name: "Delete" }).click();
    await page.locator("#delete-confirm").click();
    await page.waitForFunction(
      () =>
        document.querySelectorAll(".chat-row").length === 24 &&
        !document.querySelector('[data-task-id="preview-07"]'),
    );
    const managed = await page.evaluate(async () => ({
      listing: (
        await (
          await fetch("tasks?summary=true&order=pinned_first&limit=3")
        ).json()
      ).tasks,
      deleted: (await fetch("tasks/preview-07")).status,
      renamed: (await (await fetch("tasks/preview-06")).json()).task.title,
    }));
    assert.equal(managed.listing[0].task_id, "preview-05");
    assert.equal(managed.listing[0].pinned, true);
    assert.equal(managed.deleted, 404);
    assert.equal(managed.renamed, "Porch lights");
    await page.locator('[data-task-id="preview-02"]').click();
    const image = page.locator("#messages img.attachment-image");
    await image.waitFor();
    assert.equal(await image.count(), 1);
    await page.waitForFunction(() => {
      const img = document.querySelector("#messages img.attachment-image");
      return img && img.complete && img.naturalWidth > 0;
    });
    assert.match(
      await image.getAttribute("src"),
      /\/preview\/tasks\/preview-02\/attachments\/[0-9a-f]{32}$/,
    );
    assert.match(
      await page
        .locator("#messages .attachment figcaption a")
        .getAttribute("href"),
      /\?download=1$/,
    );
    const served = await page.evaluate(async () => {
      const src = document.querySelector("#messages img.attachment-image").src;
      const ok = await fetch(src);
      const missing = await fetch(
        "tasks/preview-02/attachments/" + "0".repeat(32),
      );
      const download = await fetch(src + "?download=1");
      return {
        status: ok.status,
        type: ok.headers.get("content-type"),
        nosniff: ok.headers.get("x-content-type-options"),
        disposition: download.headers.get("content-disposition"),
        missing: missing.status,
      };
    });
    assert.equal(served.status, 200);
    assert.equal(served.type, "image/png");
    assert.equal(served.nosniff, "nosniff");
    assert.match(served.disposition, /^attachment;/);
    assert.equal(served.missing, 404);
    await page.screenshot({
      path: path.join(output, "generated-image.png"),
      fullPage: true,
      animations: "disabled",
    });
    // Markdown in a sent message and in the answer is shown formatted.
    await page.locator('[data-task-id="preview-04"]').click();
    const sentMarkdown = page.locator("#messages .message.user");
    await sentMarkdown.locator("pre").waitFor();
    assert.equal(
      await sentMarkdown.locator("pre code").textContent(),
      "trigger:\n  - platform: sun\n    event: sunset",
    );
    assert.equal(await sentMarkdown.locator(".md-lang").textContent(), "yaml");
    assert.equal(
      await sentMarkdown.locator("p code").textContent(),
      "automation.evening_lights",
    );
    assert.equal(
      await sentMarkdown.locator("strong").textContent(),
      "20 minutes before sunset",
    );
    const answerMarkdown = page.locator("#messages .answer");
    assert.equal(
      await answerMarkdown.locator(".message").first().textContent(),
      "Done. automation.evening_lights now starts 20 minutes before sunset.",
    );
    assert.equal(await answerMarkdown.locator("h4").textContent(), "What changed");
    assert.equal(await answerMarkdown.locator("h5").textContent(), "Next steps");
    assert.equal(await answerMarkdown.locator("ul > li").count(), 2);
    assert.equal(await answerMarkdown.locator("ol > li").count(), 2);
    assert.equal(await answerMarkdown.locator("em").textContent(), "scene");
    assert.match(
      await answerMarkdown.locator("pre code").textContent(),
      /^trigger:\n {2}- platform: sun\n[^]*now asks\."$/,
    );
    assert.deepEqual(await answerMarkdown.locator("th").allTextContents(), [
      "Check",
      "Result",
    ]);
    // A <br> breaks the line inside a table cell.
    assert.equal(await answerMarkdown.locator("td br").count(), 1);
    assert.equal(
      await answerMarkdown.locator("blockquote").textContent(),
      "A negative offset runs before the event, a positive one after it.",
    );
    const docsLink = answerMarkdown.locator("a");
    assert.equal(
      await docsLink.getAttribute("href"),
      "https://www.home-assistant.io/docs/automation/trigger/#sun-trigger",
    );
    assert.equal(await docsLink.getAttribute("target"), "_blank");
    assert.equal(await docsLink.getAttribute("rel"), "noreferrer noopener");
    // None of the Markdown symbols are left in the conversation or its preview.
    assert.doesNotMatch(
      await page.locator("#messages").textContent(),
      /```|\*\*|##|<br>|\]\(/,
    );
    assert.equal(
      await page
        .locator('[data-task-id="preview-04"] .row-preview')
        .textContent(),
      "Done. automation.evening_lights now starts 20 minutes before sunset.",
    );
    await page.setViewportSize({ width: 1440, height: 1400 });
    await page.screenshot({
      path: path.join(output, "markdown.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page.setViewportSize({ width: 1440, height: 950 });
    // Laundry schedule: preserve the equations and the Markdown around them.
    const inlineMath = String.raw`\(30 + 45 + 20 = 95\)`;
    const displayMath = String.raw`\[
15 + 95 = 110
\]`;
    const dollarMath = "$$15 + 95 = 110$$";
    const laundry = await page.evaluate(({ inlineMath, displayMath, dollarMath }) => {
      const answer = renderMarkdown([
        "Each load takes " + inlineMath + " minutes.",
        "**Earliest finish:**",
        displayMath,
        dollarMath,
        "The supply budget is $15.",
      ].join("\n\n"), "message");
      return {
        inline: [...answer.querySelectorAll("p > code")].map(node => node.textContent),
        blocks: [...answer.querySelectorAll("pre > code")].map(node => node.textContent),
        label: answer.querySelector("strong")?.textContent,
        lastLine: answer.lastElementChild.textContent,
      };
    }, { inlineMath, displayMath, dollarMath });
    assert.deepEqual(laundry, {
      inline: [inlineMath],
      blocks: [displayMath, dollarMath],
      label: "Earliest finish:",
      lastLine: "The supply budget is $15.",
    });
    // A message that would keep the parser busy, or is very long, stays plain text.
    assert.deepEqual(
      await page.evaluate(() => {
        const slow = renderMarkdown("**a ".repeat(12000), "message");
        const long = renderMarkdown("# Title\n" + "a".repeat(50000), "message");
        return [
          slow.className,
          slow.childElementCount,
          slow.textContent.length,
          long.className,
          long.childElementCount,
        ];
      }),
      ["message", 0, 48000, "message", 0],
    );
    // CX-REPLY-ALIGN: at phone and desktop widths a finished reply (its "Worked for" line, its text and its
    // "Completed" line) starts at the left edge of the message column, with no indent, and no part of it is cut off.
    await page.locator('[data-task-id="preview-01"]').click();
    await page.locator("#activity.worked .activity-toggle").waitFor();
    await page.locator("#messages .answer .turn-meta").waitFor();
    for (const width of [320, 390, 700, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      const reply = await page.evaluate(() => {
        const area = document.querySelector("#messages");
        const box = area.getBoundingClientRect();
        const style = getComputedStyle(area);
        const left = box.left + parseFloat(style.paddingLeft);
        const right = box.right - parseFloat(style.paddingRight);
        const exchange = [...area.querySelectorAll(".exchange")].at(-1);
        const parts = {
          worked: exchange.querySelector("#activity .activity-toggle"),
          text: exchange.querySelector(".answer > .message"),
          completed: exchange.querySelector(".answer > .turn-meta"),
        };
        return {
          left,
          right,
          windowRight: innerWidth,
          scrolled: area.scrollLeft,
          wider: area.scrollWidth > area.clientWidth,
          parts: Object.fromEntries(
            Object.entries(parts).map(([name, node]) => {
              const rect = node.getBoundingClientRect();
              // The reply text and the Completed line are measured at their first character, so an indent counts.
              let left = rect.left;
              if (name !== "worked") {
                const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT, {
                  acceptNode: (text) => (text.data.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
                });
                const text = walker.nextNode();
                const range = document.createRange();
                const start = text.data.search(/\S/);
                range.setStart(text, start);
                range.setEnd(text, start + 1);
                left = range.getBoundingClientRect().left;
              }
              return [name, { left, right: rect.right, cut: node.scrollWidth > node.clientWidth + 1 }];
            }),
          ),
        };
      });
      assert.equal(reply.scrolled, 0, `the messages scroll sideways at ${width} px`);
      assert.equal(reply.wider, false, `the messages are wider than their column at ${width} px`);
      for (const [name, part] of Object.entries(reply.parts)) {
        assert.ok(
          Math.abs(part.left - reply.left) <= 1,
          `${name} starts at ${part.left} px, the column at ${reply.left} px (${width} px window)`,
        );
        assert.ok(
          part.right <= reply.right + 0.5 && part.right <= reply.windowRight,
          `${name} ends at ${part.right} px, past the column at ${reply.right} px (${width} px window)`,
        );
        assert.equal(part.cut, false, `${name} is cut off at ${width} px`);
      }
      // The pulsing dot of the working line ("Working on your request… · 4 steps · 4s") sits on the middle of
      // the line, not on its text baseline. A copy of the line is measured; the real one is on screen only while Codex works.
      const line = await page.evaluate(() => {
        const block = document.createElement("div");
        block.className = "activity running";
        block.id = "dot-check";
        const toggle = document.createElement("button");
        toggle.className = "activity-toggle";
        const chevron = document.createElement("span");
        chevron.className = "activity-chevron";
        const label = document.createElement("span");
        label.textContent = "Working on your request… · 4 steps";
        const elapsed = document.createElement("span");
        elapsed.className = "elapsed";
        elapsed.textContent = " · 4s";
        label.append(elapsed);
        toggle.append(chevron, label);
        block.append(toggle);
        const area = document.querySelector("#messages");
        area.append(block);
        block.scrollIntoView({ block: "center" });
        const rect = (node) => {
          const r = node.getBoundingClientRect();
          return { x: r.left, y: r.top, width: r.width, height: r.height };
        };
        return { toggle: rect(toggle), chevron: rect(chevron), label: rect(label) };
      });
      // The dot is drawn by ::before, so find its rows in a picture of the chevron's column of the line.
      const shot = await page.screenshot({
        clip: { x: line.chevron.x, y: line.toggle.y, width: line.chevron.width, height: line.toggle.height },
        animations: "disabled",
      });
      const dotRows = await page.evaluate(async (data) => {
        document.querySelector("#dot-check").remove();
        const image = new Image();
        image.src = `data:image/png;base64,${data}`;
        await image.decode();
        const canvas = document.createElement("canvas");
        canvas.width = image.width;
        canvas.height = image.height;
        const context = canvas.getContext("2d");
        context.drawImage(image, 0, 0);
        const pixels = context.getImageData(0, 0, image.width, image.height).data;
        const background = pixels.slice(0, 3);
        const rows = [];
        for (let y = 0; y < image.height; y++)
          for (let x = 0; x < image.width; x++) {
            const at = (y * image.width + x) * 4;
            if ([0, 1, 2].some((c) => Math.abs(pixels[at + c] - background[c]) > 40)) {
              rows.push(y);
              break;
            }
          }
        return rows;
      }, shot.toString("base64"));
      assert.ok(dotRows.length >= 4, `the working dot is not drawn at ${width} px`);
      const dot = {
        dot: line.toggle.y + (dotRows[0] + dotRows.at(-1) + 1) / 2,
        line: line.label.y + line.label.height / 2,
      };
      assert.ok(
        Math.abs(dot.dot - dot.line) <= 1.5,
        `the working dot's middle is at ${dot.dot} px, the line's at ${dot.line} px (${width} px window)`,
      );
    }
    await page.setViewportSize({ width: 1440, height: 950 });
    await page.locator('[data-task-id="preview-00"]').click();
    await page
      .locator("#messages")
      .getByText("Your evening routine looks good.", { exact: false })
      .waitFor();
    // The numbered suggestions of this answer are a list.
    assert.equal(await page.locator("#messages .details ol > li").count(), 2);
    // A finished exchange keeps its steps behind a collapsed toggle. This saved chat has no start time,
    // so the line counts the steps instead of reading "Worked for" (CX-FB2-FLOW).
    const storedToggle = page.locator("#activity .activity-toggle");
    await storedToggle.waitFor();
    assert.equal(await storedToggle.textContent(), "Activity (3 steps)");
    assert.equal(await page.locator("#activity-steps").isVisible(), false);
    await storedToggle.click();
    assert.equal(await page.locator("#activity .step").count(), 3);
    assert.equal(
      await page.locator("#activity .step-command .step-text").textContent(),
      "cat /config/automations.yaml",
    );
    await page.getByRole("button", { name: "Show output" }).click();
    await page.locator("#activity .step-output").waitFor();
    await page.screenshot({
      path: path.join(output, "activity-finished.png"),
      fullPage: true,
    });
    await storedToggle.click();
    assert.equal(await page.locator("#activity-steps").isVisible(), false);
    // CX-PICKER-PARITY: one chip, the model name and then the level. It opens one panel above it:
    // the level in bold, the model name with a ">" under it, and a slider with one stop per level.
    assert.equal(await page.locator("#model-button, #effort-button").count(), 0);
    assert.equal(await page.locator("#chat-picker button.picker-pill").count(), 1);
    assert.equal(await page.locator("#picker-model").isVisible(), true);
    assert.equal(await page.locator("#effort-label").isVisible(), true);
    assert.equal(await page.locator("#picker-open-label").isVisible(), false);
    await openPicker(page);
    assert.equal(await page.locator("#picker-button").getAttribute("aria-expanded"), "true");
    // While the panel is open the chip reads "Select effort".
    assert.equal(await page.locator("#picker-open-label").isVisible(), true);
    assert.equal(await page.locator("#picker-model").isVisible(), false);
    assert.equal(await page.locator("#effort-label").isVisible(), false);
    assert.equal(await page.locator("#model-popover").isHidden(), true);
    assert.equal(
      await page.locator("#effort-title").evaluate((node) => Number(getComputedStyle(node).fontWeight) >= 600),
      true,
    );
    assert.equal(
      await page.locator("#effort-model-name").textContent(),
      await page.locator("#picker-model").textContent(),
    );
    assert.equal(await page.locator("#effort-model svg.effort-model-chevron").count(), 1);
    // No lightning (fast mode) toggle anywhere on the page, in the composer or the panel, until it
    // works: the Codex CLI has no setting for it (returns in 0.6, CX-04-FB-MODEL).
    assert.equal(
      await page.evaluate(() =>
        [...document.querySelectorAll(".effort-bolt, [class*='bolt'], [id*='bolt'], [id*='fast'], .mdi-lightning-bolt, .mdi-flash, path")]
          .filter((node) =>
            node.tagName.toLowerCase() !== "path" ||
            /^m13 3-8 10h6l-1 8 9-11h-6l1-7z$/i.test(node.getAttribute("d") || ""),
          )
          .filter((node) => node.getClientRects().length > 0 || node.closest("#effort-popover")).length,
      ),
      0,
    );
    assert.equal(await page.getByRole("button", { name: /fast|lightning/i }).count(), 0);
    assert.equal(
      await page.locator("#effort-dots span").count(),
      Number(await page.locator("#effort-slider").getAttribute("max")) + 1,
    );
    // The model name opens the model list; a model returns to the panel.
    await chooseModel(page, "GPT-6 Astra");
    assert.equal(await page.locator("#model-popover").isHidden(), true);
    assert.equal(await page.locator("#effort-model-name").textContent(), "GPT-6 Astra");
    assert.equal(await page.locator("#effort-slider").getAttribute("max"), "5");
    assert.equal(await page.locator("#effort-dots span").count(), 6);
    await page.locator("#effort-slider").fill("3");
    await effortSaved(page, "Extra High");
    // Changing the level keeps the panel open, as in the desktop app.
    assert.equal(await page.locator("#effort-popover").isVisible(), true);
    assert.equal(await page.locator("#effort-title").textContent(), "Extra High");
    await page.locator("#effort-model").click();
    await page.locator("#model-popover").waitFor();
    assert.equal(await page.locator("#effort-popover").isHidden(), true);
    assert.equal(await page.locator("#picker-open-label").isVisible(), true);
    assert.equal(await page.locator("#model-popover .picker-heading").textContent(), "Select model");
    assert.deepEqual(
      await page
        .locator('#model-options .model-option[aria-pressed="true"]')
        .evaluateAll((options) => options.map((option) => option.textContent)),
      ["GPT-6 Astra✓"],
    );
    await page.screenshot({
      path: path.join(output, "model-picker.png"),
      fullPage: true,
    });
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#model-popover").isHidden(), true);
    assert.equal(await page.locator("#effort-popover").isHidden(), true);
    assert.equal(
      await page.locator("#picker-button").getAttribute("aria-expanded"),
      "false",
    );
    assert.equal(await page.locator("#picker-model").textContent(), "GPT-6 Astra");
    assert.equal(await page.locator("#picker-model").isVisible(), true);
    assert.equal(await page.locator("#effort-label").textContent(), "Extra High");
    // On a desktop (1280 px) the panel and the model list open right above the chip, with their right
    // edges on the chip's right edge, as in the desktop app, and inside the window.
    await page.setViewportSize({ width: 1280, height: 900 });
    await openPicker(page);
    for (const id of ["#effort-popover", "#model-popover"]) {
      if (id === "#model-popover") {
        await page.locator("#effort-model").click();
        await page.locator(id).waitFor();
      }
      const chip = await page.locator("#picker-button").boundingBox();
      const box = await page.locator(id).boundingBox();
      assert.ok(
        Math.abs(box.x + box.width - (chip.x + chip.width)) <= 4,
        `${id} right edge ${box.x + box.width} is on the chip's right edge ${chip.x + chip.width}`,
      );
      assert.ok(
        box.y + box.height <= chip.y && chip.y - (box.y + box.height) <= 30,
        `${id} sits right above the chip (${box.y + box.height} vs ${chip.y})`,
      );
      assert.ok(box.x >= 0 && box.x + box.width <= 1280, `${id} is inside the window`);
    }
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 1440, height: 950 });
    // Ctrl+Shift+M opens the panel too; a press outside closes it.
    await page.locator("#message").focus();
    await page.keyboard.press("Control+Shift+M");
    await page.locator("#effort-popover").waitFor();
    await page.screenshot({
      path: path.join(output, "reasoning-picker.png"),
      fullPage: true,
    });
    const outside = await page.locator("#messages").boundingBox();
    await page.mouse.click(outside.x + 4, outside.y + outside.height / 2);
    assert.equal(await page.locator("#effort-popover").isHidden(), true);
    assert.equal(await page.locator("#picker-open-label").isVisible(), false);
    // One menu at a time: opening the picker, by any route, closes the add menu, the command list
    // and the permissions pop-up, and opening one of those closes the picker.
    await page.locator("#attach").click();
    assert.equal(await page.locator("#plus-menu").isVisible(), true);
    await page.keyboard.press("Control+Shift+M");
    await page.locator("#effort-popover").waitFor();
    assert.equal(await page.locator("#plus-menu").isHidden(), true, "Ctrl+Shift+M closes the add menu");
    await page.locator("#attach").click();
    assert.equal(await page.locator("#plus-menu").isVisible(), true);
    assert.equal(await page.locator("#effort-popover").isHidden(), true, "the add menu closes the picker");
    await page.locator("#picker-button").click();
    await page.locator("#effort-popover").waitFor();
    assert.equal(await page.locator("#plus-menu").isHidden(), true, "the chip closes the add menu");
    await page.keyboard.press("Escape");
    await page.locator("#message").fill("");
    await page.locator("#message").pressSequentially("/");
    await page.locator("#slash-popover").waitFor();
    await page.keyboard.press("Control+Shift+M");
    await page.locator("#effort-popover").waitFor();
    assert.equal(await page.locator("#slash-popover").isHidden(), true, "Ctrl+Shift+M closes the command list");
    await page.keyboard.press("Escape");
    await page.locator("#message").fill("");
    await page.locator("#permission-button").click();
    assert.equal(await page.locator("#permissions-dialog").evaluate((node) => node.open), true);
    await page.keyboard.press("Control+Shift+M");
    await page.locator("#effort-popover").waitFor();
    assert.equal(
      await page.locator("#permissions-dialog").evaluate((node) => node.open),
      false,
      "Ctrl+Shift+M closes the permissions pop-up",
    );
    await page.locator("#permission-button").click();
    assert.equal(await page.locator("#permissions-dialog").evaluate((node) => node.open), true);
    assert.equal(await page.locator("#effort-popover").isHidden(), true, "the permissions pop-up closes the picker");
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#permissions-dialog").evaluate((node) => node.open), false);
    await page.locator("#message").fill("Draft that should stay in this chat");
    await page.locator('[data-task-id="preview-01"]').click();
    await page
      .locator("#messages")
      .getByText("The dashboard configuration is valid.", { exact: true })
      .waitFor();
    assert.equal(
      await page.locator("#messages .check.check-valid").textContent(),
      "✓Home Assistant configuration check passed",
    );
    await page.locator('[data-task-id="preview-03"]').click();
    await page.locator("#messages .check.check-invalid").waitFor();
    assert.match(
      await page.locator("#messages .check-invalid .check-detail").textContent(),
      /required key 'trigger'/,
    );
    // The answer gives no verdict on saved copies (CX-04-NO-CHECKS).
    assert.equal(await page.locator("#messages .backups-saved, #messages .backups-missing").count(), 0);
    await page.screenshot({
      path: path.join(output, "config-check-failed.png"),
      fullPage: true,
    });
    await page.locator('[data-task-id="preview-01"]').click();
    await page.locator('[data-task-id="preview-00"]').click();
    assert.equal(
      await page.locator("#message").inputValue(),
      "Draft that should stay in this chat",
    );
    await page
      .locator("#messages")
      .getByText("Your evening routine looks good.", { exact: false })
      .waitFor();
    await page.locator("#message").fill("Apply the first suggestion.");
    await page
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    // The worker's own first step shows at once, with a pulsing dot and a timer
    // that counts up, long before Codex reports anything.
    await page
      .locator("#activity.running .step-phase.is-running")
      .waitFor({ timeout: 5000, state: "attached" });
    assert.equal(
      await page.locator("#activity .step-phase .step-text").first().textContent(),
      "Noting the current state of your configuration files",
    );
    /** Return the seconds on the step list timer, checking its format. */
    const elapsedSeconds = async () => {
      const text = await page
        .locator("#activity .activity-toggle .elapsed")
        .textContent();
      assert.match(text, /^ · \d+s$/);
      return Number(text.match(/\d+/)[0]);
    };
    await page.waitForFunction(() =>
      /\d+s$/.test(
        document.querySelector("#activity .activity-toggle .elapsed")
          ?.textContent || "",
      ),
    );
    const firstReading = await elapsedSeconds();
    assert.ok(firstReading <= 2, `timer started at ${firstReading}s`);
    assert.deepEqual(
      await page.evaluate(() => {
        // The waiting line is only on screen for a moment, so check its dot on a copy.
        const line = document.createElement("p");
        line.className = "pending";
        document.querySelector("#messages").append(line);
        const names = [
          getComputedStyle(line, "::before").animationName,
          getComputedStyle(
            document.querySelector("#activity .activity-chevron"),
            "::before",
          ).animationName,
        ];
        line.remove();
        return names;
      }),
      ["activity-pulse", "activity-pulse"],
    );
    // Steps appear one by one while the simulated run works, closed under the working line until it is tapped
    // (CX-04-FB-THINK).
    await page
      .locator("#activity.running .step-command.is-running")
      .waitFor({ timeout: 15000, state: "attached" });
    const workingToggle = page.locator("#activity .activity-toggle");
    assert.equal(await workingToggle.getAttribute("aria-expanded"), "false");
    assert.equal(await page.locator("#activity-steps").isVisible(), false);
    assert.match(await workingToggle.textContent(), /^Working on your request/);
    await workingToggle.click();
    assert.equal(await workingToggle.getAttribute("aria-expanded"), "true");
    assert.equal(await page.locator("#activity-steps").isVisible(), true);
    await workingToggle.click();
    assert.equal(await page.locator("#activity-steps").isVisible(), false);
    await workingToggle.click();
    assert.equal(await page.locator("#activity-steps").isVisible(), true);
    assert.ok((await elapsedSeconds()) > firstReading, "timer did not advance");
    assert.deepEqual(
      await page.locator("#activity .step-phase .step-text").allTextContents(),
      [
        "Noting the current state of your configuration files",
        "Starting Codex",
        "Codex is thinking",
      ],
    );
    assert.equal(await page.locator("#activity .step-phase.is-running").count(), 0);
    await page.screenshot({
      path: path.join(output, "activity-running.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page
      .locator("#messages")
      .getByText("Preview response: Apply the first suggestion.", {
        exact: true,
      })
      .waitFor();
    // Three steps from Codex and four from the worker (its text messages show outside the list); the timer stops with the run,
    // and the closed line reads the exchange's total time (CX-FB2-FLOW).
    await page.waitForFunction(() =>
      /^Worked for \d+s$/.test(
        document.querySelector("#activity .activity-toggle")?.textContent || "",
      ),
    );
    // The tap held only until the turn ended: the line is closed again under "Worked for".
    assert.equal(await page.locator("#activity .activity-toggle").getAttribute("aria-expanded"), "false");
    assert.equal(await page.locator("#activity-steps").isVisible(), false);
    await page.locator("#activity .activity-toggle").click();
    assert.equal(await page.locator("#activity .step").count(), 7);
    assert.equal(await page.locator(".elapsed").count(), 0);
    assert.equal(await page.locator("#activity").count(), 1);
    assert.equal(await page.locator("#activity .step.is-running").count(), 0);
    assert.equal(await page.locator(".message.user").count(), 2);
    assert.equal(await page.locator(".answer").count(), 2);
    // CX-REPLY-ALIGN: the text messages Codex wrote along the way ("I'll look for …", "I found …") are blocks of
    // their own, spaced apart from each other and from the answer, and start where it starts. Once the exchange is
    // over they sit inside the open list, between its step groups (CX-BLEED-FIX).
    const narration = await page.evaluate(() => {
      const exchange = [...document.querySelectorAll("#messages .exchange")].at(-1);
      const blocks = [...exchange.querySelectorAll("#activity-steps > .message.interim"), exchange.querySelector(".answer > .message")];
      const firstCharacter = (node) => {
        const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT, {
          acceptNode: (text) => (text.data.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
        });
        const text = walker.nextNode();
        const range = document.createRange();
        const start = text.data.search(/\S/);
        range.setStart(text, start);
        range.setEnd(text, start + 1);
        return range.getBoundingClientRect().left;
      };
      return blocks.map((node) => {
        const rect = node.getBoundingClientRect();
        return { top: rect.top, bottom: rect.bottom, left: firstCharacter(node) };
      });
    });
    assert.ok(narration.length >= 3, `${narration.length - 1} narration blocks above the answer`);
    for (let n = 1; n < narration.length; n++) {
      const gap = narration[n].top - narration[n - 1].bottom;
      assert.ok(gap >= 8, `block ${n} is ${gap} px below the one above it`);
      assert.ok(
        Math.abs(narration[n].left - narration[0].left) <= 1,
        `block ${n} starts at ${narration[n].left} px, the first at ${narration[0].left} px`,
      );
    }
    const saved = await page.evaluate(
      async () => (await (await fetch("tasks/preview-00")).json()).task,
    );
    assert.deepEqual(saved.chat_settings, {
      model: "gpt-6-astra",
      reasoning_effort: "xhigh",
    });
    assert.deepEqual(
      saved.turns.at(-1).execution_settings,
      saved.chat_settings,
    );
    await openPicker(page);
    await page.locator("#effort-slider").press("End");
    await effortSaved(page, "Ultra");
    await chooseModel(page, "GPT-6.1 Terra");
    assert.equal(await page.locator("#effort-label").textContent(), "Ultra");
    assert.deepEqual(
      await page.evaluate(
        async () =>
          (await (await fetch("tasks/preview-00")).json()).task.chat_settings,
      ),
      { model: "gpt-6.1-terra", reasoning_effort: "ultra" },
    );
    // Choosing the model the add-on runs anyway clears the chat's own selection.
    await chooseModel(page, "GPT-6.2 Sol");
    assert.deepEqual(
      await page.evaluate(
        async () =>
          (await (await fetch("tasks/preview-00")).json()).task.chat_settings,
      ),
      { model: null, reasoning_effort: "ultra" },
    );
    await chooseModel(page, "GPT-6 Luna");
    assert.equal(await page.locator("#effort-label").textContent(), "Medium");
    // The slider follows the model: GPT-6 Luna has five levels, so five stops.
    assert.equal(await page.locator("#effort-slider").getAttribute("max"), "4");
    assert.equal(await page.locator("#effort-dots span").count(), 5);
    await page.locator("#effort-slider").fill("2");
    await effortSaved(page, "High");
    await page
      .getByRole("button", {
        name: "Use the default model and reasoning",
        exact: true,
      })
      .click();
    await effortSaved(page, "Medium");
    // Reset goes back to the default model too, not only its level.
    assert.deepEqual(
      await page.evaluate(
        async () =>
          (await (await fetch("tasks/preview-00")).json()).task.chat_settings,
      ),
      { model: null, reasoning_effort: null },
    );
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#effort-popover").isHidden(), true);
    const divider = await page.locator("#divider").boundingBox();
    await page.mouse.move(divider.x + 2, 300);
    await page.mouse.down();
    await page.mouse.move(355, 300);
    await page.mouse.up();
    assert.equal(
      await page.locator("#divider").getAttribute("aria-valuenow"),
      "355",
    );
    await page.locator("#divider").focus();
    await page.keyboard.press("ArrowLeft");
    assert.equal(
      await page.locator("#divider").getAttribute("aria-valuenow"),
      "335",
    );
    await page.locator("#message").focus();
    await page.screenshot({
      path: path.join(output, "desktop.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    assert.equal(await page.locator(".answer").count(), 0);
    // A chat that holds a model no longer in the list can still open the menu:
    // the button shows the id, no row is marked, and the first row has the focus.
    await page.evaluate(() => {
      state.chatSettings.set(null, { model: "gone", reasoning_effort: null });
      controls();
    });
    assert.equal(await page.locator("#picker-model").textContent(), "gone");
    await openPicker(page);
    await page.locator("#effort-model").click();
    assert.equal(
      await page.locator('#model-options [aria-pressed="true"]').count(),
      0,
    );
    assert.equal(
      await page.evaluate(() => document.activeElement.textContent),
      "GPT-6.2 Sol",
    );
    await page.keyboard.press("Escape");
    await page.evaluate(() => {
      state.chatSettings.delete(null);
      controls();
    });
    // A new chat names the model it runs on, and the menu marks that model.
    assert.equal(
      await page.locator("#picker-model").textContent(),
      "GPT-6.2 Sol",
    );
    await openPicker(page);
    await page.locator("#effort-model").click();
    assert.deepEqual(
      await page
        .locator("#model-options .model-option")
        .evaluateAll((options) =>
          options
            .filter((option) => option.getAttribute("aria-pressed") === "true")
            .map((option) => option.textContent),
        ),
      ["GPT-6.2 Sol✓"],
    );
    assert.equal(await page.locator("#model-options .model-option").count(), 9);
    await modelRow(page, "GPT-5.6 Luna").click();
    await page.locator("#effort-popover").waitFor();
    await page.waitForFunction(
      () => document.querySelector("#picker-model").textContent === "GPT-5.6 Luna",
    );
    await page.locator("#effort-slider").fill("4");
    await effortSaved(page, "Max");
    await page.keyboard.press("Escape");
    const hostile = '<img src=x onerror="window.untrustedRan=true">';
    await page.locator("#message").fill(hostile);
    await page
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await page
      .locator("#messages")
      .getByText("Preview response: " + hostile, { exact: true })
      .waitFor();
    assert.equal(await page.locator("#messages img").count(), 0);
    assert.equal(await page.evaluate(() => window.untrustedRan), undefined);
    assert.equal(
      await page.locator("#picker-model").textContent(),
      "GPT-5.6 Luna",
    );
    // The chip uses an icon chevron, not a text glyph.
    assert.equal(
      await page.locator("#picker-button svg.picker-chevron").count(),
      1,
    );
    // Markdown typed into a message is formatted, but cannot add markup, load
    // an image, or open anything other than a web or mail address.
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    const typed = [
      "First line",
      "second line with `inline code`",
      "",
      "```",
      "<i>code</i> **stays** as typed",
      "```",
      "",
      "[script](javascript:window.untrustedRan=true) [file](/config/automations.yaml)",
      "![remote image](https://example.invalid/pixel.png)",
      "<b>raw</b> <script>window.untrustedRan=true</script>",
      "[site](https://www.home-assistant.io/)",
    ].join("\n");
    await page.locator("#message").fill(typed);
    await page
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await page.locator("#messages .answer pre").waitFor();
    const typedMessage = page.locator("#messages .message.user");
    // A single Enter is still a line break.
    assert.equal(await typedMessage.locator("p").first().locator("br").count(), 1);
    assert.equal(
      await typedMessage.locator("p code").textContent(),
      "inline code",
    );
    assert.equal(
      await typedMessage.locator("pre code").textContent(),
      "<i>code</i> **stays** as typed",
    );
    assert.match(
      await typedMessage.textContent(),
      /<b>raw<\/b> <script>window\.untrustedRan=true<\/script>/,
    );
    assert.equal(
      await page.locator("#messages").locator("img, b, i, script").count(),
      0,
    );
    // The same text is in the sent message and in the echoed answer.
    assert.deepEqual(
      await page
        .locator("#messages a:not(.interim a)")
        .evaluateAll((links) =>
          links.map((link) => [link.textContent, link.href, link.target, link.rel]),
        ),
      Array(2)
        .fill([
          [
            "remote image",
            "https://example.invalid/pixel.png",
            "_blank",
            "noreferrer noopener",
          ],
          [
            "site",
            "https://www.home-assistant.io/",
            "_blank",
            "noreferrer noopener",
          ],
        ])
        .flat(),
    );
    assert.equal(await page.evaluate(() => window.untrustedRan), undefined);
    assert.deepEqual(
      requested.filter((url) => url.includes("example.invalid")),
      [],
    );
    // Codex receives the message exactly as it was typed.
    assert.equal(
      await page.evaluate(
        async () =>
          (await (await fetch("tasks/" + state.id)).json()).task.turns.at(-1)
            .message,
      ),
      typed,
    );
    // Attach an image to a new chat: pending strip, remove, re-add, send.
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    const shot = pngBuffer();
    await page.locator("#file-input").setInputFiles([
      { name: "dashboard shot.png", mimeType: "image/png", buffer: shot },
      {
        name: "notes.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("not an image"),
      },
    ]);
    await page.locator(".pending-file").waitFor();
    assert.equal(await page.locator(".pending-file").count(), 1);
    // The accepted image appears before the rest of the batch finishes validation.
    await page.locator("#error").filter({hasText: /notes\.txt is not a PNG/}).waitFor();
    assert.match(
      await page.locator("#error").textContent(),
      /notes\.txt is not a PNG/,
    );
    await page
      .getByRole("button", { name: "Remove dashboard shot.png" })
      .click();
    assert.equal(await page.locator(".pending-file").count(), 0);
    assert.equal(await page.locator("#pending-files").isHidden(), true);
    await page.locator("#file-input").setInputFiles({
      name: "dashboard shot.png",
      mimeType: "image/png",
      buffer: shot,
    });
    await page.locator(".pending-file").waitFor();
    await page.locator("#message").fill("Why does this card look wrong?");
    await page.locator("#composer").screenshot({
      path: path.join(output, "attach-pending.png"),
      animations: "disabled",
    });
    await page
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await page
      .locator("#messages")
      .getByText("Preview response: Why does this card look wrong?", {
        exact: true,
      })
      .waitFor();
    const sentImage = page.locator(
      "#messages .user-attachments img.attachment-image",
    );
    await sentImage.waitFor();
    assert.equal(await sentImage.count(), 1);
    assert.equal(await page.locator(".pending-file").count(), 0);
    assert.match(
      await page
        .locator("#messages .user-attachments figcaption")
        .textContent(),
      /dashboard shot\.png/,
    );
    assert.deepEqual(
      await page.evaluate(async () => {
        const src = document.querySelector(
          "#messages .user-attachments img",
        ).src;
        const response = await fetch(src);
        return [response.status, response.headers.get("content-type")];
      }),
      [200, "image/png"],
    );
    // Once the daily cleanup has removed the upload, the chat says so. A new
    // browser context has no cached copy of the image.
    const sentChat = await page.evaluate(
      () => document.querySelector("#messages .user-attachments img").src,
    );
    const sentChatId = sentChat.match(/tasks\/([^/]+)\/attachments/)[1];
    assert.equal(
      await page.evaluate(
        async () => (await fetch("fixture/remove-uploads", { method: "POST" })).status,
      ),
      200,
    );
    const fresh = await browser.newContext();
    try {
      const freshPage = await fresh.newPage();
      await freshPage.goto("http://127.0.0.1:9137/preview/");
      await freshPage.locator(`[data-task-id="${sentChatId}"]`).click();
      await freshPage
        .locator("#messages p.attachment-missing", {
          hasText: "Attachment removed after 7 days.",
        })
        .waitFor();
    } finally {
      await fresh.close();
    }
    // Timing regressions, made deterministic by gating image decoding: a
    // batch stays with the chat it was picked in, and Send waits for images
    // that are still being prepared.
    await page.evaluate(() => {
      const original = window.createImageBitmap.bind(window);
      window.__bitmapGates = [];
      window.__restoreBitmap = () => {
        window.createImageBitmap = original;
      };
      /** Hold each decode back until the test releases it. */
      window.createImageBitmap = (...args) =>
        new Promise((resolve) => {
          window.__bitmapGates.push(() => resolve(original(...args)));
        });
    });
    /** Wait for a held-back image decode and let it go ahead. */
    const releaseDecode = async () => {
      await page.waitForFunction(() => window.__bitmapGates.length > 0);
      await page.evaluate(() => window.__bitmapGates.shift()());
    };
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    await page.locator("#file-input").setInputFiles([
      { name: "first.png", mimeType: "image/png", buffer: shot },
      { name: "second.png", mimeType: "image/png", buffer: shot },
    ]);
    await page.waitForFunction(() => window.__bitmapGates.length === 1);
    assert.equal(await page.locator(".pending-file.preparing").count(), 1);
    // Switch chats while the first image is still decoding.
    await page.locator('[data-task-id="preview-00"]').click();
    await page
      .locator("#messages")
      .getByText("Your evening routine looks good.", { exact: false })
      .first()
      .waitFor();
    assert.equal(await page.locator(".pending-file").count(), 0);
    await releaseDecode();
    await releaseDecode();
    assert.equal(await page.locator(".pending-file").count(), 0);
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    await page.waitForFunction(
      () =>
        document.querySelectorAll(".pending-file:not(.preparing)").length ===
          2 &&
        document.querySelectorAll(".pending-file.preparing").length === 0,
    );
    // Send stays disabled until the third image is ready, then sends all three.
    await page.locator("#message").fill("Compare these three.");
    await page.locator("#file-input").setInputFiles({
      name: "third.png",
      mimeType: "image/png",
      buffer: shot,
    });
    await page.waitForFunction(() => window.__bitmapGates.length === 1);
    assert.equal(await page.locator(".pending-file.preparing").count(), 1);
    assert.equal(await page.locator("#send").isDisabled(), true);
    await page.locator("#message").press("Enter");
    assert.equal(await page.locator(".answer").count(), 0);
    await releaseDecode();
    await page.waitForFunction(
      () =>
        document.querySelectorAll(".pending-file:not(.preparing)").length ===
        3,
    );
    assert.equal(await page.locator("#send").isDisabled(), false);
    await page
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await page
      .locator("#messages")
      .getByText("Preview response: Compare these three.", { exact: true })
      .waitFor();
    assert.equal(
      await page.locator("#messages .user-attachments img").count(),
      3,
    );
    assert.equal(await page.locator(".pending-file").count(), 0);
    await page.evaluate(() => window.__restoreBitmap());
    // An image without a MIME type, as some drop and clipboard sources
    // deliver, is accepted; the worker checks the bytes.
    await page.evaluate(async (bytes) => {
      await addUploads([
        new File([new Uint8Array(bytes)], "untyped-shot", { type: "" }),
      ]);
    }, [...shot]);
    assert.equal(await page.locator(".pending-file").count(), 1);
    assert.equal(
      await page.locator(".pending-name").textContent(),
      "untyped-shot",
    );
    await page.getByRole("button", { name: "Remove untyped-shot" }).click();
    // A file the picker cannot deliver is reported instead of vanishing.
    await page.evaluate(async () => {
      await acceptFiles([new File([], "cloud-photo.jpg", { type: "image/jpeg" })]);
    });
    assert.equal(await page.locator(".pending-file").count(), 0);
    assert.match(
      await page.locator("#error").textContent(),
      /cloud-photo\.jpg is empty or could not be read/,
    );
    // Queue: while one chat works, messages sent elsewhere wait in line. The
    // fixture keeps a chat whose message starts with "Take your time" working until
    // it is released.
    const queueButton = page.getByRole("button", {
      name: "Add message to queue",
      exact: true,
    });
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    await page
      .locator("#message")
      .fill(
        "Take your time and review every automation that uses the porch motion sensor.",
      );
    await page
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    await page.getByRole("button", { name: "Stop task" }).waitFor();
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    await page
      .locator("#notice")
      .filter({ hasText: /Another chat is working\. You can send this message now/ })
      .waitFor();
    assert.match(
      await page.locator("#notice").textContent(),
      /wait in the queue and start on its own when the running chat finishes\.$/,
    );
    const typo = "Add a sunset ofset to the evening lights.";
    const corrected = "Add a 20-minute sunset offset to the evening lights.";
    await page.locator("#message").fill(typo);
    await queueButton.click();
    const queuedBubble = page.locator("#messages .message.user.queued");
    await queuedBubble.waitFor();
    assert.equal(await queuedBubble.textContent(), typo);
    assert.equal(
      await page.locator("#chat-status").textContent(),
      "In queue · not sent yet",
    );
    assert.equal(await page.locator("#messages .answer").count(), 0);
    assert.match(
      await page.locator("#queued-note").textContent(),
      /^Next in line\./,
    );
    assert.match(
      await page.locator("#notice").textContent(),
      /waiting in the queue and has not been sent yet/,
    );
    assert.equal(await page.locator("#send").isDisabled(), true);
    assert.equal(await page.locator("#picker-button").isDisabled(), true);
    await page.locator(".chat-row.queued").waitFor();
    assert.equal(
      await page.locator("#chat-list .list-group").first().textContent(),
      "In queue",
    );
    assert.equal(
      await page.locator(".chat-row.queued .row-meta span").first().textContent(),
      "Next in queue",
    );
    // Edit the waiting message; Escape cancels, Enter saves.
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    await page.locator("#queued-edit").fill("Discarded edit");
    await page.locator("#queued-edit").press("Escape");
    assert.equal(await queuedBubble.textContent(), typo);
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    await page.locator("#queued-edit").fill(corrected);
    await page.locator("#queued-edit").press("Enter");
    await page.waitForFunction(
      (text) =>
        document.querySelector("#messages .message.user.queued")
          ?.textContent === text &&
        document.querySelector(".chat-row.queued .row-title")?.textContent ===
          text,
      corrected,
    );
    assert.equal(await page.locator("#chat-title").textContent(), corrected);
    // The chat that only exists in the queue survives a reload of the panel.
    await page.reload();
    await queuedBubble.waitFor();
    assert.equal(await queuedBubble.textContent(), corrected);
    // A follow-up in a saved chat waits too, with its image, below the history.
    await page.locator('[data-task-id="preview-02"]').click();
    await page
      .locator("#messages")
      .getByText("Here is a cartoon sheep", { exact: false })
      .waitFor();
    await page.locator("#file-input").setInputFiles({
      name: "scarf.png",
      mimeType: "image/png",
      buffer: shot,
    });
    await page.locator(".pending-file").waitFor();
    const followUp = "Give the sheep a scarf for the winter dashboard.";
    await page.locator("#message").fill(followUp);
    assert.match(
      await page.locator("#notice").textContent(),
      /and the message already in the queue finish\.$/,
    );
    await queueButton.click();
    await queuedBubble.waitFor();
    assert.equal(await page.locator("#messages .answer").count(), 1);
    assert.match(
      await page.locator("#chat-status").textContent(),
      /^Completed · .* · next message in queue$/,
    );
    await page.waitForFunction(() => {
      const img = document.querySelector(
        "#messages .exchange.queued .user-attachments img",
      );
      return img && img.complete && img.naturalWidth > 0;
    });
    // The loaded image must not push the edit and remove buttons out of view.
    await page.waitForFunction(
      () =>
        document.querySelector("#queued-remove").getBoundingClientRect()
          .bottom <=
        document.querySelector("#messages").getBoundingClientRect().bottom,
    );
    await page.waitForFunction(
      () => document.querySelectorAll(".chat-row.queued").length === 2,
    );
    assert.equal(await page.locator('[data-task-id="preview-02"]').count(), 1);
    assert.equal(
      await page
        .locator('.chat-row.queued:has([data-task-id="preview-02"]) .row-meta span')
        .first()
        .textContent(),
      "In queue · 2 of 2",
    );
    assert.match(
      await page.locator("#queued-note").textContent(),
      /after the running chat and the message ahead of it/,
    );
    // A third message is removed again, which also drops its unsent chat.
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    await page.locator("#message").fill("Changed my mind");
    await queueButton.click();
    await queuedBubble.waitFor();
    await page.waitForFunction(
      () => document.querySelectorAll(".chat-row.queued").length === 3,
    );
    await page.getByRole("button", { name: "Remove", exact: true }).click();
    await page.waitForFunction(
      () =>
        document.querySelectorAll(".chat-row.queued").length === 2 &&
        document.querySelector("#chat-title").textContent === "New chat",
    );
    assert.equal(await page.locator("#error").isHidden(), true);
    const waiting = await page.evaluate(async () => {
      const listing = await (await fetch("tasks?summary=true&limit=1")).json();
      return {
        active: listing.active_task_id,
        queue: listing.queue.map((entry) => [
          entry.message,
          entry.new_chat,
          entry.position,
        ]),
      };
    });
    assert.notEqual(waiting.active, null);
    assert.deepEqual(waiting.queue, [
      [corrected, true, 1],
      [followUp, false, 2],
    ]);
    // Releasing the working chat starts the queued messages in order.
    await page.locator(".chat-row.queued .row-main").first().click();
    await queuedBubble.waitFor();
    await page.locator("#message").focus();
    await page.screenshot({
      path: path.join(output, "queue.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page.evaluate(() => fetch("fixture/release", { method: "POST" }));
    await page
      .locator("#messages")
      .getByText("Preview response: " + corrected, { exact: true })
      .waitFor({ timeout: 15000 });
    assert.equal(await queuedBubble.count(), 0);
    assert.equal(await page.locator("#messages .message.user").count(), 1);
    await page.waitForFunction(
      () =>
        document.querySelectorAll(".chat-row.queued").length === 0 &&
        document.querySelector("#send").getAttribute("aria-label") ===
          "Send message",
    );
    await page.locator('[data-task-id="preview-02"]').click();
    await page
      .locator("#messages")
      .getByText("Preview response: " + followUp, { exact: true })
      .waitFor();
    assert.equal(await queuedBubble.count(), 0);
    assert.equal(
      await page.locator("#messages .user-attachments img").count(),
      1,
    );
    assert.equal(await page.locator("#notice").isHidden(), true);
    // A question from Codex shows its choices as buttons. Picking one sends it
    // as the next message and leaves a draft in the message box alone. The
    // fixture answers a message that starts with "Ask me" with a question.
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    await page
      .locator("#message")
      .fill("Ask me before you remove the duplicate automation.");
    await page
      .getByRole("button", { name: "Send message", exact: true })
      .click();
    const choiceButtons = page.locator("#messages .choices .choice");
    await choiceButtons.first().waitFor();
    assert.deepEqual(await choiceButtons.allTextContents(), [
      "Go ahead",
      "Don't change anything",
    ]);
    assert.equal(
      await page.locator("#messages .choices").getAttribute("aria-label"),
      "Answers Codex offers",
    );
    assert.match(
      await page.locator("#chat-status").textContent(),
      /^Needs your reply · /,
    );
    await page.waitForFunction(() => !document.querySelector("#messages .choice")?.disabled);
    assert.equal(await choiceButtons.first().isEnabled(), true);
    await page.screenshot({
      path: path.join(output, "choices.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page.locator("#message").fill("A draft I am still writing");
    const asked = await page.evaluate(async () => {
      const id = localStorage.getItem("codex-last-chat");
      const task = (await (await fetch(`tasks/${id}`)).json()).task;
      return {
        id,
        turn: task.turns[0].turn_id,
        choices: task.turns[0].choices,
      };
    });
    assert.deepEqual(asked.choices, ["Go ahead", "Don't change anything"]);
    await choiceButtons.nth(1).click();
    await page
      .locator("#messages")
      .getByText("Preview response: Don't change anything", { exact: true })
      .waitFor();
    assert.equal(await choiceButtons.count(), 0);
    assert.deepEqual(
      await page.locator("#messages .message.user").allTextContents(),
      [
        "Ask me before you remove the duplicate automation.",
        "Don't change anything",
      ],
    );
    // The question stays in the history, and the draft in the message box.
    await page
      .locator("#messages")
      .getByText("Remove the duplicate automation?", { exact: true })
      .waitFor();
    assert.equal(
      await page.locator("#message").inputValue(),
      "A draft I am still writing",
    );
    await page.locator("#message").fill("");
    // An answer that names a question no longer waiting is refused.
    const late = await page.evaluate(async ({ id, turn }) => {
      const response = await fetch(`tasks/${id}/continue`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ choice: 0, turn_id: turn }),
      });
      return { status: response.status, error: (await response.json()).error };
    }, asked);
    assert.deepEqual(late, {
      status: 409,
      error: "This question is no longer waiting for an answer.",
    });
    await page.getByRole("button", { name: "Open settings" }).click();
    // Settings sections start collapsed; the ChatGPT header inside Accounts and connections carries the short status.
    await page.locator("#accounts-section > h3 .settings-toggle").click();
    await page
      .locator("#chatgpt-section .settings-status")
      .getByText("Signed in", { exact: true })
      .waitFor();
    await page.locator("#chatgpt-section .settings-toggle").click();
    await page
      .getByText("Signed in (local preview)", { exact: true })
      .waitFor();
    await page.locator("#workspace-section .settings-toggle").click();
    await page.locator("#workspace-open").click();
    await page.locator("#agents").fill("Keep edits focused.");
    await page.getByRole("button", { name: "Save instructions" }).click();
    await page.getByText("Instructions saved.").waitFor();
    await page.getByRole("button", { name: "Close workspace rules" }).click();
    // The open chat, here the one created by sending a message above, is
    // remembered across a reload of the panel.
    const openTitle = await page.locator("#chat-title").textContent();
    assert.notEqual(openTitle, "New chat");
    await page.reload();
    // The remembered chat is opened by the boot script itself, so the
    // welcome screen is never shown first.
    assert.notEqual(await page.locator("#chat-title").textContent(), "New chat");
    await page.locator(".chat-row").first().waitFor();
    assert.equal(
      await page.locator("#divider").getAttribute("aria-valuenow"),
      "335",
    );
    await page.locator("#chat-title").getByText(openTitle).waitFor();
    await page.locator('[data-task-id="preview-03"]').click();
    await page.locator("#chat-title").getByText("Earlier chat 3").waitFor();
    await page.reload();
    assert.notEqual(await page.locator("#chat-title").textContent(), "New chat");
    await page.locator("#chat-title").getByText("Earlier chat 3").waitFor();
    assert.equal(
      await page
        .locator('[data-task-id="preview-03"]')
        .getAttribute("aria-current"),
      "true",
    );
    assert.equal(await page.locator("#error").isHidden(), true);
    // A remembered chat that no longer exists falls back to a new chat quietly.
    await page.evaluate(() =>
      localStorage.setItem("codex-last-chat", "preview-gone"),
    );
    await page.reload();
    await page.locator(".chat-row").first().waitFor();
    assert.equal(await page.locator("#chat-title").textContent(), "New chat");
    assert.equal(await page.locator("#error").isHidden(), true);
    assert.equal(
      await page.evaluate(() => localStorage.getItem("codex-last-chat")),
      null,
    );
    // Choosing New chat is remembered too.
    await page.locator('[data-task-id="preview-03"]').click();
    await page.locator("#chat-title").getByText("Earlier chat 3").waitFor();
    await page.getByRole("button", { name: "New chat", exact: false }).click();
    await page.reload();
    await page.locator(".chat-row").first().waitFor();
    assert.equal(await page.locator("#chat-title").textContent(), "New chat");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => document.querySelector("#sidebar").inert);
    assert.equal(
      await page.locator("#sidebar").evaluate((el) => el.inert),
      true,
    );
    await page
      .getByRole("button", { name: "Open conversations", exact: true })
      .click();
    await page.locator('[data-task-id="preview-00"]').click();
    await page
      .locator("#messages")
      .getByText("Preview response: Apply the first suggestion.", {
        exact: true,
      })
      .waitFor();
    // preview-00 was reset to the default model above, so it shows that model.
    assert.equal(await page.locator("#picker-model").textContent(), "GPT-6.2 Sol");
    assert.equal(await page.locator("#effort-label").textContent(), "Medium");
    // A tap on a chat keeps the side bar open (CX-04-PHONE-LIST); the scrim closes it.
    assert.equal(await page.locator("#scrim").isHidden(), false);
    await page.locator("#scrim").click({ position: { x: 380, y: 600 } });
    assert.equal(await page.locator("#scrim").isHidden(), true);
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
    );
    await page.screenshot({
      path: path.join(output, "mobile.png"),
      fullPage: true,
      animations: "disabled",
    });
    // On a phone the line wraps, and stays above an open chat that is scrolled.
    await integrationLine(page, "not_connected", integrationLines.not_connected);
    assert.equal(await integrationLineStaysOnTop(page), true);
    await page.screenshot({
      path: path.join(output, "mobile-integration-notice.png"),
      animations: "disabled",
    });
    await page.setViewportSize({ width: 320, height: 568 });
    assert.equal(await integrationLineStaysOnTop(page), true);
    await page.emulateMedia({ colorScheme: "dark" });
    await page.screenshot({
      path: path.join(output, "mobile-integration-notice-dark.png"),
      animations: "disabled",
    });
    await page.emulateMedia({ colorScheme: "light" });
    await page.setViewportSize({ width: 390, height: 844 });
    await integrationLine(page, "ok", "");
    await openPicker(page);
    await page.screenshot({
      path: path.join(output, "mobile-reasoning.png"),
      fullPage: true,
    });
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 320, height: 568 });
    await openPicker(page);
    const panel = await page.locator("#effort-popover").boundingBox();
    assert(panel.x >= 0 && panel.y >= 0 && panel.x + panel.width <= 320);
    await page.locator("#effort-model").click();
    const popover = await page.locator("#model-popover").boundingBox();
    assert(
      popover.x >= 0 && popover.y >= 0 && popover.x + popover.width <= 320,
    );
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
    );
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 390, height: 844 });
    await page
      .getByRole("button", { name: "Open conversations", exact: true })
      .click();
    await page.screenshot({
      path: path.join(output, "mobile-sidebar.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page
      .getByRole("button", { name: "Close conversations", exact: true })
      .last()
      .click();
    await page.emulateMedia({ colorScheme: "dark" });
    await openPicker(page);
    await page.screenshot({
      path: path.join(output, "mobile-dark.png"),
      fullPage: true,
      animations: "disabled",
    });
    await page.keyboard.press("Escape");
    // On a phone, a long code line scrolls inside its block instead of widening the page.
    await page
      .getByRole("button", { name: "Open conversations", exact: true })
      .click();
    await page.locator('[data-task-id="preview-04"]').click();
    const wideCode = page.locator("#messages .answer pre");
    await wideCode.waitFor();
    assert.equal(
      await wideCode.evaluate((pre) => pre.scrollWidth > pre.clientWidth),
      true,
    );
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
    );
    await page.screenshot({
      path: path.join(output, "markdown-mobile-dark.png"),
      fullPage: true,
      animations: "disabled",
    });
    // Long press on a phone opens the actions sheet without selecting the chat.
    const touch = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
    });
    const phone = await touch.newPage();
    phone.on("pageerror", (error) => errors.push(error.message));
    await phone.goto("http://127.0.0.1:9137/preview/");
    await phone.locator(".chat-row").first().waitFor({ state: "attached" });
    // CX-PICKER-PARITY on a phone, by touch only: the chip opens the panel, the model name opens the
    // list, a model returns to the panel, the slider sets the level, and a tap outside closes it.
    // This page opened on a new chat, so the choices stay in the page and the fixture's chats keep theirs.
    await phone.waitForFunction(
      () => !document.querySelector("#picker-button").disabled,
    );
    await phone.locator("#picker-button").tap();
    await phone.locator("#effort-popover").waitFor();
    assert.equal(await phone.locator("#picker-open-label").isVisible(), true);
    const phonePanel = await phone.locator("#effort-popover").boundingBox();
    assert.ok(
      phonePanel.x >= 0 && phonePanel.y >= 0 && phonePanel.x + phonePanel.width <= 390,
      `phone picker panel is inside the screen (${JSON.stringify(phonePanel)})`,
    );
    await phone.locator("#effort-model").tap();
    await phone.locator("#model-popover").waitFor();
    const phoneList = await phone.locator("#model-popover").boundingBox();
    assert.ok(
      phoneList.x >= 0 && phoneList.y >= 0 && phoneList.x + phoneList.width <= 390,
      `phone model list is inside the screen (${JSON.stringify(phoneList)})`,
    );
    await modelRow(phone, "GPT-6 Luna").tap();
    await phone.locator("#effort-popover").waitFor();
    await phone.waitForFunction(
      () =>
        document.querySelector("#effort-model-name").textContent === "GPT-6 Luna" &&
        !document.querySelector("#effort-slider").disabled,
    );
    assert.equal(await phone.locator("#effort-slider").getAttribute("max"), "4");
    await phone.locator("#effort-slider").fill("2");
    await effortSaved(phone, "High");
    assert.equal(
      await phone.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
    );
    await phone.locator("#greeting").tap();
    assert.equal(await phone.locator("#effort-popover").isHidden(), true);
    assert.equal(await phone.locator("#picker-model").textContent(), "GPT-6 Luna");
    assert.equal(await phone.locator("#effort-label").textContent(), "High");
    await phone
      .getByRole("button", { name: "Open conversations", exact: true })
      .click();
    await phone.waitForFunction(
      () => document.querySelector("#sidebar").getBoundingClientRect().x >= 0,
    );
    const input = await touch.newCDPSession(phone);
    /** Touch the middle of the preview-01 chat row for hold milliseconds. */
    const press = async (hold) => {
      const row = phone.locator('[data-task-id="preview-01"]');
      await row.scrollIntoViewIfNeeded();
      const box = await row.boundingBox();
      const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
      await input.send("Input.dispatchTouchEvent", {
        type: "touchStart",
        touchPoints: [point],
      });
      await phone.waitForTimeout(hold);
      await input.send("Input.dispatchTouchEvent", {
        type: "touchEnd",
        touchPoints: [],
      });
    };
    await press(700);
    await phone.locator("#chat-menu.sheet").waitFor({ state: "visible" });
    assert.equal(await phone.locator("#chat-title").textContent(), "New chat");
    assert.equal(
      await phone.locator("#chat-menu-title").textContent(),
      "Check the energy dashboard",
    );
    await phone.getByRole("menuitem", { name: "Cancel" }).click();
    assert.equal(await phone.locator("#chat-menu").isHidden(), true);
    await press(50);
    await phone
      .locator("#messages")
      .getByText("The dashboard configuration is valid.", { exact: true })
      .waitFor();
    // The tap kept the side bar open (CX-04-PHONE-LIST); close it to reach the chat area.
    await phone.locator("#scrim").click({ position: { x: 380, y: 600 } });
    // The screenshot the user attached shows with their message.
    // No bold Verification box under the answer: the checks sit in one closed block, readback and note inside.
    // CX-FB2-FLOW: that block is inside the answer's closed "Worked for" line, and a failed check keeps one
    // short line under the answer, in the error color.
    const errorColor = await phone.evaluate(() => { const probe = document.createElement("span"); probe.style.color = "var(--error)";
      document.body.append(probe); const color = getComputedStyle(probe).color; probe.remove(); return color; });
    const failedLine = phone.locator(".answer .check-failed-line");
    await failedLine.waitFor();
    assert.equal(await failedLine.textContent(), "1 check failed");
    assert.equal(await failedLine.evaluate(node => getComputedStyle(node).color), errorColor);
    assert.ok(await failedLine.evaluate(node => node.getBoundingClientRect().height) <= 24);
    assert.equal(await phone.locator(".verification > summary").isVisible(), false);
    await phone.locator("#activity > .activity-toggle").click();
    await phone.locator(".verification > summary").waitFor();
    assert.equal(await phone.locator(".verification h3").count(), 0);
    assert.equal(await phone.locator(".verification").evaluate(node => node.open), false);
    // The closed line names the check that needs review, in the error color, and keeps one line of height.
    const closedLine = phone.locator(".verification > summary");
    assert.equal(await closedLine.textContent(), "Checks and readback (2) · 1 failed");
    assert.equal(await closedLine.evaluate(node => node.classList.contains("has-failed")), true);
    assert.equal(await closedLine.evaluate(node => getComputedStyle(node).color),
      await phone.evaluate(() => { const probe = document.createElement("span"); probe.style.color = "var(--error)";
        document.body.append(probe); const color = getComputedStyle(probe).color; probe.remove(); return color; }));
    assert.ok(await closedLine.evaluate(node => node.getBoundingClientRect().height) <= 24);
    assert.equal(await phone.locator(".verification-result").first().isVisible(), false);
    await phone.locator(".verification > summary").click();
    assert.equal(await phone.locator(".verification-result").first().isVisible(), true);
    assert.equal(await phone.locator(".verification-result").count(), 2);
    await phone.locator(".verification-result").nth(1).locator("summary").click();
    assert.equal(await phone.locator(".verification-result").nth(1).locator("summary").textContent(), "Failed · /lovelace/energy");
    assert.equal(await phone.locator(".verification").getByText("Fresh dashboard configuration readback differs from the saved configuration.", {exact:true}).isVisible(), true);
    // No dashboard is rendered: no screenshot and no screenshot note in the checks.
    assert.equal(await phone.locator(".verification img").count(), 0);
    assert.equal(await phone.locator(".verification").getByText(/screenshot/i).count(), 0);
    await phone.screenshot({path: path.join(output, "verification-mobile.png"), fullPage: true});
    // From a closed "Worked for" line, the failed-check line opens the line and the checks in one tap.
    await phone.locator("#activity > .activity-toggle").click();
    await phone.locator(".verification").evaluate(node => { node.open = false; });
    assert.equal(await phone.locator(".verification > summary").isVisible(), false);
    await failedLine.click();
    assert.equal(await phone.locator(".verification-result").first().isVisible(), true);
    assert.equal(
      await phone.locator("#messages .user-attachments img").count(),
      1,
    );
    await touch.close();
    // Android WebViews get a single-select file input; everyone else keeps multiple.
    assert.equal(await page.locator("#file-input").getAttribute("multiple"), "");
    const webview = await browser.newContext({
      viewport: { width: 390, height: 844 },
      hasTouch: true,
      isMobile: true,
      userAgent:
        "Mozilla/5.0 (Linux; Android 14; NE2213 Build/UKQ1.230924.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/128.0.0.0 Mobile Safari/537.36 Home Assistant/2025.9.1 (Android 14; NE2213)",
    });
    const app = await webview.newPage();
    app.on("pageerror", (error) => errors.push(error.message));
    await app.goto("http://127.0.0.1:9137/preview/");
    await app.locator(".chat-row").first().waitFor({ state: "attached" });
    assert.equal(await app.locator("#file-input").getAttribute("multiple"), null);
    await app.locator("#file-input").setInputFiles({
      name: "IMG_20260924.jpg",
      mimeType: "image/jpeg",
      buffer: shot,
    });
    await app.locator(".pending-file").waitFor();
    await webview.close();
    await require("./browser_approvals.cjs")(browser, output, errors);
    await require("./browser_text_size.cjs")(browser, output, errors);
    await require("./browser_slash.cjs")(browser, output, errors);
    await require("./browser_file_link.cjs")(browser, output, errors);
    await require("./browser_answer_bleed.cjs")(browser, output, errors);
    assert.deepEqual(errors, []);
    console.log(
      "PASS: queued messages (wait behind a working chat, edit, remove, start in order), choices under a question (pick one, draft kept, late answer refused), attached images (pick, reject, remove, send, render, batch stays with its chat, send waits for decoding), chat actions (pin, rename, delete, long press), generated image attachments, Markdown formatting (sent messages, answers, untrusted text, slow or long text, narrow screens), saved model/reasoning choices, model compatibility, the model a chat runs on by name, quota bars, the line about the Codex integration, keyboard/reset controls, history, pagination, continuation, new chats, drafts, reopening the last chat, safe text, settings, text size (three sizes, kept per device, phone and iPad), slash commands (list, filter, tap, keys, run not sent), file links in answers (card, download, refused paths), narration kept out of the answer (inside the closed list, aligned with it at four widths), resize, mobile and dark mode. Screenshots: " +
        output,
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
