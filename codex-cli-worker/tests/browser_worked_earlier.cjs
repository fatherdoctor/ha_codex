// Every earlier answer's "Worked for" line opens that exchange's steps, as the latest answer's line does
// (CX-WORKED-EARLIER). Run with tests/web_fixture.py serving the page on port 9137.
// At phone and desktop width:
// 1. Two exchanges in one chat: the first answer becomes an earlier answer with a closed "Worked for" toggle.
// 2. Opening it loads that exchange's saved steps once, from the worker, by its turn id.
// 3. A step's output opens and closes inside the earlier list, and the latest answer's list is unchanged.
// 4. The open line stays open when the chat is drawn again, without a second fetch.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

const base = 'http://127.0.0.1:9137/preview/';
const WORKED = /^Worked for \d+s$/;

/** Send a message in the open chat and wait until its exchange ends with a closed "Worked for" line. */
async function sendAndFinish(page, text) {
  await page.locator('#message').fill(text);
  await page.getByRole('button', {name: 'Send message', exact: true}).click();
  await page.waitForFunction(count => {
    const last = document.querySelectorAll('.exchange')[count - 1];
    const toggle = document.querySelector('#activity .activity-toggle');
    return last && last.querySelector('.answer') && /^Worked for \d+s$/.test(toggle?.textContent || '');
  }, await page.locator('.exchange').count() + 1);
}

async function checkWidth(browser, width, height) {
  const label = `width ${width}`;
  const context = await browser.newContext({viewport: {width, height}});
  const page = await context.newPage();
  const errors = [];
  const turnFetches = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => {
    if (/\/activity\?turn=/.test(request.url())) turnFetches.push(request.url());
  });
  await page.goto(base);
  await page.locator('.chat-row').first().waitFor();
  await page.locator('#new-chat').evaluate(el => el.click());
  await sendAndFinish(page, 'Check the hallway lights');
  const latestSteps = await page.evaluate(() =>
    [...state.activity.steps.values()].filter(step => step.kind !== 'message').length);
  assert.ok(latestSteps > 0, `${label}: the first exchange saved steps`);
  await sendAndFinish(page, 'Now check the porch lights');

  const first = page.locator('.exchange').nth(0);
  const toggle = first.locator('.activity.worked > .activity-toggle');
  assert.match(await toggle.textContent(), WORKED, `${label}: the earlier answer has the Worked for toggle`);
  assert.equal(await toggle.getAttribute('aria-expanded'), 'false', `${label}: it starts closed`);
  assert.equal(await first.locator('.worked-line').count(), 0, `${label}: not plain text any more`);
  assert.equal(turnFetches.length, 0, `${label}: nothing is fetched before the line opens`);

  await toggle.click();
  // A group of several steps opens from its own heading (CX-WORKLOG-GROUPS); a single step shows as a row.
  await first.locator('.activity-steps .step-group-head, .activity-steps .step-group > .step').first().waitFor();
  assert.equal(await toggle.getAttribute('aria-expanded'), 'true', `${label}: the line opens`);
  const heads = first.locator('.activity-steps .step-group-head');
  const headCount = await heads.count();
  assert.ok(headCount > 0, `${label}: the fixture's steps make at least one group with a heading`);
  for (let i = 0; i < headCount; i++) {
    const head = heads.nth(i);
    assert.equal(await head.getAttribute('aria-expanded'), 'false', `${label}: a finished group starts closed`);
    assert.match(await head.textContent(), /^[A-Z]/, `${label}: the heading sums up the group`);
    assert.equal(await head.locator('xpath=..').locator('.step:visible').count(), 0, `${label}: its rows are folded`);
    await head.click();
    assert.equal(await heads.nth(i).getAttribute('aria-expanded'), 'true', `${label}: a tap opens the group`);
  }
  assert.equal(await first.locator('.activity-steps .step:not(:visible)').count(), 0, `${label}: every row shows`);
  const steps = await first.locator('.activity-steps .step').count();
  assert.equal(steps, latestSteps, `${label}: the earlier list shows the same steps it showed as the latest`);
  assert.equal(turnFetches.length, 1, `${label}: one fetch`);
  const turnId = await page.evaluate(() => state.task.turns[0].turn_id);
  assert.ok(turnFetches[0].endsWith(`turn=${turnId}`), `${label}: the fetch names the first exchange`);
  assert.equal(await first.locator('.activity-steps .step-message').count(), 0, `${label}: no message rows`);

  const output = first.locator('.step-output-toggle').first();
  if (await output.count()) {
    await output.click();
    assert.equal(await first.locator('.step-output').count(), 1, `${label}: a step's output opens`);
    await first.locator('.step-output-toggle').first().click();
    assert.equal(await first.locator('.step-output').count(), 0, `${label}: and closes`);
  }
  assert.equal(await page.locator('#activity > .activity-toggle').getAttribute('aria-expanded'), 'false',
    `${label}: the latest answer's line stays closed`);

  await page.evaluate(() => renderTask(true));
  const again = page.locator('.exchange').nth(0).locator('.activity.worked > .activity-toggle');
  assert.equal(await again.getAttribute('aria-expanded'), 'true', `${label}: stays open after a redraw`);
  assert.ok((await page.locator('.exchange').nth(0).locator('.activity-steps .step').count()) > 0,
    `${label}: steps still show after a redraw`);
  assert.equal(await page.locator('.exchange').nth(0).locator('.step-group-head[aria-expanded="false"]').count(), 0,
    `${label}: opened groups stay open after a redraw`);
  assert.equal(turnFetches.length, 1, `${label}: no second fetch`);
  await again.click();
  assert.equal(await page.locator('.exchange').nth(0).locator('.activity-steps').isVisible(), false,
    `${label}: closes again`);
  const words = await page.evaluate(() => {
    const row = (kind, failed) => {
      const node = document.createElement('div');
      node.className = `step step-${kind} is-${failed ? 'failed' : 'done'}`;
      node.dataset.kind = kind;
      return node;
    };
    return [
      stepGroupLabel([row('command'), row('file_change'), row('command', true), row('web_search'), row('mystery')]),
      stepGroupLabel([row('file_change'), row('file_change')]),
    ];
  });
  assert.deepEqual(words, ['Ran 2 commands, edited a file, searched the web, 1 other step · 1 failed', 'Made 2 file edits'],
    `${label}: the heading counts each kind in the order it first ran`);
  assert.deepEqual(errors, [], `${label}: no page errors`);
  await context.close();
}

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage']});
  try {
    for (const [width, height] of [[390, 844], [1280, 900]]) await checkWidth(browser, width, height);
  } finally {
    await browser.close();
  }
  console.log('PASS: earlier answers open their saved steps at phone and desktop width');
})().catch(error => {
  console.error(error);
  process.exit(1);
});
