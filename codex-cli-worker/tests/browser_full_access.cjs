// Full access against web_fixture.py: the warning must be confirmed, then the banner, the choice and the marker show.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const base = 'http://127.0.0.1:9137/preview/';
(async()=>{
 const browser = await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 try {
  for (const width of [1280,390]) {
   const context = await browser.newContext({viewport:{width,height:900}});
   const page = await context.newPage();
   const errors=[]; page.on('pageerror',e=>errors.push(e.message));
   await page.goto(base);
   await page.locator('.chat-row').first().waitFor();
   assert.equal(await page.locator('#full-access-banner').isHidden(),true,'off: no banner');
   await page.locator('#permission-button').click();
   assert.equal(await page.locator('[data-mode="full-access"]').isDisabled(),true,'off: the choice is disabled');
   await page.locator('[data-close="permissions-dialog"]').click();

   await page.evaluate(()=>document.getElementById('settings-button').click());
   await page.locator('#settings').waitFor();
   // Settings sections start collapsed.
   await page.locator('#full-access-section > h3 .settings-toggle').click();
   await page.locator('#full-access-toggle').click();
   await page.locator('#full-access-dialog').waitFor();
   assert.match(await page.locator('#full-access-warning').textContent(),/outside the sandbox/);
   await page.locator('#full-access-cancel').click();
   assert.equal(await page.locator('#full-access-banner').isHidden(),true,'cancel leaves it off');
   await page.locator('#full-access-toggle').click();
   await page.locator('#full-access-confirm').click();
   await page.locator('#full-access-banner').waitFor({state:'visible'});
   assert.match(await page.locator('#full-access-state').textContent(),/^On\./);
   await page.evaluate(()=>document.getElementById('settings').close());

   await page.locator('#permission-button').click();
   assert.equal(await page.locator('[data-mode="full-access"]').isDisabled(),false,'on: the choice is enabled');
   await page.locator('[data-mode="full-access"]').click();
   await page.waitForFunction(()=>document.getElementById('permission-button').classList.contains('full-access'));
   assert.match(await page.locator('#permission-button').getAttribute('aria-label'),/Full access/);

   await page.locator('#permission-button').click();
   await page.locator('[data-mode="default"]').click();
   await page.evaluate(()=>document.getElementById('settings-button').click());
   await page.locator('#full-access-toggle').click();
   await page.locator('#full-access-banner').waitFor({state:'hidden'});
   assert.equal(await page.locator('[data-mode="full-access"]').isDisabled(),true,'off again: the choice is disabled');
   assert.deepEqual(errors,[]);
   await context.close();
  }
  console.log('PASS: Full access warning, cancel, confirm, banner, chat choice and marker, turn off, desktop and mobile');
 } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
