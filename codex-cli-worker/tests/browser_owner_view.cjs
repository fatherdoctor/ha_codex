// What the owner sees on a phone (390 px) and on a desktop (1280 px), checked on every pull request:
// 1. Settings shows its sections in order, and nothing in it is wider than the window.
// 2. The ChatGPT and GitHub panels show their main controls, in order and inside the window, when signed out,
//    while signing in and when signed in.
// 3. The Remote connection window shows Turn on when remote control is off, and the pairing code, Copy code,
//    New pairing code and Turn off when it is on, inside the window.
// 4. A chat's final answer shows once, under its in-between messages, which show once each and in order: in the saved
//    first chat, whose stored steps hold a copy of the answer, and in a new chat sent from the page.
// The checks look at structure (which controls show, their order, their boxes), not at pixels. The panel states
// are worker responses of the shape the worker sends; the chat comes from tests/web_fixture.py, which plays a
// recorded Codex event stream. Run with tests/web_fixture.py serving the page. Every code and name is synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

const PAGE = 'http://127.0.0.1:9137/preview/';
const VIEWPORTS = [{width: 390, height: 844}, {width: 1280, height: 800}];
const QR = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#fff"/></svg>';
const PAIRING_QR = `data:image/svg+xml;base64,${Buffer.from(QR).toString('base64')}`;

const login = ok => ({status_ok: ok, has_auth_file: ok, message: ok ? 'Signed in using ChatGPT.' : ''});
const AUTH = {
  'signed out': {status: 'idle', pending: false, codex_login: login(false)},
  'signing in': {
    status: 'waiting_for_user', pending: true, process_running: true, login_id: 'L1', user_code: 'TEST-0000',
    verification_url: 'https://auth.openai.com/codex/device', qr_path: 'auth/qr.svg?login=L1', expires_in: 900,
    expires_at: '2099-01-01T00:00:00Z', codex_login: login(false),
  },
  'signed in': {status: 'idle', pending: false, codex_login: login(true)},
};
const githubBase = {
  state: 'disconnected', kind: null, account: null, client_id_source: 'built_in', repositories: [],
  expected_repositories: [], missing: [], extra: [], permissions: null, permission_problem: null, expires_at: null,
  connected_at: null, last_check: null, last_test: null, revoke_url: 'https://github.com/settings/apps/authorizations',
  flow: {status: 'idle'},
};
const GITHUB = {
  'signed out': githubBase,
  'signing in': {...githubBase, state: 'connecting', flow: {status: 'waiting_for_user', user_code: 'TEST-0001',
    verification_uri: 'https://github.com/login/device', expires_at: '2099-01-01T00:00:00Z', expires_in: 900, error: ''}},
  'signed in': {...githubBase, state: 'connected', kind: 'app', account: {login: 'example-owner'},
    connected_at: '2026-11-15T13:15:00Z', expires_at: '2099-01-01T00:00:00Z', permissions: {contents: 'write'}},
};
// The controls each panel shows, in page order. A link counts by its text; a sign-in or pairing code counts as
// "code". While signing in and in the Remote window the list is exact. Signed out and signed in it holds the main
// controls, which must show in this order; other controls may show between them.
const CHATGPT_CONTROLS = {
  'signed out': {exact: false, names: ['Sign in']},
  'signing in': {exact: true, names: ['Open the ChatGPT sign-in page', 'code', 'Cancel']},
  'signed in': {exact: false, names: ['Sign out']},
};
const GITHUB_CONTROLS = {
  'signed out': {exact: false, names: ['Connect GitHub']},
  'signing in': {exact: true, names: ['Open github.com/login/device', 'code', 'Cancel']},
  'signed in': {exact: false, names: ['Read test', 'Check now', 'Disconnect']},
};
const SECTIONS = ['Accounts and connections', 'Full access', 'Text size', 'Scheduled runs', 'Workspace instructions',
  'Advanced'];
