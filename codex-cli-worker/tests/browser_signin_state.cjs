// The signing-in state of the ChatGPT and GitHub panels in Settings: only the sign-in link, the code with its timer
// and (ChatGPT) the folded QR code show, then one quiet Cancel. No Sign out, no Codex requirement paragraph and no
// account-access line while signing in; the signed-out and signed-in states keep them. Runs at phone width and
// on a desktop. browser_settings_phone.cjs runs this file; it also runs alone. Run with tests/web_fixture.py
// serving the page. Every code, name and address is synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

const PAGE = 'http://127.0.0.1:9137/preview/';
const VIEWPORTS = [{width: 360, height: 640}, {width: 1280, height: 800}];
const QR = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#fff"/></svg>';
// The worker names an account-access line only when a sign-in is needed (account_access.py).
const ACCESS = 'Sign in with a ChatGPT account that has Codex access.';
const waiting = {
  status: 'waiting_for_user', pending: true, process_running: true, login_id: 'L1', user_code: 'TEST-0000',
  verification_url: 'https://auth.openai.com/codex/device', qr_path: 'auth/qr.svg?login=L1', expires_in: 900,
  expires_at: '2026-11-16T13:15:00Z', codex_login: {status_ok: false, has_auth_file: false, message: ''},
};
const signedOut = {status: 'idle', pending: false, codex_login: {status_ok: false, has_auth_file: false, message: ''}};
const signedIn = {status: 'idle', pending: false, codex_login: {status_ok: true, has_auth_file: true,
  message: 'Signed in using ChatGPT.', server_check: 'ok', server_checked_at: '2026-11-16T13:15:00Z'}};
// The worker status read behind the plan and weekly quota rows; the email must never reach the page.
const STATUS_EXTRA = {codex_usage: {status: 'ok', weekly_percent: 64, five_hour_percent: 90},
  account_access: {state: 'unconfirmed', plan_type: 'plus', message: ACCESS, email: 'SYNTHETIC_PRIVATE_ACCOUNT'}};
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
  await page.route('**/status', async route => {
    if (new URL(route.request().url()).pathname.endsWith('/auth/status')) return route.fallback();
    const response = await route.fetch();
    return route.fulfill({response, json: {...await response.json(), ...STATUS_EXTRA}});
  });
  await page.goto(PAGE);
  await page.locator('#login-status').waitFor({state: 'attached'});
  const conversations = page.getByRole('button', {name: 'Open conversations', exact: true});
  if (await conversations.isVisible()) await conversations.click();
  await page.getByRole('button', {name: 'Open settings', exact: true}).click();
  for (const id of ['accounts-section', 'chatgpt-section', 'github-section'])
    await page.locator(`#${id} .settings-toggle`).first().click();
  return {page, errors};
}

/** The link's text color must differ from its background, or the label is invisible (the older #login-card rule did this). */
async function assertLinkLabelShows(page, selector, label) {
  const colors = await page.locator(selector).evaluate(a => {
    const style = getComputedStyle(a);
    return {text: style.color, background: style.backgroundColor};
  });
  assert.notEqual(colors.text, colors.background, `${label}: the sign-in link label has the color of its button`);
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
  await assertLinkLabelShows(page, '#login-card a.signin-link', `${label} Settings ChatGPT`);
  await assertLinkLabelShows(page, '#signin-banner-card a.signin-link', `${label} banner`);
  if (phone) {
    const height = await link.evaluate(a => a.getBoundingClientRect().height);
    assert.ok(height >= 44, `${label}: the sign-in link is ${height} px tall, under 44`);
  }
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
    '#github-token, #github-local, #github-connect, #github-cancel, ' +
    '#github-accept, #github-disconnect, p.muted')]
    .filter(element => element.getClientRects().length > 0)
    .map(element => element.id || element.className || element.tagName.toLowerCase()));
  assert.deepEqual(names, ['signin-link', 'signin-code', 'muted signin-expiry', 'github-cancel'],
    `${label}: GitHub panel shows the link, the code and Cancel, in that order`);
  assert.ok(await page.locator('#github-flow .signin-expiry').isVisible(), `${label}: the code timer shows`);
  const text = await page.locator('#github-section').innerText();
  for (const gone of ['Sign out', 'Use a token instead', 'Lets the assistant'])
    assert.ok(!text.includes(gone), `${label}: "${gone}" shows while signing in`);
  await assertLinkLabelShows(page, '#github-flow a.signin-link', `${label} Settings GitHub`);
  assert.equal(await page.locator('#github-cancel').evaluate(b => b.classList.contains('subtle')), true,
    `${label}: Cancel is the quiet kind of button`);
}

