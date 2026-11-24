// Capture the README screenshots from web_fixture.py: sample data only, light and dark, desktop and phone.
//
// Start a fresh fixture first (python codex-cli-worker/tests/web_fixture.py). Run this file once against it,
// then once more against a fixture started with CODEX_FIXTURE_SIGN_IN=pending for the sign-in shots.
// Before the first shot the script saves sample workspace rules through the page's own save, so that no shot
// shows the "No workspace instructions yet" warning of a new install.
// Files go to SCREENSHOT_DIR (default: screenshots/ at the repository root) under stable names that
// screenshots/SCREENSHOTS.md lists.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const base = 'http://127.0.0.1:9137/preview/';
const output = process.env.SCREENSHOT_DIR || path.join(__dirname, '..', '..', 'screenshots');
const signInMode = process.env.CODEX_FIXTURE_SIGN_IN === 'pending';
const sizes = {desktop: {width: 1280, height: 800}, phone: {width: 390, height: 844}};
// Script errors from every page opened in this run; the run fails if any page reported one.
const pageErrors = [];

// A fixed date keeps the "next run" time the same on every refresh. 12:00 UTC is 07:00 in
// New York on that date, so the daily 07:00 proposal shows 07:00 as its next run.
const sampleTime = Date.parse('2026-11-16T12:00:00Z') / 1000;
const sampleProposal = {
  id: 'sample-proposal', name: 'Morning battery check', approved: false, enabled: false, revision: 'sample',
  prompt: 'List every battery below 20 percent and say which device it belongs to.', mode: 'read-only',
  spec: {kind: 'cron', expression: '0 7 * * *'}, next_due: sampleTime,
};
const sampleSchedule = {
  id: 'sample-on', name: 'Weekly configuration check', approved: true, enabled: true, revision: 'sample',
  prompt: 'Run the configuration check and report anything new.', mode: 'read-only',
  spec: {kind: 'interval', seconds: 7 * 24 * 3600}, next_due: sampleTime + 86400,
};
/** Answer the schedule routes with the sample data above; nothing is saved. */
async function sampleScheduling(page) {
  await page.route('**/schedules**', route => {
    if (new URL(route.request().url()).pathname.includes('/assets/')) return route.continue();
    return route.fulfill({json: {
      ok: true, timezone: 'America/New_York', schedules: [sampleProposal, sampleSchedule],
      runs: [{created: sampleTime - 7200, status: 'completed', task_id: 'sample-task', error: ''}],
    }});
  });
}

// Sample workspace rules: with an AGENTS.md in place the page shows no "No workspace instructions yet" warning.
const sampleRules = '# Rules for this home\n\n- Ask before you change an automation.\n- Check the configuration after each change.\n';

/** Save the sample workspace rules in the fixture, through the same request as the Settings editor. */
async function seedWorkspaceRules(browser) {
  const page = await browser.newPage();
  await page.goto(base);
  const status = await page.evaluate(async content => (await fetch('agents', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({content}),
  })).status, sampleRules);
  assert.equal(status, 200, `saving the sample workspace rules returned HTTP ${status}`);
  await page.close();
}

/** Open a page at one size and color scheme, with the sidebar closed on a phone. */
async function openPage(browser, size, scheme, chatId = null) {
  const page = await browser.newPage({viewport: sizes[size], colorScheme: scheme});
  // The page reopens the conversation it remembers, as it does for a returning user.
  if (chatId) await page.addInitScript(id => localStorage.setItem('codex-last-chat', id), chatId);
  page.on('pageerror', error => pageErrors.push(error.message));
  await sampleScheduling(page);
  // The page asks for the workspace rules after it loads; wait for that answer so the warning check below is real.
  const rulesRead = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/agents'));
  await page.goto(base);
  await page.locator('.chat-row').first().waitFor();
  await rulesRead;
  assert.ok(await page.locator('#agents-hint').isHidden(), 'the "No workspace instructions yet" warning shows');
  return page;
}

async function closeSidebarOnPhone(page, size) {
  if (size !== 'phone') return;
  if (await page.locator('body').evaluate(body => body.classList.contains('sidebar-open')))
    await page.locator('#close-sidebar').click();
  await page.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().right <= 0);
}

/** On a phone the conversation list, with New chat and Settings, opens from a button. */
async function openConversations(page) {
  const conversations = page.getByRole('button', {name: 'Open conversations', exact: true});
  if (await conversations.isVisible()) await conversations.click();
}

async function openSettings(page) {
  await openConversations(page);
  await page.getByRole('button', {name: 'Open settings', exact: true}).click();
}