const remoteBase = {available: true, configured_name: 'Home Assistant', machine_name: '', log: []};
const REMOTE = {
  off: {...remoteBase, enabled: false, state: 'off'},
  on: {...remoteBase, enabled: true, state: 'pairing', machine_name: 'Home Assistant', pairing_code: 'SAMPLE-0000',
    pairing_qr: PAIRING_QR, pairing_url: 'https://chatgpt.com/codex/pair?pairing_code=SAMPLE-NOT-VALID'},
};
const REMOTE_CONTROLS = {
  off: {exact: true, names: ['Turn on']},
  on: {exact: true, names: ['Continue in ChatGPT', 'code', 'Copy code', 'New pairing code', 'Turn off']},
};

/** Each shown element under root that reaches past the window's sides, after clipping by a scrolling parent. */
function outsideWindow(root) {
  // This function and shownControls run in the page, one at a time, so each carries its own helper.
  const shown = el => el.checkVisibility({visibilityProperty: true});
  const found = [];
  for (const el of root.querySelectorAll('*')) {
    if (!shown(el)) continue;
    let {left, right} = el.getBoundingClientRect();
    if (right - left < 1) continue;
    for (let parent = el.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
      if (getComputedStyle(parent).overflowX === 'visible') continue;
      const box = parent.getBoundingClientRect();
      left = Math.max(left, box.left);
      right = Math.min(right, box.right);
    }
    if (right - left >= 1 && (left < -0.5 || right > innerWidth + 0.5))
      found.push(`${el.tagName.toLowerCase()}#${el.id}.${el.className} ${Math.round(left)}-${Math.round(right)}`);
  }
  const pageWidth = document.documentElement.scrollWidth;
  if (pageWidth > innerWidth) found.push(`page scrolls sideways to ${pageWidth}`);
  // A box that scrolls hides what is past its side, so the loop above cannot see it: the box must not scroll
  // sideways. Code blocks and text fields may.
  for (const el of [root, ...root.querySelectorAll('*')]) {
    if (!shown(el) || el.matches('pre, pre *, textarea')) continue;
    if (['auto', 'scroll'].includes(getComputedStyle(el).overflowX) && el.scrollWidth > el.clientWidth + 1)
      found.push(`${el.tagName.toLowerCase()}#${el.id} scrolls sideways (${el.scrollWidth} > ${el.clientWidth})`);
  }
  return found;
}

/** The shown controls under selector, in page order, with whether each sits inside the window's sides. */
function shownControls(selector) {
  const shown = el => el.checkVisibility({visibilityProperty: true});
  const root = document.querySelector(selector);
  const controls = [...root.querySelectorAll('a, button:not(.settings-toggle), .signin-code, .pairing-code')].filter(shown);
  return controls.map(el => {
    const box = el.getBoundingClientRect();
    const name = el.matches('.signin-code, .pairing-code') ? 'code' : el.textContent.trim().replace(/\s+/g, ' ');
    return {name, inside: box.left >= -0.5 && box.right <= innerWidth + 0.5 && box.width > 0 && box.height > 0};
  });
}

/** Load the page with the given worker responses and return it with its script errors. */
async function openPage(browser, viewport, {auth = AUTH['signed in'], github = GITHUB['signed out'], remote = REMOTE.off} = {}) {
  const page = await browser.newPage({viewport});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/auth/status', route => route.fulfill({json: {ok: true, auth}}));
  await page.route('**/auth/qr.svg*', route => route.fulfill({contentType: 'image/svg+xml', body: QR}));
  await page.route('**/github', route => route.fulfill({json: {ok: true, github}}));
  await page.route('**/remote-control', route => route.fulfill({json: {ok: true, remote_control: remote}}));
  await page.goto(PAGE);
  await page.locator('#login-status').waitFor({state: 'attached'});
  return {page, errors};
}

