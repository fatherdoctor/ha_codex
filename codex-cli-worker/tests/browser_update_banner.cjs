// Update banners against web_fixture.py, phone and desktop: each shows its version, Dismiss hides it for that
// version only, and the update banner links to the app's Info page in the top window.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const base = 'http://127.0.0.1:9137/preview/';

async function open(context, status) {
 const page = await context.newPage();
 const errors = []; page.on('pageerror', e => errors.push(e.message));
 await page.route('**/update-status', route => route.fulfill({json: {ok: true, ...status}}));
 await page.goto(base);
 await page.locator('.chat-row').first().waitFor();
 // The banner script asks once at load; wait until it has answered.
 await page.waitForFunction(() => document.getElementById('update-banner-open').hasAttribute('href')
   || document.getElementById('update-banner').hidden === document.getElementById('restart-banner').hidden);
 return {page, errors};
}

(async () => {
 const browser = await chromium.launch({headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage']});
 try {
  for (const width of [1280, 390]) {
   const context = await browser.newContext({viewport: {width, height: 900}});
   const both = {restart_version: '0.5.0', update_version: '0.5.1', info_url: '/config/app/local_codex_cli_worker/info'};
   let {page, errors} = await open(context, both);
   await page.locator('#restart-banner').waitFor();
   assert.equal(await page.locator('#restart-banner-text').textContent(),
     'Version 0.5.0 is installed. Restart Home Assistant to finish.');
   assert.equal(await page.locator('#update-banner-text').textContent(), 'Update available: version 0.5.1');
   const link = page.locator('#update-banner-open');
   assert.equal(await link.isVisible(), true, `${width}: the Info page link shows`);
   assert.equal(await link.getAttribute('href'), '/config/app/local_codex_cli_worker/info');
   assert.equal(await link.getAttribute('target'), '_top');
   for (const id of ['#restart-banner', '#update-banner']) {
    const box = await page.locator(id).boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= width, `${width}: ${id} stays inside the window`);
   }
   await page.locator('#restart-banner-dismiss').click();
   assert.equal(await page.locator('#restart-banner').isHidden(), true, 'Dismiss hides the restart banner');
   assert.equal(await page.locator('#update-banner').isVisible(), true, 'the other banner stays');
   await page.locator('#update-banner-dismiss').click();
   assert.deepEqual(errors, []);
   await page.close();

   // The same versions stay dismissed on the next visit.
   ({page, errors} = await open(context, both));
   assert.equal(await page.locator('#restart-banner').isHidden(), true, 'the dismissed restart banner stays hidden');
   assert.equal(await page.locator('#update-banner').isHidden(), true, 'the dismissed update banner stays hidden');
   await page.close();

   // A newer version shows each banner again.
   ({page, errors} = await open(context, {restart_version: '0.5.1', update_version: '0.5.2', info_url: null}));
   await page.locator('#restart-banner').waitFor();
   assert.match(await page.locator('#restart-banner-text').textContent(), /^Version 0\.5\.1 is installed/);
   await page.locator('#update-banner').waitFor();
   assert.equal(await page.locator('#update-banner-open').isHidden(), true, 'no Info page link without a path');
   assert.deepEqual(errors, []);
   await page.close();

   // Nothing to say: no banner.
   ({page, errors} = await open(context, {restart_version: null, update_version: null, info_url: null}));
   assert.equal(await page.locator('#restart-banner').isHidden(), true);
   assert.equal(await page.locator('#update-banner').isHidden(), true);
   assert.deepEqual(errors, []);
   await context.close();
  }
  console.log('browser_update_banner PASS');
 } finally {
  await browser.close();
 }
})().catch(error => { console.error(error); process.exit(1); });
