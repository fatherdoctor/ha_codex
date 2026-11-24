// The Settings icon lights as soon as a chat exchange ends with a schedule proposal waiting, without opening Settings.
// Runs against the disposable web fixture (python codex-cli-worker/tests/web_fixture.py), on phone and desktop widths.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    for (const width of [390, 1280]) {
      const page = await browser.newPage({viewport:{width, height:844}});
      const errors = []; page.on('pageerror', (error) => errors.push(error.message));
      let proposed = false;
      await page.route('**/schedules**', (route) => {
        if (route.request().method() !== 'GET' || /assets\//.test(route.request().url())) return route.continue();
        const schedules = proposed ? [{id:'s1', approved:false, enabled:false, revision:'r1', spec:{kind:'interval', seconds:3600}, author:'codex'}] : [];
        return route.fulfill({json:{ok:true, timezone:'America/Chicago', schedules, runs:[]}});
      });
      await page.goto('http://127.0.0.1:9137/preview/');
      await page.waitForFunction(() => typeof state !== 'undefined' && document.querySelector('.chat-row'));
      if (!(await page.locator('#schedule-dialog').count())) { console.log(`browser_sched_badge: ${width}: scheduled runs off, skipped`); await page.close(); continue; }
      const level = () => page.evaluate(() => document.body.dataset.attention || '');
      // A chat is running, the agent proposes a schedule, and the exchange ends: the next read shows the proposal.
      await page.evaluate(() => { state.id = 'sample'; state.task = {task_id:'sample', status:'running'}; });
      proposed = true;
      await page.evaluate(() => { const real = window.api; window.api = async (path, ...rest) => path.startsWith('tasks/') ? {task:{task_id:'sample', status:'completed', turns:[]}} : real(path, ...rest); });
      assert.equal(await level(), '', `width ${width}: no warning before the exchange ends`);
      await page.evaluate(() => fetchSelected());
      await page.waitForFunction(() => document.body.dataset.attention === 'yellow');
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('browser_sched_badge: PASS (390, 1280)');
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exit(1); });
