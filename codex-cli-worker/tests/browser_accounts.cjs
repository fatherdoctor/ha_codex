// Accounts and connections in Settings, and the page's answer to an expired Home Assistant ingress session.
// Run with tests/web_fixture.py serving the page. Every name, code and session here is synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const {unpauseRemote} = require('./remote_unpaused.cjs');

const base = 'http://127.0.0.1:9137/preview/';
const signedIn = {status: 'idle', pending: false, codex_login: {status_ok: true, message: 'Signed in to ChatGPT.'}};
const github = {
  state: 'ok', kind: 'app', account: {login: 'synthetic-owner'}, client_id_source: 'built_in',
  repositories: [{full_name: 'synthetic-owner/house', private: true, push: true}],
  expected_repositories: ['synthetic-owner/house'], missing: [], extra: [],
  permissions: {contents: 'write', metadata: 'read'}, permission_problem: null, expires_at: null,
  connected_at: '2026-11-16T12:00:00Z', last_check: {at: '2026-11-16T13:00:00Z', state: 'ok', reason: ''},
  last_test: null, revoke_url: 'https://github.com/settings/apps/authorizations',
  flow: {status: 'idle', user_code: '', verification_uri: '', expires_at: null, error: ''},
};
const remote = {
  enabled: true, available: true, state: 'ready', message: 'Connected', configured_name: 'Sample relay',
  machine_name: 'Sample relay', log: [],
};

async function openSettings(page) {
  const conversations = page.getByRole('button', {name: 'Open conversations', exact: true});
  if (await conversations.isVisible()) await conversations.click();
  await page.getByRole('button', {name: 'Open settings', exact: true}).click();
}
const accountsStatus = page => page.locator('#accounts-section > h3 .settings-status').textContent();

