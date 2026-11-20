// The phone's chat-list button uses the sidebar-panel icon and lines up under Home Assistant's menu button (its
// icon center 28px from the page edge); a yellow and a red warning each show one dot of the right color at the top
// right of that button and of the Settings cog. Run with tests/web_fixture.py serving the page. Every name is synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

const signedIn = {status: 'authenticated', pending: false, codex_login: {status_ok: true, message: 'Signed in'}};
const github = (state) => ({state, kind: state === 'revoked' ? null : 'app', account: null, repositories: [],
  expected_repositories: [], missing: [], extra: [], permissions: null, permission_problem: null, expires_at: null,
  connected_at: null, last_check: null, last_test: null, revoke_url: 'https://github.com/settings/apps/authorizations',
  flow: {status: 'idle', user_code: '', verification_uri: '', expires_at: null, error: ''}});
const chat = (id, permission) => ({task_id: id, title: `Synthetic ${id}`, status: permission ? 'running' : 'completed',
  created_at: '2026-11-19T12:00:00Z', updated_at: '2026-11-19T12:00:00Z', summary: '', question: '', pinned: false,
  archived: false, can_continue: false, permission_required: permission});

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 390, height: 844}});
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    const world = {github: github('ok'), chats: []};
    await page.route('**/auth/status', r => r.fulfill({json: {ok: true, auth: signedIn}}));
    await page.route('**/github', r => r.fulfill({json: {ok: true, github: world.github}}));
    await page.route('**/remote-control', r => r.fulfill({json: {ok: true, remote_control: {available: true, enabled: false,
      state: 'off', message: '', configured_name: 'Home Assistant', machine_name: '', pairing_code: '', restarts: 0}}}));
    await page.route('**/schedules', r => r.fulfill({json: {ok: true, schedules: [], runs: [], timezone: 'UTC'}}));
    await page.route(/\/tasks\?/, r => r.fulfill({json: {ok: true, tasks: world.chats, total: world.chats.length,
      next_offset: null, active_task_id: world.chats.find(c => c.permission_required)?.task_id || null, queue: []}}));
    await page.goto('http://127.0.0.1:9137/preview/');
    await page.waitForFunction(() => document.getElementById('codex-status-row').dataset.state === 'ready');
    const reread = () => page.evaluate(async () => {
      await loadGithub().catch(() => {}); await loadList();
    });
    const look = () => page.evaluate(() => {
      const button = ['menu', 'phone-history'].map(id => document.getElementById(id)).find(el => el.offsetParent !== null);
      const dot = (el) => {
        const after = getComputedStyle(el, '::after');
        return after.content === 'none' ? null : {color: after.backgroundColor, width: after.width, height: after.height};
      };
      const rect = button.getBoundingClientRect();
      const before = getComputedStyle(button, '::before');
      return {
        mask: before.webkitMaskImage || before.maskImage, glyphSize: getComputedStyle(button).fontSize,
        centerX: rect.left + rect.width / 2, button: dot(button), cog: dot(document.getElementById('settings-button')),
        iconWidth: before.width, iconHeight: before.height,
      };
    });

    // No warning: the sidebar icon, 24px, centered 28px from the edge, no dot anywhere.
    let seen = await look();
    assert.match(seen.mask, /rect/, 'chat-list button uses the sidebar-panel icon (a rounded box)');
    assert.doesNotMatch(seen.mask, /M4 7h16/, 'no three-line icon');
    assert.equal(seen.glyphSize, '0px', 'no three-line text glyph shows');
    assert.deepEqual([seen.iconWidth, seen.iconHeight], ['24px', '24px'], 'icon is 24px');
    assert.equal(seen.centerX, 28, 'icon sits 28px from the page edge, under the menu button');
    assert.deepEqual([seen.button, seen.cog], [null, null], 'no dot without a warning');

    // Yellow: an approval waits.
    world.chats = [chat('a', true)]; await reread();
    seen = await look();
    assert.ok(seen.button && seen.cog, 'yellow: a dot on the button and on the cog');
    assert.equal(seen.button.color, seen.cog.color, 'same yellow on both');
    assert.deepEqual([seen.button.width, seen.button.height], ['9px', '9px'], 'one small dot');
    const yellow = seen.button.color;

    // Red: GitHub signed out.
    world.chats = []; world.github = github('revoked'); await reread();
    seen = await look();
    assert.ok(seen.button && seen.cog, 'red: a dot on the button and on the cog');
    assert.equal(seen.button.color, seen.cog.color, 'same red on both');
    assert.notEqual(seen.button.color, yellow, 'red differs from yellow');
    assert.equal(await page.evaluate(() => document.querySelectorAll('#settings-button .mdi-icon, #menu svg').length), 1, 'the cog stays, no warning symbol swapped in');
    assert.deepEqual(errors, []);
    await page.screenshot({path: process.env.ICON_DOTS_SHOT || '/tmp/icon-dots-phone.png'});
    console.log('PASS icon dots: sidebar icon under the menu button; yellow and red dots on the button and the cog');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
