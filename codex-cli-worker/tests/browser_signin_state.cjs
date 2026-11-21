// The signing-in state of the ChatGPT and GitHub panels in Settings: only the sign-in link, the code with its timer
// and (ChatGPT) the folded QR code show, then one quiet Cancel. No Sign out, no Codex availability paragraph and no
// "confirmed access" line while signing in; the signed-out and signed-in states keep them. Runs at phone width and
// on a desktop. browser_settings_phone.cjs runs this file; it also runs alone. Run with tests/web_fixture.py
// serving the page. Every code, name and address is synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

const PAGE = 'http://127.0.0.1:9137/preview/';
const VIEWPORTS = [{width: 360, height: 640}, {width: 1280, height: 800}];
const QR = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#fff"/></svg>';
const ACCESS = 'A successful turn confirmed Codex access with the current sign-in.';
const waiting = {
  status: 'waiting_for_user', pending: true, process_running: true, login_id: 'L1', user_code: 'TEST-0000',
  verification_url: 'https://auth.openai.com/codex/device', qr_path: 'auth/qr.svg?login=L1', expires_in: 900,
  expires_at: '2026-11-16T13:15:00Z', codex_login: {status_ok: false, has_auth_file: false, message: ''},
};
const signedOut = {status: 'idle', pending: false, codex_login: {status_ok: false, has_auth_file: false, message: ''}};
const signedIn = {status: 'idle', pending: false, codex_login: {status_ok: true, has_auth_file: true, message: 'Signed in using ChatGPT.'}};
const githubBase = {
  state: 'disconnected', kind: null, account: null, client_id_source: 'built_in', repositories: [],
  expected_repositories: [], missing: [], extra: [], permissions: null, permission_problem: null, expires_at: null,
  connected_at: null, last_check: null, last_test: null, revoke_url: 'https://github.com/settings/apps/authorizations',
  flow: {status: 'idle'},
};
const githubWaiting = {...githubBase, state: 'connecting', flow: {status: 'waiting_for_user', user_code: 'TEST-0001',
  verification_uri: 'https://github.com/login/device', expires_at: '2026-11-16T13:15:00Z', expires_in: 900, error: ''}};
const githubConnected = {...githubBase, state: 'connected', kind: 'app', account: {login: 'example-owner'},
  connected_at: '2026-11-15T13:15:00Z', expires_at: '2026-12-15T13:15:00Z', permissions: {contents: 'write'}};

/** Open Settings with the given ChatGPT and GitHub states and the accounts part unfolded. */
async function openSettings(browser, viewport, auth, github) {
  const page = await browser.newPage({viewport});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/auth/status', route => route.fulfill({json: {ok: true, auth}}));
  await page.route('**/auth/qr.svg*', route => route.fulfill({contentType: 'image/svg+xml', body: QR}));
  await page.route('**/github', route => route.fulfill({json: {ok: true, github}}));
  await page.goto(PAGE);
  await page.locator('#login-status').waitFor({state: 'attached'});
  const conversations = page.getByRole('button', {name: 'Open conversations', exact: true});
  if (await conversations.isVisible()) await conversations.click();
  await page.getByRole('button', {name: 'Open settings', exact: true}).click();
  for (const id of ['accounts-section', 'chatgpt-section', 'github-section'])
    await page.locator(`#${id} .settings-toggle`).first().click();
  return {page, errors};
}

/** The ChatGPT panel while signing in: link, code with timer, folded QR, one quiet Cancel. */
async function checkChatgptSigningIn(page, phone, label) {
  await page.locator('#login-card .signin-code').waitFor();
  const visible = await page.locator('#chatgpt-section').evaluate(root => [...root.querySelectorAll(
    '#login-status, #codex-access, #login-requirement, #login-card a, #login-card .signin-code, ' +
    '#login-card .signin-expiry, #login-card .signin-qr-fold > summary, #login, #login-cancel, #logout')]
    .filter(element => element.getClientRects().length > 0).map(element => element.id || element.className ||
      element.tagName.toLowerCase()));
  assert.deepEqual(visible,
    ['signin-link', 'signin-code', 'muted signin-expiry', 'summary', 'login-cancel'],
    `${label}: ChatGPT panel shows the link, the code, the QR fold and Cancel, in that order`);
  assert.ok(await page.locator('#login-card .signin-expiry').isVisible(), `${label}: the code timer shows`);
  const text = await page.locator('#chatgpt-section').innerText();
  for (const gone of ['Sign out', 'Check OpenAI', 'availability', ACCESS])
    assert.ok(!text.includes(gone), `${label}: "${gone}" shows while signing in`);
  assert.equal(await page.locator('#login-card .signin-qr-fold').evaluate(d => d.open), !phone,
    `${label}: QR code ${phone ? 'folded' : 'open'} on start`);
  const link = page.locator('#login-card a.signin-link');
  const background = await link.evaluate(a => getComputedStyle(a).backgroundColor);
  assert.notEqual(background, 'rgba(0, 0, 0, 0)', `${label}: the sign-in link is a filled button`);
  assert.equal(await page.locator('#login-cancel').evaluate(b => b.classList.contains('subtle')), true,
    `${label}: Cancel is the quiet kind of button`);
  const order = await page.evaluate(() => ['#login-card', '#login-cancel']
    .map(selector => document.querySelector(selector).getBoundingClientRect().top));
  assert.ok(order[0] < order[1], `${label}: Cancel sits below the card`);
}