/** A same-origin page that stands in for the Home Assistant frontend: a hass object whose callWS is recorded. */
function hostPage(sessionReply, page = base) {
  return `<!doctype html><home-assistant></home-assistant><script>
    window.wsCalls = [];
    document.querySelector('home-assistant').hass = {
      callWS: async request => {
        window.wsCalls.push(request);
        ${sessionReply}
      },
    };
  <\/script><iframe src="${page}" style="width:400px;height:800px"></iframe>`;
}

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    // 1. One section holds the three parts; its header names each part's state in a few words.
    {
      const page = await browser.newPage({viewport: {width: 400, height: 860}});
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      let rc = remote;
      const posts = [];
      await page.route('**/auth/status', route => route.fulfill({json: {ok: true, auth: signedIn}}));
      await page.route('**/github', route => route.fulfill({json: {ok: true, github}}));
      await page.route('**/remote-control', route => route.fulfill({json: {ok: true, remote_control: rc}}));
      await page.route('**/remote-control/stop', route => {
        posts.push('stop');
        rc = {...remote, enabled: false, state: 'off'};
        return route.fulfill({json: {ok: true, remote_control: rc}});
      });
      await unpauseRemote(page); await page.goto(base);
      await openSettings(page);
      await page.waitForFunction(() => document.querySelector('#accounts-section > h3 .settings-status').textContent.includes('app on'));
      assert.equal(await accountsStatus(page), 'ChatGPT signed in · GitHub connected · app on');
      assert.equal(await page.locator('#accounts-section').evaluate(s => s.classList.contains('needs-attention')), false);
      // Collapsed by default; opening it shows the ChatGPT and GitHub headers and the app pairing part.
      assert.equal(await page.locator('#accounts-section > h3 .settings-toggle').getAttribute('aria-expanded'), 'false');
      await page.locator('#accounts-section > h3 .settings-toggle').click();
      assert.equal(await page.locator('#chatgpt-section .settings-status').textContent(), 'Signed in');
      assert.equal(await page.locator('#github-section .settings-status').textContent(), 'Connected as synthetic-owner, 1 repository');
      // The app pairing part has its own collapsed header, like ChatGPT and GitHub.
      assert.equal(await page.locator('#app-pairing-section .settings-status').textContent(), 'On');
      assert.equal(await page.locator('#app-pairing-new').isVisible(), false);
      await page.locator('#app-pairing-section .settings-toggle').click();
      assert.equal(await page.locator('#app-pairing-status').textContent(), 'Connected');
      assert.equal(await page.locator('#app-pairing-new').isVisible(), true);
      // Turn off is the remote-control stop: the part and the header follow the new state.
      await page.locator('#app-pairing-off').click();
      await page.waitForFunction(() => document.querySelector('#accounts-section > h3 .settings-status').textContent.endsWith('app off'));
      assert.deepEqual(posts, ['stop']);
      assert.equal(await page.locator('#app-pairing-off').isVisible(), false);
      assert.equal(await page.locator('#app-pairing-new').isVisible(), false);
      assert.match(await page.locator('#app-pairing-status').textContent(), /^Off\. When on, this Home Assistant shows/);
      // A connection error marks the section and the header names only that part.
      rc = {...remote, state: 'error', message: 'The relay stopped.'};
      await page.locator('#close-settings').click();
      await openSettings(page);
      await page.waitForFunction(() => document.querySelector('#accounts-section > h3 .settings-status').textContent === 'app connection error');
      assert.equal(await page.locator('#accounts-section').evaluate(s => s.classList.contains('needs-attention')), true);
      assert.equal(await page.locator('#app-pairing-section .settings-status').textContent(), 'Connection error');
      assert.equal(await page.locator('#app-pairing-section').evaluate(s => s.classList.contains('needs-attention')), true);
      assert.equal(await page.locator('#chatgpt-section').evaluate(s => s.classList.contains('needs-attention')), false);
      // Get a pairing code opens the remote connection window and asks for a code.
      rc = remote;
      await page.locator('#close-settings').click();
      await openSettings(page);
      // The section stays open from the first visit.
      assert.equal(await page.locator('#accounts-section > h3 .settings-toggle').getAttribute('aria-expanded'), 'true');
      await page.locator('#app-pairing-new').waitFor({state: 'visible'});
      let paired = false;
      await page.route('**/remote-control/pair', route => {
        paired = true;
        // As in the worker, the status read after pairing also carries the code.
        rc = {...remote, pairing_code: 'TEST-0000'};
        return route.fulfill({json: {ok: true, remote_control: rc}});
      });
      await page.locator('#app-pairing-new').click();
      await page.locator('#connection-dialog').waitFor({state: 'visible'});
      await page.locator('#remote-control-card .pairing-code').waitFor({state: 'visible'});
      assert.equal(paired, true);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.deepEqual(errors, []);
      await page.close();
      console.log('PASS accounts: one section, header names each part, Turn off stops remote control, attention names only the part, pairing code');
    }

    // 2. A 401 renews the ingress session through Home Assistant, once, and sends the request again.
    for (const [hostPath, expected] of [
      ['app/local_codex_cli', [{type: 'supervisor/ingress/session', slug: 'local_codex_cli'}]],
      ['ingress-host', [{type: 'supervisor/api', endpoint: '/ingress/session', method: 'post'}]],
    ]) {
      const page = await browser.newPage();
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      let probeCalls = 0;
      let armed = false;
      // Once armed, the first two requests fail as an expired session would; after the renewal they succeed.
      await page.route('**/preview/browser-permissions', route => {
        if (!armed) return route.fulfill({json: {ok: true, permissions: []}});
        probeCalls += 1;
        return probeCalls <= 2
          ? route.fulfill({status: 401, contentType: 'text/plain', body: '401: Unauthorized'})
          : route.fulfill({json: {ok: true, permissions: []}});
      });
      await page.route(`http://127.0.0.1:9137/${hostPath}`, route => route.fulfill({contentType: 'text/html', body: hostPage(
        "return request.type === 'supervisor/ingress/session' || request.type === 'supervisor/api' ? {session: 'synthetic-session'} : null;")}));
      await unpauseRemote(page); await page.goto(`http://127.0.0.1:9137/${hostPath}`);
      const frame = await (await page.waitForSelector('iframe')).contentFrame();
      await frame.waitForFunction(() => typeof api === 'function');
      armed = true;
      // Two requests fail together and share one renewal.
      const results = await frame.evaluate(() => Promise.all([api('browser-permissions'), api('browser-permissions')]).then(r => r.map(d => d.ok)));
      assert.deepEqual(results, [true, true]);
      assert.equal(probeCalls, 4);
      assert.deepEqual(await page.evaluate(() => window.wsCalls), expected);
      assert.equal(await frame.locator('#session-banner').isHidden(), true);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('PASS 401: one renewal through Home Assistant (with the app slug when the address shows it), then the request again');

    // 3. When the renewal fails, the page says so in words, with a Reload button, and never shows the HTTP code.
    // The address names the app, so the renewal tries both requests before it gives up.
    {
      const page = await browser.newPage();
      await page.route('**/preview/browser-permissions', route => route.fulfill({status: 401, contentType: 'text/plain', body: '401: Unauthorized'}));
      await page.route('http://127.0.0.1:9137/app/local_codex_cli', route => route.fulfill({contentType: 'text/html', body: hostPage(
        "throw new Error('Unauthorized');")}));
      await unpauseRemote(page); await page.goto('http://127.0.0.1:9137/app/local_codex_cli');
      const frame = await (await page.waitForSelector('iframe')).contentFrame();
      await frame.waitForFunction(() => typeof api === 'function');
      const message = await frame.evaluate(() => api('browser-permissions').then(() => 'no error', e => e.message));
      assert.equal(message, 'Your Home Assistant session expired. Reload the page to continue.');
      assert.doesNotMatch(message, /401|HTTP/);
      assert.equal(await frame.locator('#session-banner').isVisible(), true);
      assert.doesNotMatch(await frame.locator('#session-banner').textContent(), /401|HTTP/);
      assert.equal(await frame.getByRole('button', {name: 'Reload', exact: true}).isVisible(), true);
      assert.equal((await page.evaluate(() => window.wsCalls)).length, 2, 'both renewal requests are tried');
      await page.close();
    }
    // 4. Outside Home Assistant there is no parent to ask: no renewal, the same plain message.
    {
      const page = await browser.newPage();
      await page.route('**/preview/browser-permissions', route => route.fulfill({status: 401, contentType: 'text/plain', body: '401: Unauthorized'}));
      await unpauseRemote(page); await page.goto(base);
      await page.waitForFunction(() => typeof api === 'function');
      const message = await page.evaluate(() => api('browser-permissions').then(() => 'no error', e => e.message));
      assert.equal(message, 'Your Home Assistant session expired. Reload the page to continue.');
      assert.equal(await page.locator('#session-banner').isVisible(), true);
      // A later request that gets through (the owner signed in again in another tab) hides the banner.
      await page.unroute('**/preview/browser-permissions');
      await page.route('**/preview/browser-permissions', route => route.fulfill({json: {ok: true, permissions: []}}));
      await page.evaluate(() => api('browser-permissions'));
      assert.equal(await page.locator('#session-banner').isHidden(), true);
      await page.close();
    }
    // 5. A parent on another origin cannot be asked: the page never reaches its callWS and shows the plain banner.
    // Chromium refuses to frame a local address from a page a test route serves on another origin; this browser
    // turns that check off, and only this case uses it.
    const crossOrigin = await chromium.launch({headless: true, args: ['--disable-features=LocalNetworkAccessChecks']});
    try {
      const page = await crossOrigin.newPage();
      await page.route('**/preview/browser-permissions', route => route.fulfill({status: 401, contentType: 'text/plain', body: '401: Unauthorized'}));
      // The same server under its other local name: the parent and the page are on different origins.
      await page.route('http://127.0.0.1:9137/ingress-host', route => route.fulfill({contentType: 'text/html', body: hostPage(
        "return {session: 'synthetic-session'};", 'http://localhost:9137/preview/')}));
      await unpauseRemote(page); await page.goto('http://127.0.0.1:9137/ingress-host');
      const frame = await (await page.waitForSelector('iframe')).contentFrame();
      await frame.waitForFunction(() => typeof api === 'function');
      const message = await frame.evaluate(() => api('browser-permissions').then(() => 'no error', e => e.message));
      assert.equal(message, 'Your Home Assistant session expired. Reload the page to continue.');
      assert.equal(await frame.locator('#session-banner').isVisible(), true);
      assert.deepEqual(await page.evaluate(() => window.wsCalls), []);
    } finally {
      await crossOrigin.close();
    }
    console.log('PASS 401: a failed renewal shows a plain message and a Reload button, never the HTTP code; a parent on another origin is not asked');
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exit(1);
});