/** Open a Settings section that starts collapsed; a section that is already open stays open. */
async function expandSection(page, id) {
  const toggle = page.locator(`#${id} > :is(h3, h4) > .settings-toggle`);
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
}

/** Make the window tall enough that each Settings section fits in one capture. */
async function tallWindow(page, size) {
  await page.setViewportSize({...sizes[size], height: 1600});
}

/** Screenshot the area from the top of `first` to the bottom of `last`, both inside Settings. */
async function shootRange(page, first, last, file) {
  await first.scrollIntoViewIfNeeded();
  const top = await first.boundingBox();
  const bottom = await last.boundingBox();
  assert.ok(top && bottom, `nothing to capture for ${file}`);
  const panel = await page.locator('#settings').boundingBox();
  const clip = {x: panel.x, y: top.y - 12, width: panel.width, height: bottom.y + bottom.height - top.y + 24};
  assert.ok(clip.y >= 0 && clip.y + clip.height <= page.viewportSize().height, `${file} does not fit the window`);
  await page.screenshot({path: path.join(output, file), clip, animations: 'disabled'});
}

/** The chat page with one saved conversation open: a change to an automation, explained. */
async function chat(browser, size, scheme) {
  const page = await openPage(browser, size, scheme, 'preview-04');
  await page.getByText('Done.', {exact: true}).first().waitFor();
  await closeSidebarOnPhone(page, size);
  // Start at the question, so the shot reads from what was asked to what was done.
  await page.locator('.message.user').first().evaluate(node => node.scrollIntoView({block: 'start'}));
  await page.screenshot({path: path.join(output, `chat-${scheme}-${size}.png`), animations: 'disabled'});
  return page;
}

/** The Info tab's three pictures: the chat list, the usage area, and the message box below a reply. */
async function info(browser, size, scheme) {
  const page = await openPage(browser, size, scheme, 'preview-04');
  await page.getByText('Done.', {exact: true}).first().waitFor();
  await openConversations(page);
  await page.waitForFunction(() => document.querySelector('#sidebar').getBoundingClientRect().left >= 0);
  // The sample schedule proposal raises the "Schedule proposal waiting" warning; dismiss it, as a person would, so
  // the usage picture shows the usage rows and not that warning.
  const dismiss = page.locator('#attention-dismiss');
  await dismiss.waitFor();
  await dismiss.click();
  await dismiss.waitFor({state: 'hidden'});
  // The pointer stays where the button was, over Settings; move it off, so no row shows its hover color.
  await page.mouse.move(0, 0);
  await shootBetween(page, page.locator('#new-chat'), page.locator('.chats-card'), page.locator('#sidebar'),
    `info-chats-${scheme}-${size}.png`);
  const footer = page.locator('.sidebar-footer');
  await shootBetween(page, footer, footer, page.locator('#sidebar'), `info-usage-${scheme}-${size}.png`);
  await closeSidebarOnPhone(page, size);
  // The end of the answer, then the message box: what a reply looks like and where the next question goes.
  await page.locator('#messages').evaluate(node => { node.scrollTop = node.scrollHeight; });
  await shootBetween(page, page.getByText('Next steps', {exact: true}), page.locator('#composer'),
    page.locator('.compose-area'), `info-reply-${scheme}-${size}.png`);
  return page;
}

/** Screenshot, at the width of `frame`, the area from the top of `first` to the bottom of `last`. */
async function shootBetween(page, first, last, frame, file) {
  const top = await first.boundingBox();
  const bottom = await last.boundingBox();
  const sides = await frame.boundingBox();
  assert.ok(top && bottom && sides, `nothing to capture for ${file}`);
  const clip = {x: Math.max(sides.x, 0), y: Math.max(top.y - 12, 0), width: sides.width};
  clip.height = Math.min(bottom.y + bottom.height + 12, page.viewportSize().height) - clip.y;
  assert.ok(clip.height > 0 && clip.x + clip.width <= page.viewportSize().width, `${file} does not fit the window`);
  await page.screenshot({path: path.join(output, file), clip, animations: 'disabled'});
}

