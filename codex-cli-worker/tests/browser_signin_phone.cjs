// Sign-in on a phone: the ChatGPT banner above the chat and the ChatGPT and GitHub sign-in cards in Settings scroll
// at phone width, so a person can reach the sign-in link and read the whole code; the QR code starts folded on a
// phone and open on a desktop. browser_settings_phone.cjs runs this file; it also runs alone. Run with
// tests/web_fixture.py serving the page. Every code, name and address is synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

const PAGE = 'http://127.0.0.1:9137/preview/';
const PHONES = [{width: 360, height: 560}, {width: 390, height: 640}];
const DESKTOP = {width: 1280, height: 800};
// A plain white square stands in for the worker's QR code.
const QR = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10" fill="#fff"/></svg>';
const waiting = {
  status: 'waiting_for_user', pending: true, process_running: true, login_id: 'L1', user_code: 'TEST-0000',
  verification_url: 'https://auth.openai.com/codex/device', qr_path: 'auth/qr.svg?login=L1', expires_in: 900,
  expires_at: '2026-11-16T13:15:00Z', codex_login: {status_ok: false, has_auth_file: false, message: ''},
};
const github = {
  state: 'connecting', kind: null, account: null, client_id_source: 'built_in', repositories: [],
  expected_repositories: [], missing: [], extra: [], permissions: null, permission_problem: null, expires_at: null,
  connected_at: null, last_check: null, last_test: null, revoke_url: 'https://github.com/settings/apps/authorizations',
  flow: {status: 'waiting_for_user', user_code: 'TEST-0001', verification_uri: 'https://github.com/login/device',
    expires_at: '2026-11-16T13:15:00Z', expires_in: 900, error: ''},
};

/** A page with a ChatGPT sign-in and a GitHub sign-in that both wait for their codes. */
async function openPage(browser, viewport) {
  const page = await browser.newPage({viewport});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/auth/status', route => route.fulfill({json: {ok: true, auth: waiting}}));
  await page.route('**/auth/qr.svg*', route => route.fulfill({contentType: 'image/svg+xml', body: QR}));
  await page.route('**/github', route => route.fulfill({json: {ok: true, github}}));
  await page.goto(PAGE);
  await page.locator('#signin-banner .signin-code').waitFor();
  return {page, errors};
}

/**
 * Turn the mouse wheel over the scroll area, as a person scrolls, until the element is wholly inside the window
 * and the scroll area. A script scroll would also move a box that clips its content, which no person can scroll;
 * the wheel does not.
 */
async function wheelTo(page, area, target, label) {
  const box = await page.locator(area).boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + Math.min(box.height / 2, 40));
  for (let turn = 0; turn < 20; turn += 1) {
    if (await inView(page, area, target)) return;
    await page.mouse.wheel(0, 120);
    await page.waitForTimeout(50);
  }
  assert.fail(`${label}: ${target} cannot be scrolled into the window`);
}

/** True when the element lies wholly inside the window and the area, and a tap at its center lands on it. */
function inView(page, area, target) {
  return page.evaluate(([areaSelector, targetSelector]) => {
    const element = document.querySelector(targetSelector);
    const box = element.getBoundingClientRect();
    const frame = document.querySelector(areaSelector).getBoundingClientRect();
    const inside = box.width > 0 && box.height > 0 && box.left >= 0 && box.right <= innerWidth + 0.5 &&
      box.top >= Math.max(0, frame.top) - 0.5 && box.bottom <= Math.min(innerHeight, frame.bottom) + 0.5;
    const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    return inside && Boolean(hit) && (hit === element || element.contains(hit));
  }, [area, target]);
}

/** The code shows in full: no letter is cut at the card's side. */
async function codeWhole(page, selector, label) {
  const whole = await page.locator(selector).evaluate(code =>
    code.scrollWidth <= code.clientWidth + 0.5 && code.getBoundingClientRect().right <= innerWidth + 0.5);
  assert.ok(whole, `${label}: the code ${selector} is cut at the side`);
}

/** The QR code starts folded (phone) or open (desktop), and "Show QR code" opens it. */
async function checkQr(page, card, phone, label) {
  const fold = page.locator(`${card} .signin-qr-fold`);
  assert.equal(await fold.evaluate(details => details.open), !phone, `${label}: QR code open on start`);
  assert.equal(await page.locator(`${card} .signin-qr`).isVisible(), !phone, `${label}: QR code shown on start`);
  if (!phone) return;
  await fold.locator('summary').click();
  assert.equal(await page.locator(`${card} .signin-qr`).isVisible(), true, `${label}: Show QR code opens it`);
}

/** Run every check in an open browser. */
async function run(browser) {
  for (const viewport of PHONES) {
    const label = `${viewport.width}x${viewport.height}`;
    // The banner above the chat: the code, then the link and the Cancel button by a wheel turn.
    const {page, errors} = await openPage(browser, viewport);
    await codeWhole(page, '#signin-banner .signin-code', `${label} banner`);
    await wheelTo(page, '#signin-banner', '#signin-banner .signin-code', `${label} banner`);
    await wheelTo(page, '#signin-banner', '#signin-banner a[href^="https://auth.openai.com/"]', `${label} banner`);
    await wheelTo(page, '#signin-banner', '#banner-cancel', `${label} banner`);
    await checkQr(page, '#signin-banner', true, `${label} banner`);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true,
      `${label}: the page scrolls sideways`);

    // Settings: the ChatGPT and the GitHub sign-in cards, opened as a person opens them.
    const conversations = page.getByRole('button', {name: 'Open conversations', exact: true});
    if (await conversations.isVisible()) await conversations.click();
    await page.getByRole('button', {name: 'Open settings', exact: true}).click();
    for (const id of ['accounts-section', 'chatgpt-section', 'github-section'])
      await page.locator(`#${id} .settings-toggle`).first().click();
    await page.locator('#github-flow .signin-code').waitFor();
    await checkQr(page, '#login-card', true, `${label} settings`);
    await page.locator('#login-card .signin-qr-fold').evaluate(details => { details.open = false; });
    for (const target of ['#login-card .signin-code', '#login-card a[href^="https://auth.openai.com/"]',
      '#github-flow .signin-code', '#github-flow a[href^="https://github.com/login/device"]']) {
      await wheelTo(page, '#settings', target, `${label} settings`);
      if (target.endsWith('.signin-code')) await codeWhole(page, target, `${label} settings`);
    }
    assert.equal(await page.locator('#settings').evaluate(d => d.scrollWidth <= d.clientWidth), true,
      `${label}: the settings panel scrolls sideways`);
    assert.deepEqual(errors, []);
    await page.close();
  }

  // A desktop window shows the QR code open, as before.
  const {page, errors} = await openPage(browser, DESKTOP);
  await checkQr(page, '#signin-banner', false, 'desktop banner');
  assert.ok(await inView(page, '#signin-banner', '#signin-banner a[href^="https://auth.openai.com/"]'),
    'desktop: the sign-in link is off the screen');
  assert.deepEqual(errors, []);
  await page.close();
  console.log('PASS: sign-in at 360 and 390 px: the banner and the Settings cards scroll to the code and the link, '
    + 'the QR code starts folded; at 1280 px the QR code starts open');
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