/** The GitHub panel while signing in: link, code with timer, one quiet Cancel; nothing else. */
async function checkGithubSigningIn(page, label) {
  await page.locator('#github-flow .signin-code').waitFor();
  const names = await page.locator('#github-section').evaluate(root => [...root.querySelectorAll(
    '#github-status, #github-details, #github-repositories, #github-flow a, #github-flow .signin-code, ' +
    '#github-token, #github-local, #github-connect, #github-cancel, #github-test, #github-check, ' +
    '#github-accept, #github-disconnect, #github-test-result, p.muted')]
    .filter(element => element.getClientRects().length > 0)
    .map(element => element.id || element.className || element.tagName.toLowerCase()));
  assert.deepEqual(names, ['signin-link', 'signin-code', 'muted signin-expiry', 'github-cancel'],
    `${label}: GitHub panel shows the link, the code and Cancel, in that order`);
  assert.ok(await page.locator('#github-flow .signin-expiry').isVisible(), `${label}: the code timer shows`);
  const text = await page.locator('#github-section').innerText();
  for (const gone of ['Disconnect', 'Use a token instead', 'Lets the assistant'])
    assert.ok(!text.includes(gone), `${label}: "${gone}" shows while signing in`);
  assert.equal(await page.locator('#github-cancel').evaluate(b => b.classList.contains('subtle')), true,
    `${label}: Cancel is the quiet kind of button`);
}

/** The other states keep what the signing-in state hides. */
async function checkOtherStates(browser) {
  let {page, errors} = await openSettings(browser, VIEWPORTS[1], signedOut, githubBase);
  let text = await page.locator('#chatgpt-section').innerText();
  assert.ok(text.includes('Check current availability'), 'signed out: the availability text shows');
  assert.ok(await page.locator('#login').isVisible(), 'signed out: Sign in shows');
  assert.ok(await page.locator('#github-token summary').isVisible(), 'GitHub disconnected: the token option shows');
  assert.deepEqual(errors, []);
  await page.close();

  ({page, errors} = await openSettings(browser, VIEWPORTS[1], signedIn, githubConnected));
  assert.ok(await page.locator('#logout').isVisible(), 'signed in: Sign out shows');
  assert.ok(await page.locator('#github-disconnect').isVisible(), 'GitHub connected: Disconnect shows');
  assert.ok(await page.locator('#github-details').isVisible(), 'GitHub connected: the details show');
  assert.equal(await page.locator('#chatgpt-section.signing-in, #github-section.signing-in').count(), 0,
    'no panel is in the signing-in state');
  text = await page.locator('#chatgpt-section').innerText();
  assert.ok(text.includes('Signed in'), 'signed in: the status line shows');
  assert.deepEqual(errors, []);
  await page.close();

  // Connect again while connected: the new sign-in shows alone, without Disconnect or the old details.
  ({page, errors} = await openSettings(browser, VIEWPORTS[0],  signedIn,
    {...githubConnected, flow: githubWaiting.flow}));
  await page.locator('#github-flow .signin-code').waitFor();
  assert.ok(!(await page.locator('#github-disconnect').isVisible()), 'connect again: Disconnect is hidden');
  assert.ok(!(await page.locator('#github-details').isVisible()), 'connect again: the details are hidden');
  assert.ok(await page.locator('#github-cancel').isVisible(), 'connect again: Cancel shows');
  assert.deepEqual(errors, []);
  await page.close();
}

/** Run every check in an open browser. */
async function run(browser) {
  for (const viewport of VIEWPORTS) {
    const phone = viewport.width < 700;
    const label = `${viewport.width}x${viewport.height}`;
    const {page, errors} = await openSettings(browser, viewport, waiting, githubWaiting);
    await checkChatgptSigningIn(page, phone, label);
    await checkGithubSigningIn(page, label);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true,
      `${label}: the page scrolls sideways`);
    assert.equal(await page.locator('#settings').evaluate(d => d.scrollWidth <= d.clientWidth), true,
      `${label}: the settings panel scrolls sideways`);
    assert.deepEqual(errors, []);
    await page.close();
  }
  await checkOtherStates(browser);
  console.log('PASS: signing-in panels (ChatGPT and GitHub) at 360 and 1280 px show the link, the code with its timer, '
    + 'the folded QR code (ChatGPT) and one Cancel; signed-out and signed-in panels keep their text and buttons');
}

module.exports = run;

if (require.main === module) {
  (async () => {
    const browser = await chromium.launch({headless: true});
    try {
      await run(browser);
    } finally {
      await browser.close();
    }
  })().catch(error => { console.error(error); process.exitCode = 1; });
}
