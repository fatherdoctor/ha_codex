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
   assert.equal(await page.locator('#full-access-banner').count(),0,'no banner exists, on or off');
   await page.locator('#permission-button').click();
   assert.equal(await page.locator('[data-mode="full-access"]').isDisabled(),true,'off: the choice is disabled');
   assert.equal(await page.locator('[data-mode="full-access"] strong').evaluate(e=>getComputedStyle(e).fontWeight),'400','off: Full access is not bold');
   await page.locator('[data-close="permissions-dialog"]').click();

   await page.evaluate(()=>document.getElementById('settings-button').click());
   await page.locator('#settings').waitFor();
   // Settings sections start collapsed.
   await page.locator('#full-access-section > h3 .settings-toggle').click();
   await page.locator('#full-access-toggle').click();
   await page.locator('#full-access-dialog').waitFor();
   assert.match(await page.locator('#full-access-warning').textContent(),/outside the sandbox/);
   await page.locator('#full-access-cancel').click();
   assert.equal(await page.evaluate(()=>document.body.dataset.fullAccess),'off','cancel leaves it off');
   await page.locator('#full-access-toggle').click();
   await page.locator('#full-access-confirm').click();
   await page.waitForFunction(()=>document.body.dataset.fullAccess==='on');
   assert.match(await page.locator('#full-access-state').textContent(),/^On\./);
   await page.evaluate(()=>document.getElementById('settings').close());

   await page.locator('#permission-button').click();
   assert.equal(await page.locator('[data-mode="full-access"]').isDisabled(),false,'on: the choice is enabled');
   assert.equal(await page.locator('[data-mode="full-access"] strong').evaluate(e=>getComputedStyle(e).fontWeight),'700','on: Full access is bold');
   assert.equal(await page.locator('[data-mode="default"] strong').evaluate(e=>getComputedStyle(e).fontWeight),'400','on: the other choices are not bold');
   await page.locator('[data-mode="full-access"]').click();
   await page.waitForFunction(()=>document.getElementById('permission-button').classList.contains('full-access'));
   assert.match(await page.locator('#permission-button').getAttribute('aria-label'),/Full access/);

   await page.locator('#permission-button').click();
   await page.locator('[data-mode="default"]').click();
   await page.evaluate(()=>document.getElementById('settings-button').click());
   await page.locator('#full-access-toggle').click();
   await page.waitForFunction(()=>document.body.dataset.fullAccess==='off');
   assert.equal(await page.locator('[data-mode="full-access"]').isDisabled(),true,'off again: the choice is disabled');

   // Turned off while a chat is still set to Full access: the page shows and sends Default.
   await page.evaluate(()=>document.getElementById('settings').close());
   await page.evaluate(()=>document.querySelector('.chat-row .row-main').click());
   await page.locator('.message').first().waitFor();
   await page.evaluate(()=>document.getElementById('settings-button').click());
   await page.locator('#full-access-toggle').click();
   await page.locator('#full-access-confirm').click();
   await page.waitForFunction(()=>document.body.dataset.fullAccess==='on');
   await page.evaluate(()=>document.getElementById('settings').close());
   await page.locator('#permission-button').click();
   await page.locator('[data-mode="full-access"]').click();
   await page.waitForFunction(()=>document.getElementById('permission-button').classList.contains('full-access'));
   await page.evaluate(()=>document.getElementById('settings-button').click());
   await page.locator('#full-access-toggle').click();
   await page.waitForFunction(()=>document.body.dataset.fullAccess==='off');
   await page.evaluate(()=>document.getElementById('settings').close());
   assert.equal(await page.locator('#permission-button').getAttribute('aria-label'),'Default permissions','off: the chat shows Default');
   assert.equal(await page.locator('#permission-button.full-access').count(),0,'off: no Full access marker');
   const sent = page.waitForRequest(request=>request.method()==='POST' && /\/(tasks|continue)$/.test(new URL(request.url()).pathname));
   await page.locator('#message').fill('Next message');
   await page.locator('#send').click();
   assert.equal((await sent).postDataJSON().permission_mode,'default','off: the next message sends Default');

   // Turned on again: the chat starts at Default, and the owner must choose Full access again.
   await page.evaluate(()=>document.getElementById('settings-button').click());
   await page.locator('#full-access-toggle').click();
   await page.locator('#full-access-confirm').click();
   await page.waitForFunction(()=>document.body.dataset.fullAccess==='on');
   await page.evaluate(()=>document.getElementById('settings').close());
   assert.equal(await page.locator('#permission-button').getAttribute('aria-label'),'Default permissions','on again: the chat shows Default');
   assert.equal(await page.locator('#permission-button.full-access').count(),0,'on again: no Full access marker');
   const again = page.waitForRequest(request=>request.method()==='POST' && /\/(tasks|continue)$/.test(new URL(request.url()).pathname));
   await page.locator('#message').fill('One more message');
   await page.waitForFunction(()=>!document.getElementById('send').disabled);
   await page.locator('#send').click();
   assert.equal((await again).postDataJSON().permission_mode,'default','on again: the next message sends Default');
   await page.evaluate(()=>document.getElementById('settings-button').click());
   await page.locator('#full-access-toggle').click();
   await page.waitForFunction(()=>document.body.dataset.fullAccess==='off');
   assert.deepEqual(errors,[]);
   await context.close();
  }
  console.log('PASS: Full access warning, cancel, confirm, no banner, bold choice while on, chat choice and marker, turn off, a Full access chat falls back to Default and stays there when Full access is turned on again, desktop and mobile');
 } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