/** Signed in, the ChatGPT part lists its details like GitHub's, and never the account's email. */
async function checkChatgptDetails(page) {
  const details = page.locator('#chatgpt-details');
  await details.locator('dd', {hasText: '64% left'}).waitFor();
  const rows = await details.evaluate(dl => [...dl.querySelectorAll('dt')].map(dt => dt.textContent));
  assert.deepEqual(rows, ['Sign-in', 'Plan', 'Last check', 'Weekly quota'], 'signed in: the ChatGPT detail rows');
  const text = await details.innerText();
  for (const shown of ['ChatGPT account', 'Plus', 'OpenAI accepted the sign-in'])
    assert.ok(text.includes(shown), `signed in: the details show "${shown}"`);
  assert.doesNotMatch(await page.locator('body').innerText(), /SYNTHETIC_PRIVATE_ACCOUNT/, 'the email shows');
}

/** The other states keep what the signing-in state hides. */
async function checkOtherStates(browser) {
  let {page, errors} = await openSettings(browser, VIEWPORTS[1], signedOut, githubBase);
  let text = await page.locator('#chatgpt-section').innerText();
  assert.ok(text.includes('A ChatGPT account with Codex access is required.'), 'signed out: the requirement shows');
  assert.ok(!/Free and Go|availability/.test(text), 'signed out: no account-type availability text');
  assert.ok(await page.locator('#login').isVisible(), 'signed out: Sign in shows');
  assert.ok(!(await page.locator('#logout').isVisible()), 'signed out: Sign out is hidden');
  assert.ok(!(await page.locator('#chatgpt-details').isVisible()), 'signed out: no ChatGPT details');
  assert.ok(await page.locator('#github-token summary').isVisible(), 'GitHub disconnected: the token option shows');
  assert.deepEqual(errors, []);
  await page.close();

  ({page, errors} = await openSettings(browser, VIEWPORTS[1], signedIn, githubConnected));
  assert.ok(await page.locator('#logout').isVisible(), 'signed in: Sign out shows');
  assert.ok(!(await page.locator('#login').isVisible()), 'signed in: Sign in is hidden');
  await checkChatgptDetails(page);
  assert.ok(await page.locator('#github-disconnect').isVisible(), 'GitHub connected: Sign out shows');
  assert.ok(await page.locator('#github-details').isVisible(), 'GitHub connected: the details show');
  assert.equal(await page.locator('#chatgpt-section.signing-in, #github-section.signing-in').count(), 0,
    'no panel is in the signing-in state');
  text = await page.locator('#chatgpt-section').innerText();
  assert.ok(text.includes('Signed in'), 'signed in: the status line shows');
  assert.deepEqual(errors, []);
  await page.close();

  // Sign in while connected: the new sign-in shows alone, without Sign out or the old details.
  ({page, errors} = await openSettings(browser, VIEWPORTS[0],  signedIn,
    {...githubConnected, flow: githubWaiting.flow}));
  await page.locator('#github-flow .signin-code').waitFor();
  assert.ok(!(await page.locator('#github-disconnect').isVisible()), 'sign in again: Sign out is hidden');
  assert.ok(!(await page.locator('#github-details').isVisible()), 'connect again: the details are hidden');
  for (const id of ['github-repositories', 'github-local'])
    assert.ok(!(await page.locator(`#${id}`).isVisible()), `connect again: #${id} is hidden`);
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
    + 'the folded QR code (ChatGPT) and one Cancel; signed-out and signed-in panels keep their text and buttons; Sign in '
    + 'only while signed out, and the ChatGPT details only while signed in');
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
