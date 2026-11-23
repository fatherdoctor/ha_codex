// The warning on the Settings icon (and the phone's menu buttons) against mocked worker replies.
// Run with tests/web_fixture.py serving the page. Scheduled runs are on by default, so it also checks schedule
// proposals; with CODEX_FIXTURE_SCHEDULED_RUNS=0 the fixture turns them off and the proposal case is skipped.
// Every account, chat and schedule here is synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const {unpauseRemote} = require('./remote_unpaused.cjs');

const signedIn = {status: 'authenticated', pending: false, codex_login: {status_ok: true, message: 'Signed in'}};
const signedOut = {status: 'idle', pending: false, codex_login: {status_ok: false, message: 'Not logged in'}};
function github(state) {
  const connected = !['disconnected', 'connecting'].includes(state);
  return {state, kind: connected ? 'app' : null, account: connected ? {login: 'synthetic-owner'} : null,
    repositories: [], expected_repositories: [], missing: [], extra: [], permissions: null,
    permission_problem: null, expires_at: null, connected_at: null, last_check: null, last_test: null,
    revoke_url: 'https://github.com/settings/apps/authorizations',
    flow: {status: 'idle', user_code: '', verification_uri: '', expires_at: null, error: ''}};
}
function remote(enabled, state) {
  return {available: true, enabled, state, message: state === 'error' ? 'Remote control is not connected.' : '',
    configured_name: 'Home Assistant', machine_name: '', pairing_code: '', restarts: 0};
}
function chat(id, permission) {
  return {task_id: id, title: `Synthetic ${id}`, status: permission ? 'running' : 'completed',
    created_at: '2026-11-19T12:00:00Z', updated_at: '2026-11-19T12:00:00Z', summary: '', question: '',
    pinned: false, archived: false, can_continue: false, permission_required: permission};
}
function schedule(id, approved, revision = 'r1') {
  return {id, name: `Synthetic ${id}`, prompt: 'Check the synthetic sensor.', mode: 'read-only',
    spec: {kind: 'interval', seconds: 3600}, approved, enabled: approved, next_due: null, revision};
}

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const width of [390, 1100]) {
      const page = await browser.newPage({viewport: {width, height: 844}});
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      const world = {auth: signedIn, github: github('disconnected'), remote: remote(false, 'off'), chats: [], archived: [], schedules: []};
      await page.route('**/auth/status', r => r.fulfill({json: {ok: true, auth: world.auth}}));
      await page.route('**/github', r => r.fulfill({json: {ok: true, github: world.github}}));
      await page.route('**/remote-control', r => r.fulfill({json: {ok: true, remote_control: world.remote}}));
      await page.route('**/schedules', r => r.fulfill({json: {ok: true, schedules: world.schedules, runs: [], timezone: 'UTC'}}));
      await page.route(/\/tasks\?/, r => {
        const tasks = new URL(r.request().url()).searchParams.get('archived') === 'true' ? world.archived : world.chats;
        return r.fulfill({json: {ok: true, tasks, total: tasks.length, next_offset: null,
          active_task_id: tasks.find(c => c.permission_required)?.task_id || null, queue: []}});
      });

      // Read everything again, as the page's own polls do.
      const reread = () => page.evaluate(async () => {
        await loadLogin().catch(() => {});
        await loadGithub().catch(() => {});
        await loadRemoteControl().catch(() => {});
        await loadList();
      });
      // Opening Settings reads the schedules; closing it leaves the result on the icon.
      const rereadSchedules = async () => {
        await page.evaluate(() => document.getElementById('settings-button').onclick());
        await page.evaluate(() => document.getElementById('settings').close());
      };
      const look = () => page.evaluate(() => {
        const visible = (id) => { const el = document.getElementById(id); return el.offsetParent !== null; };
        const menu = ['menu', 'phone-history'].find(visible);
        return {
          level: document.body.dataset.attention || '',
          label: document.getElementById('attention-label').textContent,
          dismiss: !document.getElementById('attention-dismiss').hidden,
          cog: getComputedStyle(document.getElementById('settings-button'), '::after').backgroundColor,
          menuMark: menu ? getComputedStyle(document.getElementById(menu), '::after').content : null,
          menuColor: menu ? getComputedStyle(document.getElementById(menu), '::after').backgroundColor : null,
        };
      });
      const dismiss = async () => {
        // On a phone the dismiss row is in the side bar: open it with the menu button, close it after.
        const phone = width < 700;
        if (phone) await page.locator('#menu:visible, #phone-history:visible').first().click();
        await page.locator('#attention-dismiss').click();
        if (phone) await page.evaluate(() => sidebar(false));
      };

      await unpauseRemote(page);

      await page.goto('http://127.0.0.1:9137/preview/');
      await page.waitForFunction(() => document.getElementById('codex-status-row').dataset.state === 'ready');
      await reread();

      // Never signed in to GitHub, Remote off, nothing waits: no warning.
      let seen = await look();
      assert.deepEqual([seen.level, seen.label, seen.dismiss], ['', '', false], `${width} nothing`);
      if (width < 700) assert.equal(seen.menuMark, 'none', `${width} no dot on the menu button`);

      // GitHub signed out on purpose reads "disconnected": still nothing.
      world.github = github('ok'); await reread();
      world.github = github('disconnected'); await reread();
      assert.equal((await look()).level, '', `${width} signed out on purpose`);

      // An unintended GitHub sign-out is red and can be dismissed.
      world.github = github('revoked'); await reread();
      seen = await look();
      assert.deepEqual([seen.level, seen.label, seen.dismiss], ['red', 'GitHub signed out', true], `${width} github red`);
      if (width < 700) assert.notEqual(seen.menuMark, 'none', `${width} menu button shows the dot`);
      await dismiss();
      assert.equal((await look()).level, '', `${width} github dismissed`);
      // It stays away on the next read, and comes back after GitHub worked and failed again.
      await reread();
      assert.equal((await look()).level, '', `${width} github stays dismissed`);
      world.github = github('ok'); await reread();
      world.github = github('revoked'); await reread();
      assert.equal((await look()).level, 'red', `${width} github new sign-out`);
      // An expired sign-in is red too, like a revoked one.
      world.github = github('ok'); await reread();
      world.github = github('expired'); await reread();
      seen = await look();
      assert.deepEqual([seen.level, seen.label], ['red', 'GitHub signed out'], `${width} github expired`);
      world.github = github('ok'); await reread();

      // Remote: Off is the owner's choice; an error while it is on is red and can be dismissed.
      world.remote = remote(false, 'off'); await reread();
      assert.equal((await look()).level, '', `${width} remote off`);
      // Remote not available on this install (switch on, state error) shows nothing: the owner cannot fix it here.
      world.remote = {...remote(true, 'error'), available: false}; await reread();
      assert.equal((await look()).level, '', `${width} remote not available`);
      world.remote = remote(true, 'error'); await reread();
      seen = await look();
      assert.deepEqual([seen.level, seen.label, seen.dismiss], ['red', 'Remote disconnected', true], `${width} remote red`);
      await dismiss();
      assert.equal((await look()).level, '', `${width} remote dismissed`);
      world.remote = remote(true, 'ready'); await reread();

      // An approval waits: yellow, dismissible; a new approval in another chat brings it back.
      world.chats = [chat('a', true), chat('b', false)]; await reread();
      seen = await look();
      assert.deepEqual([seen.level, seen.label, seen.dismiss], ['yellow', 'Approval waiting', true], `${width} approval`);
      if (width < 700) assert.notEqual(seen.menuColor, (await page.evaluate(() => 'rgba(0, 0, 0, 0)')), `${width} menu colored`);
      const yellowCog = seen.cog;
      await dismiss();
      assert.equal((await look()).level, '', `${width} approval dismissed`);
      await reread();
      assert.equal((await look()).level, '', `${width} approval stays dismissed`);
      world.chats = [chat('a', true), chat('b', true)]; await reread();
      assert.equal((await look()).level, 'yellow', `${width} new approval`);
      // The same chat asking again, after its approval was answered, is new.
      await dismiss();
      world.chats = [chat('a', false), chat('b', false)]; await reread();
      assert.equal((await look()).level, '', `${width} approval answered`);
      world.chats = [chat('a', true), chat('b', false)]; await reread();
      assert.equal((await look()).level, 'yellow', `${width} same chat asks again`);
      // A read of the archived chats leaves the approvals of the recent list in place.
      await page.evaluate(() => document.getElementById('archive-list').onclick());
      await page.waitForFunction(() => state.archived === true);
      assert.equal((await look()).level, 'yellow', `${width} archived view keeps approvals`);
      await page.evaluate(() => document.getElementById('archive-list').onclick());
      await page.waitForFunction(() => state.archived === false);
      world.chats = [chat('a', false), chat('b', false)]; await reread();

      // A schedule proposal waits: yellow; an edit after Dismiss brings it back; approving it clears the warning.
      // The page has schedules only while scheduled runs are on (the default; CODEX_FIXTURE_SCHEDULED_RUNS=0 turns them off).
      if (await page.locator('#schedule-dialog').count()) {
        world.schedules = [schedule('s1', false)]; await rereadSchedules();
        seen = await look();
        assert.deepEqual([seen.level, seen.label], ['yellow', 'Schedule proposal waiting'], `${width} proposal`);
        await dismiss();
        assert.equal((await look()).level, '', `${width} proposal dismissed`);
        world.schedules = [schedule('s1', false, 'r2')]; await rereadSchedules();
        assert.equal((await look()).level, 'yellow', `${width} edited proposal is new`);
        world.schedules = [schedule('s1', true, 'r3')]; await rereadSchedules();
        assert.equal((await look()).level, '', `${width} proposal approved`);
      } else console.log(`browser_attention: ${width}: scheduled runs off, proposal case skipped`);

      // ChatGPT signed out: red, and Dismiss does not hide it.
      world.auth = signedOut; await reread();
      seen = await look();
      assert.deepEqual([seen.level, seen.label, seen.dismiss], ['red', 'ChatGPT signed out', false], `${width} chatgpt`);
      assert.notEqual(seen.cog, yellowCog, `${width} red differs from yellow`);

      // Both at once: red wins, the label names both, and Dismiss hides only the yellow.
      world.chats = [chat('c', true)]; await reread();
      seen = await look();
      assert.deepEqual([seen.level, seen.label, seen.dismiss], ['red', 'ChatGPT signed out, Approval waiting', true], `${width} both`);
      await dismiss();
      seen = await look();
      assert.deepEqual([seen.level, seen.label, seen.dismiss], ['red', 'ChatGPT signed out', false], `${width} red stays`);

      // ChatGPT status unreadable is red too; signed in again, nothing is left.
      await page.unroute('**/auth/status');
      await page.route('**/auth/status', r => r.fulfill({status: 500, json: {ok: false, error: 'synthetic failure'}}));
      await reread();
      assert.equal((await look()).label, 'ChatGPT unavailable', `${width} chatgpt unavailable`);
      await page.unroute('**/auth/status');
      world.auth = signedIn;
      await page.route('**/auth/status', r => r.fulfill({json: {ok: true, auth: world.auth}}));
      await reread();
      assert.equal((await look()).level, '', `${width} all clear`);

      // The buttons keep their names; the state is their description.
      assert.equal(await page.getByRole('button', {name: 'Open settings', exact: true}).count(), 1);
      assert.deepEqual(errors, [], `${width} page errors`);
      await page.close();
    }
    console.log('browser_attention: ok');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
