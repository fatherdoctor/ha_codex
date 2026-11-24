// The GitHub card against a mocked worker (the b1 routes); run with tests/web_fixture.py serving the page.
// Every code, token, account and repository here is synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

const CODE = 'TEST-0000';
const TOKEN = 'github_pat_' + 'S'.repeat(70);
const REVOKE_APP = 'https://github.com/settings/apps/authorizations';
const REVOKE_TOKEN = 'https://github.com/settings/tokens';
const CONFIGURE = 'https://github.com/settings/installations/7';
const idle = {status: 'idle', user_code: '', verification_uri: '', expires_at: null, error: ''};

function status(state, extra = {}) {
  const connected = !['disconnected', 'connecting'].includes(state);
  return {
    state, kind: connected ? 'app' : null, account: connected ? {login: 'synthetic-owner'} : null,
    client_id_source: 'built_in',
    repositories: connected ? [{full_name: 'synthetic-owner/house', private: true, push: true}] : [],
    expected_repositories: connected ? ['synthetic-owner/house'] : [], missing: [], extra: [],
    permissions: connected ? {contents: 'write', metadata: 'read'} : null, permission_problem: null,
    expires_at: null, connected_at: connected ? '2026-11-16T12:00:00Z' : null,
    last_check: connected ? {at: '2026-11-16T13:00:00Z', state, reason: ''} : null, last_test: null,
    revoke_url: REVOKE_APP, add_repositories_url: connected ? CONFIGURE : '', flow: idle, ...extra,
  };
}

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [390, 1100]) {
      const page = await browser.newPage({viewport: {width, height: 844}});
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      const consoleText = []; page.on('console', m => consoleText.push(m.text()));
      page.on('dialog', d => d.accept());
      let github = status('disconnected');
      const posts = [];
      // Mocked b1 routes: GET /github and the POSTs; replies are {ok, github} or {ok:false, error, github}.
      const reply = (route, code, body) => route.fulfill({status: code, json: body});
      await page.route('**/github', route => reply(route, 200, {ok: true, github}));
      const handlers = {
        connect: () => { github = status('connecting', {flow: {status: 'waiting_for_user', user_code: CODE,
          verification_uri: 'https://github.com/login/device', expires_at: '2026-11-16T13:15:00Z', expires_in: 900, error: ''}}); return [200, {ok: true, github}]; },
        cancel: () => { github = status('disconnected', {flow: {...idle, status: 'canceled'}}); return [200, {ok: true, github}]; },
        test: () => { github = {...github, last_test: {at: '2026-11-16T13:01:00Z', ok: true, text: 'Read OK: 1 repository.'}}; return [200, {ok: true, github}]; },
        check: () => { github = status('access_changed', {extra: ['synthetic-owner/new'], missing: ['synthetic-owner/old'],
          repositories: [{full_name: 'synthetic-owner/house', private: true, push: true}, {full_name: 'synthetic-owner/new', private: false, push: true}]});
          return [200, {ok: true, github}]; },
        accept: () => { github = status('ok'); return [200, {ok: true, github}]; },
        token: (body) => body.token === TOKEN && body.repository === 'synthetic-owner/house'
          ? (github = status('ok', {kind: 'token', revoke_url: REVOKE_TOKEN}), [200, {ok: true, github}])
          : [400, {ok: false, error: 'Paste a fine-grained personal access token (it starts with github_pat_).', github}],
        disconnect: () => posts.filter(p => p.name === 'disconnect').length === 1
          ? [409, {ok: false, error: 'Wait for the running task to end, then disconnect.', github}]
          : (github = status('disconnected', {revoke_url: REVOKE_APP}), [200, {ok: true, github}]),
        'repos/clone': (body) => [200, {ok: true, text: `Cloned ${body.repository}.`, path: `/share/${body.folder}`, github}],
        'repos/link': (body) => [200, {ok: true, text: `${body.path} now uses ${body.repository} as origin.`, path: body.path, github}],
      };
      await page.route(/\/github\/(connect|cancel|token|test|check|accept|disconnect|repos\/clone|repos\/link)$/, route => {
        const name = new URL(route.request().url()).pathname.replace(/^.*\/github\//, '');
        assert.equal(route.request().method(), 'POST');
        const body = route.request().postDataJSON() || {};
        posts.push({name, body});
        const [code, data] = handlers[name](body);
        return reply(route, code, data);
      });

      await page.goto('http://127.0.0.1:9137/preview/');
      const conversations = page.getByRole('button', {name: 'Open conversations', exact: true});
      if (await conversations.isVisible()) await conversations.click();
      await page.getByRole('button', {name: 'Open settings', exact: true}).click();
      // The GitHub card follows the ChatGPT account card inside Accounts and connections; all start collapsed.
      assert.equal(await page.locator('#settings > section h3 .settings-title').first().textContent(), 'Accounts and connections');
      await page.locator('#accounts-section > h3 .settings-toggle').click();
      const card = page.locator('#github-section');
      await card.waitFor({state: 'visible'});
      assert.deepEqual(await page.evaluate(() => [...document.querySelectorAll('#accounts-section-body > section')]
        .map(section => (section.querySelector('h4 .settings-title') || section.querySelector('h4')).textContent)),
        ['ChatGPT account', 'GitHub', 'ChatGPT app pairing']);
      const toggle = card.locator('.settings-toggle');
      assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
      assert.equal(await page.locator('#github-status').isHidden(), true);
      await page.waitForFunction(() => document.querySelector('#github-section .settings-status').textContent === 'Not connected');
      await toggle.click();
      assert.equal(await toggle.getAttribute('aria-expanded'), 'true');
      await page.waitForFunction(() => document.querySelector('#github-status').textContent === 'Not connected to GitHub.');
      for (const id of ['github-test', 'github-check', 'github-accept', 'github-disconnect'])
        assert.equal(await page.locator('#' + id).isHidden(), true, id);

      // Connect shows the code and GitHub's device page; Cancel clears it.
      await page.locator('#github-connect').click();
      await card.locator('.signin-code').waitFor();
      assert.equal(await card.locator('.signin-code').textContent(), CODE);
      assert.equal(await card.locator('a[href="https://github.com/login/device"]').count(), 1);
      assert.equal(await page.locator('#github-cancel').isVisible(), true);
      assert.match(await card.locator('.signin-expiry').textContent(), /^The code expires in \d+:\d\d\.$/);
      assert.equal(await card.locator('.settings-status').textContent(), 'Connecting: enter the code');
      await page.locator('#github-cancel').click();
      await card.locator('.signin-code').waitFor({state: 'detached'});
      await page.waitForFunction(() => document.querySelector('#github-flow').textContent.includes('canceled'));

      // A code that runs out says so and offers a new one: Get a new code cancels and connects again.
      const connect = handlers.connect;
      handlers.connect = () => { github = status('connecting', {flow: {status: 'waiting_for_user', user_code: CODE,
        verification_uri: 'https://github.com/login/device', expires_at: '2026-11-16T13:15:00Z', expires_in: 1, error: ''}});
        return [200, {ok: true, github}]; };
      await page.locator('#github-connect').click();
      await card.getByRole('button', {name: 'Get a new code', exact: true}).waitFor({state: 'visible', timeout: 5000});
      assert.equal(await card.locator('.signin-expiry').textContent(), 'This code has expired. Get a new code to sign in.');
      handlers.connect = connect;
      const before = posts.length;
      await card.getByRole('button', {name: 'Get a new code', exact: true}).click();
      await page.waitForFunction(() => /expires in/.test(document.querySelector('#github-flow .signin-expiry')?.textContent || ''));
      assert.deepEqual(posts.slice(before).map(post => post.name), ['cancel', 'connect']);
      await page.locator('#github-cancel').click();
      await card.locator('.signin-code').waitFor({state: 'detached'});

      // Connect again; the worker finishes the flow and the 5-second poll shows the connection.
      await page.locator('#github-connect').click();
      await card.locator('.signin-code').waitFor();
      github = status('ok', {email: 'SYNTHETIC_PRIVATE_EMAIL', token: 'ghu_' + 'X'.repeat(36),
        flow: {...idle, status: 'completed'}});
      await page.waitForFunction(() => document.querySelector('#github-status').textContent.includes('synthetic-owner'),
        null, {timeout: 12000});
      await card.locator('.signin-code').waitFor({state: 'detached'});
      assert.equal(await card.locator('.settings-status').textContent(), 'Connected as synthetic-owner, 1 repository');
      const shown = await card.textContent();
      assert.match(shown, /synthetic-owner\/house/);
      assert.match(shown, /Does not expire; revoke it on GitHub/);
      assert.match(shown, /Contents read and write, Metadata read/);
      assert.doesNotMatch(shown, /SYNTHETIC_PRIVATE_EMAIL|ghu_X/);
      // '1 repository' is the App installation's set: one line says so and links to GitHub's page that adds more.
      assert.equal(await page.locator('#github-add-repositories').isVisible(), true);
      assert.equal(await page.locator('#github-add-repositories-link').getAttribute('href'), CONFIGURE);
      assert.match(shown, /Codex reaches only the repositories that the GitHub App is installed on\. Add repositories on GitHub/);

      // Read test and Check now; a new repository set shows the banner and Accept.
      await page.locator('#github-test').click();
      await page.waitForFunction(() => document.querySelector('#github-test-result').textContent === 'Read OK: 1 repository.');
      await page.locator('#github-check').click();
      await page.locator('#github-accept').waitFor({state: 'visible'});
      assert.match(await page.locator('#github-repositories').textContent(), /synthetic-owner\/new .*new, not accepted/);
      assert.match(await page.locator('#github-repositories').textContent(), /synthetic-owner\/old · no longer reached/);
      assert.equal(await page.locator('#github-banner').isHidden(), false);
      await page.locator('#github-accept').click();
      await page.locator('#github-accept').waitFor({state: 'hidden'});
      assert.equal(await page.locator('#github-banner').isHidden(), true);
      // A permission problem cannot be accepted from the page.
      handlers.check = () => [200, {ok: true, github}];
      github = status('access_changed', {permission_problem: 'more than Contents read and write'});
      await page.locator('#github-check').click();
      await page.waitForFunction(() => document.querySelector('#github-details').textContent.includes('more than Contents'));
      assert.equal(await page.locator('#github-accept').isHidden(), true);
      github = status('ok');
      await page.locator('#github-check').click();
      await page.waitForFunction(() => document.querySelector('#github-result').textContent.includes('Check finished'));

      // A failed Connect again shows its reason while the old sign-in stays (Astro F2).
      for (const state of ['expired', 'revoked', 'near_expiry']) {
        github = status(state, {flow: {...idle, status: 'failed', error: 'The sign-in was refused on GitHub.'}});
        await page.locator('#github-check').click();
        await page.waitForFunction(() => document.querySelector('#github-flow').textContent.includes('refused on GitHub'));
        assert.equal(await page.locator('#github-flow').textContent(),
          'The new GitHub sign-in failed. The sign-in was refused on GitHub. Your previous sign-in has not changed.');
      }
      // The recovery step fits the kind of sign-in: Connect again starts an App sign-in (Astro F4).
      github = status('expired', {kind: 'token'});
      await page.locator('#github-check').click();
      await page.waitForFunction(() => document.querySelector('#github-status').textContent.includes('token expired'));
      assert.match(await page.locator('#github-status').textContent(), /Create a new token on GitHub, then choose Use a token instead/);
      assert.match(await page.locator('#github-banner-text').textContent(), /Use a token instead/);
      assert.equal(await page.locator('#github-add-repositories').isHidden(), true, 'a token has no Add repositories link');
      assert.equal(await page.locator('#github-flow').textContent(), '');
      github = status('revoked');
      await page.locator('#github-check').click();
      await page.waitForFunction(() => document.querySelector('#github-status').textContent.includes('no longer accepts this sign-in'));
      assert.match(await page.locator('#github-status').textContent(), /Choose Connect again/);
      github = status('near_expiry');
      await page.locator('#github-check').click();
      await page.waitForFunction(() => document.querySelector('#github-status').textContent.startsWith('GitHub still works'));
      assert.equal(await page.locator('#github-banner').isHidden(), true);
      github = status('ok');
      await page.locator('#github-check').click();
      await page.waitForFunction(() => document.querySelector('#github-status').textContent.startsWith('Connected to GitHub'));

      // Clone into /share/<repository> (the default folder name) and link existing folders, by buttons.
      await page.locator('#github-local summary').click();
      assert.equal(await page.locator('#github-clone-folder').inputValue(), 'house');
      await page.locator('#github-clone').click();
      await page.waitForFunction(() => document.querySelector('#github-result').textContent === 'Cloned into /share/house.');
      assert.deepEqual(posts.at(-1), {name: 'repos/clone', body: {repository: 'synthetic-owner/house', folder: 'house'}});
      for (const path of ['/share/existing', '/homeassistant/existing']) {
        await page.locator('#github-link-path').fill(path);
        await page.locator('#github-link').click();
        await page.waitForFunction(text => document.querySelector('#github-result').textContent === text,
          `${path} now uses synthetic-owner/house as origin.`);
        assert.deepEqual(posts.at(-1), {name: 'repos/link', body: {repository: 'synthetic-owner/house', path}});
      }

      // Disconnect: 409 while a task runs, then the honest revoke link.
      await page.locator('#github-disconnect').click();
      await page.waitForFunction(() => document.querySelector('#github-result').textContent.includes('running task'));
      await page.locator('#github-disconnect').click();
      await page.locator(`#github-result a[href="${REVOKE_APP}"]`).waitFor();
      assert.match(await page.locator('#github-result').textContent(), /forgets the sign-in on this box/);
      await page.waitForFunction(() => document.querySelector('#github-status').textContent === 'Not connected to GitHub.');

      // The token fallback: a refusal shows the reason; the field never keeps the token.
      await page.locator('#github-token summary').click();
      await page.locator('#github-token-repository').fill('synthetic-owner/house');
      await page.locator('#github-token-value').fill('not-a-token');
      await page.locator('#github-token-save').click();
      await page.waitForFunction(() => document.querySelector('#github-result').textContent.includes('github_pat_'));
      assert.equal(await page.locator('#github-token-value').inputValue(), '');
      await page.locator('#github-token-value').fill(TOKEN);
      await page.locator('#github-token-save').click();
      await page.waitForFunction(() => document.querySelector('#github-result').textContent === 'Token accepted.');
      assert.equal(await page.locator('#github-token-value').inputValue(), '');
      assert.equal(posts.filter(p => p.name === 'token' && p.body.token === TOKEN).length, 1);
      await page.locator('#github-disconnect').click();
      await page.locator(`#github-result a[href="${REVOKE_TOKEN}"]`).waitFor();

      // Nothing secret is kept by the page or written to its console.
      const html = await page.content();
      assert.ok(!html.includes(TOKEN));
      const stored = await page.evaluate(() => JSON.stringify({...localStorage}) + JSON.stringify({...sessionStorage}));
      for (const secret of [CODE, TOKEN]) {
        assert.ok(!stored.includes(secret), 'browser storage');
        assert.ok(!consoleText.some(text => text.includes(secret)), 'console');
      }
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.deepEqual(errors, []);
      await page.close();
    }

    // A worker before 0.3 has no /github route: the card says so and shows no buttons.
    const page = await browser.newPage({viewport: {width: 1100, height: 844}});
    await page.route('**/github', route => route.fulfill({status: 404, body: 'Not Found'}));
    await page.goto('http://127.0.0.1:9137/preview/');
    await page.getByRole('button', {name: 'Open settings', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('#github-status').textContent.includes('Not available in this version'));
    assert.equal(await page.locator('#github-connect').isHidden(), true);
    await page.close();
    console.log('PASS: GitHub card: device code on the page, cancel, poll, read test, check, accept, clone, link, ' +
      'disconnect with the revoke link, token fallback, no secret kept, older worker.');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
