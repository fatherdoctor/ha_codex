// Exercise poll() and busyAction(), the chat page's two shared helpers, against web_fixture.py.
const assert = require("node:assert/strict");

/**
 * Run a scenario in the page with two tools: sleep(ms), and hide(hidden), which makes the page report itself as
 * hidden or shown. The scenario is a function body as text, so it can use the page's own poll() and busyAction().
 */
function inPage(page, body) {
  return page.evaluate(async (source) => {
    const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
    const hide = (hidden) =>
      Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
    try {
      return await new Function("sleep", "hide", `return (async () => {${source}})();`)(sleep, hide);
    } finally {
      delete document.hidden;
    }
  }, body);
}

/** Check the scheduler: the repeat, the stop, the hidden-page rule and where a failure goes. */
async function testPoll(page) {
  // run() calls the check, returns its result and repeats it after the delay computed from that result.
  assert.deepEqual(
    await inPage(page, `
      const seen = [];
      let count = 0;
      const check = poll("test-repeat", () => ++count, (result) => { seen.push(result); return result < 3 ? 10 : null; });
      const first = await check.run();
      const waitingAfterFirst = check.waiting;
      await sleep(120);
      return { first, waitingAfterFirst, count, seen, waiting: check.waiting, registered: polls["test-repeat"] === check };
    `),
    { first: 1, waitingAfterFirst: true, count: 3, seen: [1, 2, 3], waiting: false, registered: true },
  );
  // On a hidden page the check is not called, the delay function gets no result, and the repeat goes on.
  assert.deepEqual(
    await inPage(page, `
      const seen = [];
      let count = 0;
      const check = poll("test-hidden", () => ++count, (result) => { seen.push(result); return 10; });
      hide(true);
      await check.run();
      await sleep(60);
      const whileHidden = { count, onlyUndefined: seen.length > 1 && seen.every((result) => result === undefined) };
      hide(false);
      await sleep(60);
      check.stop();
      return { whileHidden, ranWhenShown: count > 0, waiting: check.waiting };
    `),
    { whileHidden: { count: 0, onlyUndefined: true }, ranWhenShown: true, waiting: false },
  );
  // A check registered with whileHidden also runs on a hidden page.
  assert.equal(
    await inPage(page, `
      let count = 0;
      const check = poll("test-while-hidden", () => ++count, () => null, { whileHidden: true });
      hide(true);
      await check.run();
      return count;
    `),
    1,
  );
  // A failure reaches the caller of run(); in a run the timer started it goes to onError. The repeat goes on.
  assert.deepEqual(
    await inPage(page, `
      const errors = [];
      let count = 0;
      const check = poll(
        "test-failure",
        () => { count += 1; throw new Error("failed " + count); },
        (result) => (result === undefined && count < 2 ? 10 : null),
        { onError: (error) => errors.push(error.message) },
      );
      const caught = await check.run().catch((error) => error.message);
      await sleep(60);
      return { caught, errors, count };
    `),
    { caught: "failed 1", errors: ["failed 2"], count: 2 },
  );
  // after() replaces the scheduled run, and stop() cancels it.
  assert.deepEqual(
    await inPage(page, `
      let count = 0;
      const check = poll("test-after", () => ++count, () => null);
      check.after(10);
      check.after(30);
      await sleep(80);
      const afterTwo = count;
      check.after(10);
      check.stop();
      await sleep(40);
      return { afterTwo, afterStop: count, waiting: check.waiting };
    `),
    { afterTwo: 1, afterStop: 1, waiting: false },
  );
}

/** Check the button helper: disabled while the action works, enabled after, and where a failure goes. */
async function testBusyAction(page) {
  assert.deepEqual(
    await inPage(page, `
      const button = document.createElement("button");
      let during;
      await busyAction(button, async () => { await sleep(5); during = button.disabled; });
      const afterSuccess = button.disabled;
      const shown = [];
      await busyAction(button, () => { throw new Error("no"); }, (error) => shown.push(error.message));
      const afterShownFailure = button.disabled;
      const thrown = await busyAction(button, () => { throw new Error("up"); }).catch((error) => error.message);
      return { during, afterSuccess, shown, afterShownFailure, thrown, afterThrownFailure: button.disabled };
    `),
    { during: true, afterSuccess: false, shown: ["no"], afterShownFailure: false, thrown: "up", afterThrownFailure: false },
  );
}

/** Check that the page's own repeating checks run through poll(), and that Settings stops the two it owns. */
async function testPagePolls(page) {
  assert.deepEqual(
    await page.evaluate(() => Object.keys(polls).filter((name) => !name.startsWith("test-")).sort()),
    ["activity", "codeTimes", "github", "login", "refresh", "remote", "usage"],
  );
  await page.waitForFunction(() => polls.refresh.waiting && polls.usage.waiting && polls.github.waiting);
  // The remote-control state is read again only while Settings is open.
  assert.equal(await page.evaluate(() => polls.remote.waiting), false);
  await page.locator("#settings-button").click();
  await page.waitForFunction(() => polls.remote.waiting && polls.login.waiting);
  await page.locator("#close-settings").click();
  // The dialog's close event arrives a moment after the click.
  await page.waitForFunction(() => !polls.remote.waiting && !polls.login.waiting);
  // The list and the quota keep their repeats.
  assert.deepEqual(await page.evaluate(() => [polls.refresh.waiting, polls.usage.waiting]), [true, true]);
}

/** Open the chat page and run the helper checks; page errors go to the caller's list. */
async function testPollHelpers(browser, _output, errors) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("http://127.0.0.1:9137/preview/");
  await page.locator(".chat-row").first().waitFor();
  await testPoll(page);
  await testBusyAction(page);
  await testPagePolls(page);
  await page.close();
}

module.exports = testPollHelpers;