/** Open Settings; on a phone the conversations panel holds its button. */
async function openSettings(page) {
  const conversations = page.getByRole('button', {name: 'Open conversations', exact: true});
  if (await conversations.isVisible()) await conversations.click();
  await page.getByRole('button', {name: 'Open settings', exact: true}).click();
  await page.locator('#settings').waitFor({state: 'visible'});
}

/** Unfold a Settings section by its id, once. */
async function unfold(page, id) {
  const toggle = page.locator(`#${id} .settings-toggle`).first();
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click();
}

/** Assert that a panel shows the expected controls in order, each inside the window. */
async function assertControls(page, selector, expected, label) {
  const controls = await page.evaluate(shownControls, selector);
  const names = controls.map(c => c.name);
  if (expected.exact) assert.deepEqual(names, expected.names, `${label}: controls and their order`);
  else {
    const order = expected.names.map(name => names.indexOf(name));
    assert.ok(order.every((at, i) => at >= 0 && (i === 0 || at > order[i - 1])),
      `${label}: ${JSON.stringify(expected.names)} show in this order; shown: ${JSON.stringify(names)}`);
  }
  for (const control of controls) assert.ok(control.inside, `${label}: "${control.name}" reaches past the window`);
}

/** Settings: the sections in order, and nothing wider than the window, with every section unfolded. */
async function checkSettings(browser, viewport, label) {
  const {page, errors} = await openPage(browser, viewport);
  await openSettings(page);
  const titles = await page.locator('#settings > section > h3 .settings-title').allTextContents();
  assert.deepEqual(titles, SECTIONS, `${label}: Settings sections and their order`);
  assert.ok(await page.getByRole('button', {name: 'Close settings', exact: true}).isVisible(), `${label}: Close shows`);
  assert.deepEqual(await page.locator('#settings').evaluate(outsideWindow), [], `${label}: folded Settings fits the window`);
  // One top-level section is open at a time, so each is measured as it opens; a part opens its section too.
  for (const toggle of await page.locator('#settings .settings-toggle').all()) {
    if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.evaluate(el => el.click());
    const title = await toggle.locator('.settings-title').textContent();
    assert.deepEqual(await page.locator('#settings').evaluate(outsideWindow), [], `${label}: Settings with ${title} open fits the window`);
  }
  assert.deepEqual(errors, [], `${label}: no script error`);
  await page.close();
}

/** The ChatGPT and GitHub panels in one state. */
async function checkAccounts(browser, viewport, state, label) {
  const {page, errors} = await openPage(browser, viewport, {auth: AUTH[state], github: GITHUB[state]});
  await openSettings(page);
  for (const id of ['accounts-section', 'chatgpt-section', 'github-section']) await unfold(page, id);
  if (state === 'signing in') {
    await page.locator('#login-card .signin-code').waitFor();
    await page.locator('#github-flow .signin-code').waitFor();
  } else {
    await page.locator('#login-status').filter({hasNotText: 'Loading'}).waitFor();
    await page.locator('#github-status').filter({hasNotText: 'Loading'}).waitFor();
  }
  await assertControls(page, '#chatgpt-section', CHATGPT_CONTROLS[state], `${label} ChatGPT ${state}`);
  await assertControls(page, '#github-section', GITHUB_CONTROLS[state], `${label} GitHub ${state}`);
  assert.deepEqual(await page.locator('#accounts-section').evaluate(outsideWindow), [], `${label} ${state}: accounts fit`);
  assert.deepEqual(errors, [], `${label} ${state}: no script error`);
  await page.close();
}

