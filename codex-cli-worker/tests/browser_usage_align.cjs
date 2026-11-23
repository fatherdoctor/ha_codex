// The sidebar's 5h and 7d rows and the plan line start at the left edge of the status dots, and the Settings cog is
// centered under the dots. Measured at a phone width and a PC width. Run with tests/web_fixture.py serving the page.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

const signedIn = {status: 'authenticated', pending: false, codex_login: {status_ok: true, message: 'Signed in'}};

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const [name, size] of [['phone', {width: 390, height: 844}], ['pc', {width: 1280, height: 800}]]) {
      const page = await browser.newPage({viewport: size});
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      await page.route('**/auth/status', r => r.fulfill({json: {ok: true, auth: signedIn}}));
      await page.goto('http://127.0.0.1:9137/preview/');
      await page.waitForFunction(() => document.getElementById('codex-status-row').dataset.state === 'ready');
      const edges = await page.evaluate(() => {
        const rect = el => el.getBoundingClientRect();
        // A hidden row (Remote, while Remote Control is hidden in this version) has no box to align.
        const dots = [...document.querySelectorAll('.foot-row .status-dot')].filter(el => el.checkVisibility())
          .map(el => rect(el));
        const cog = rect(document.querySelector('#settings-button .mdi-icon'));
        const left = id => {
          const box = rect(document.getElementById(id));
          const pad = parseFloat(getComputedStyle(document.getElementById(id)).paddingLeft);
          return box.left + pad;
        };
        return {
          dotLefts: dots.map(d => d.left), dotCenter: dots[0].left + dots[0].width / 2,
          cogCenter: cog.left + cog.width / 2, usage: left('usage'), plan: left('usage-plan'),
        };
      });
      assert.equal(edges.dotLefts.length, 2, `${name}: two status dots (Remote is hidden in this version)`);
      for (const d of edges.dotLefts) assert.ok(Math.abs(d - edges.dotLefts[0]) <= 1, `${name}: dots share one left edge`);
      assert.ok(Math.abs(edges.usage - edges.dotLefts[0]) <= 1, `${name}: 5h and 7d start at the dots' left edge`);
      assert.ok(Math.abs(edges.plan - edges.dotLefts[0]) <= 1, `${name}: plan line starts at the dots' left edge`);
      assert.ok(Math.abs(edges.cogCenter - edges.dotCenter) <= 1, `${name}: the cog is centered under the dots`);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('PASS usage align: usage rows and plan line start at the status dots; the cog is centered under them');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exit(1); });