/** A command waiting for one confirmation: what runs, where, and why, with Allow once and Deny. */
async function confirm(browser, size, scheme) {
  const page = await openPage(browser, size, scheme);
  await openConversations(page);
  await page.locator('#new-chat').click();
  await page.locator('#message').fill('Request permission to read the energy history for the last week');
  await page.getByRole('button', {name: 'Send message', exact: true}).click();
  const deny = page.getByRole('button', {name: 'Deny', exact: true});
  await deny.waitFor();
  assert.equal(await page.locator('.permission-card img').count(), 0);
  await closeSidebarOnPhone(page, size);
  // On a phone the card is taller than the space above the message box; end the shot at the buttons instead.
  const align = size === 'phone' ? 'end' : 'start';
  await page.locator('.permission-card').evaluate((node, block) => node.scrollIntoView({block}), align);
  await assertAboveMessageBox(page, deny, 'Deny');
  await assertAboveMessageBox(page, page.getByRole('button', {name: 'Allow once', exact: true}), 'Allow once');
  await page.screenshot({path: path.join(output, `confirm-${scheme}-${size}.png`), animations: 'disabled'});
  // Answer it, so the fixture's simulated command ends, then delete the chat, so the next capture shows the
  // same conversation list as this one.
  await deny.click();
  await page.locator('.permission-card strong').filter({hasText: /^Denied$/}).waitFor();
  await deleteCurrentChat(page);
  return page;
}

/** Fail when `button` is not fully visible between the top of the window and the top of the message box. */
async function assertAboveMessageBox(page, button, name) {
  const box = await button.boundingBox();
  const composer = await page.locator('#message').boundingBox();
  assert.ok(box && composer, `${name} or the message box is not on the page`);
  assert.ok(box.y >= 0 && box.y + box.height <= composer.y, `${name} is hidden in the confirm shot`);
}

/** Delete the chat the page has open, retrying while the fixture still counts its command as running. */
async function deleteCurrentChat(page) {
  const id = await page.evaluate(() => localStorage.getItem('codex-last-chat'));
  assert.ok(id, 'the confirm chat has no id');
  for (let attempt = 0; attempt < 20; attempt++) {
    const status = await page.evaluate(
      async taskId => (await fetch(`tasks/${encodeURIComponent(taskId)}`, {method: 'DELETE'})).status, id);
    if (status === 200) return;
    assert.equal(status, 409, `deleting the confirm chat returned HTTP ${status}`);
    await page.waitForTimeout(250);
  }
  assert.fail('the confirm chat was still running after 5 seconds');
}

/** Settings: a schedule proposal waiting for approval. */
async function settings(browser, size, scheme) {
  const page = await openPage(browser, size, scheme);
  await tallWindow(page, size);
  await openSettings(page);
  await expandSection(page, 'scheduled-work');
  const schedules = page.locator('#schedule-list');
  await schedules.getByRole('button', {name: 'Approve this proposal', exact: true}).waitFor();
  await shootRange(page, page.locator('#scheduled-work h3').first(), schedules, `schedule-proposal-${scheme}-${size}.png`);
  return page;
}

/** Settings while a sign-in waits for its code; the code and its QR code are blurred. */
async function signIn(browser, size, scheme) {
  const page = await openPage(browser, size, scheme);
  await tallWindow(page, size);
  await openSettings(page);
  await expandSection(page, 'accounts-section');
  await expandSection(page, 'chatgpt-section');
  const card = page.locator('#login-card');
  await card.locator('.signin-code').waitFor();
  // A QR image that failed to load (the fixture answers 500 without the qrcode package) is still
  // "complete", with no pixels; a shot of it shows a blank square, so stop instead.
  const qrWidth = await card.locator('.signin-qr').evaluate(image => (image.complete ? Promise.resolve()
    : new Promise(done => { image.onload = done; image.onerror = done; })).then(() => image.naturalWidth));
  if (!qrWidth) throw new Error('The sign-in QR code did not load; install the qrcode package (see SCREENSHOTS.md).');
  // Cover the sample code with a plain block and blur the QR code past reading; both are samples, but a
  // reader cannot tell that from the picture.
  await page.addStyleTag({content:
    '.signin-code { color: transparent !important; background: #8888; border-radius: 4px; }' +
    ' .signin-qr { filter: blur(16px); }'});
  await shootRange(page, page.locator('#settings h4').filter({hasText: 'ChatGPT account'}), card,
    `sign-in-${scheme}-${size}.png`);
  return page;
}

(async () => {
  fs.mkdirSync(output, {recursive: true});
  const browser = await chromium.launch({headless: true});
  const shots = signInMode ? [signIn] : [chat, info, confirm, settings];
  try {
    await seedWorkspaceRules(browser);
    for (const shoot of shots)
      for (const scheme of ['light', 'dark'])
        for (const size of Object.keys(sizes)) {
          const page = await shoot(browser, size, scheme);
          await page.close();
        }
  } finally { await browser.close(); }
  assert.deepEqual(pageErrors, []);
  console.log(`Saved the ${signInMode ? 'sign-in' : 'chat, Info tab, confirmation and Settings'} screenshots to ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