/** The Remote connection window with remote control off or on. */
async function checkRemote(browser, viewport, state, label) {
  const {page, errors} = await openPage(browser, viewport, {remote: REMOTE[state]});
  await openSettings(page);
  await unfold(page, 'accounts-section');
  await unfold(page, 'app-pairing-section');
  await page.getByRole('button', {name: 'Manage remote connection', exact: true}).click();
  const dialog = page.locator('#connection-dialog');
  await dialog.waitFor({state: 'visible'});
  await page.locator('#remote-control-status').filter({hasNotText: 'Loading'}).waitFor();
  if (state === 'on') await page.locator('#copy-pairing-code').waitFor();
  assert.ok(await page.getByRole('button', {name: 'Close remote connection', exact: true}).isVisible(), `${label}: Close shows`);
  await assertControls(page, '#remote-control-section', REMOTE_CONTROLS[state], `${label} Remote ${state}`);
  assert.deepEqual(await dialog.evaluate(outsideWindow), [], `${label} Remote ${state}: the window fits`);
  assert.deepEqual(errors, [], `${label} Remote ${state}: no script error`);
  await page.close();
}

/** In the open chat: the final answer shows once, under the two in-between messages, which show once each and in order. */
async function assertAnswerOnce(page, label) {
  const seen = await page.locator('#messages').evaluate(list => {
    const answers = [...list.querySelectorAll('.answer .message:not(.details)')];
    const answer = answers.at(-1).textContent.trim();
    const interim = [...list.querySelectorAll('.interim')];
    return {
      answer,
      count: list.innerText.split(answer).length - 1,
      interim: interim.map(el => el.textContent.trim()),
      answerLast: interim.every(el => el.compareDocumentPosition(answers.at(-1)) & Node.DOCUMENT_POSITION_FOLLOWING),
    };
  });
  assert.ok(seen.answer.length > 10, `${label}: the final answer has text`);
  assert.equal(seen.count, 1, `${label}: the final answer shows once`);
  assert.equal(seen.interim.length, 2, `${label}: two in-between messages`);
  assert.match(seen.interim[0], /^I'll read the automation/, `${label}: first in-between message first`);
  assert.match(seen.interim[1], /^The file reads fine/, `${label}: second in-between message second`);
  assert.ok(seen.answerLast, `${label}: the answer comes after the in-between messages`);
}

/** The saved first chat, whose stored steps hold a copy of the final answer, and a new chat sent from the page. */
async function checkAnswerOnce(browser, viewport, label) {
  const page = await browser.newPage({viewport});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  // The page reopens the chat it remembers. Earlier checks add chats to the fixture and push this one down the list,
  // so the check opens it this way, not from the list.
  await page.addInitScript(() => localStorage.setItem('codex-last-chat', 'preview-00'));
  await page.goto(PAGE);
  await page.locator('#activity-text .interim').first().waitFor();
  await page.locator('#messages .answer').first().waitFor();
  await assertAnswerOnce(page, `${label} saved chat`);
  await page.getByRole('button', {name: 'New chat', exact: false}).first().evaluate(el => el.click());
  const prompt = `Owner view check at ${viewport.width} px`;
  await page.locator('#message').fill(prompt);
  await page.getByRole('button', {name: 'Send message', exact: true}).click();
  await page.locator('#messages').getByText(`Preview response: ${prompt}`, {exact: true}).first().waitFor();
  await page.locator('#activity-text .interim').first().waitFor();
  await assertAnswerOnce(page, `${label} new chat`);
  assert.deepEqual(errors, [], `${label}: no script error`);
  await page.close();
}

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const viewport of VIEWPORTS) {
      const label = `${viewport.width} px`;
      await checkSettings(browser, viewport, label);
      for (const state of Object.keys(AUTH)) await checkAccounts(browser, viewport, state, label);
      for (const state of Object.keys(REMOTE)) await checkRemote(browser, viewport, state, label);
      await checkAnswerOnce(browser, viewport, label);
    }
    console.log('PASS: at 390 and 1280 px, Settings sections in order and inside the window; ChatGPT and GitHub panels signed out, signing in and signed in with their controls in order; the Remote connection window off and on; a final answer shown once under its in-between messages');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
