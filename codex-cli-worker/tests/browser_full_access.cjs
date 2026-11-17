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

   // Turned off while a chat is still set to Full access: the page shows and sends Default.
   await page.evaluate(()=>document.getElementById('settings').close());
   await page.evaluate(()=>document.querySelector('.chat-row .row-main').click());
   await page.locator('.message').first().waitFor();
   await page.evaluate(()=>document.getElementById('settings-button').click());
   await page.locator('#full-access-toggle').click();
   await page.locator('#full-access-confirm').click();
   await page.locator('#full-access-banner').waitFor({state:'visible'});
   await page.evaluate(()=>document.getElementById('settings').close());
   await page.locator('#permission-button').click();
   await page.locator('[data-mode="full-access"]').click();
   await page.waitForFunction(()=>document.getElementById('permission-button').classList.contains('full-access'));
   await page.evaluate(()=>document.getElementById('settings-button').click());
   await page.locator('#full-access-toggle').click();
   await page.locator('#full-access-banner').waitFor({state:'hidden'});
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
   await page.locator('#full-access-banner').waitFor({state:'visible'});
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
   await page.locator('#full-access-banner').waitFor({state:'hidden'});
   assert.deepEqual(errors,[]);
   await context.close();
  }
  // CX-FULLACCESS.pr3 F1: a tab asleep while another tab turns Full access off and on keeps its old choice in
  // memory. Its next message must run with Default; the worker decides, whatever the tab sends.
  {
   const context = await browser.newContext({viewport:{width:1280,height:900}});
   const tabA = await context.newPage(), tabB = await context.newPage();
   const errors=[]; for (const tab of [tabA,tabB]) tab.on('pageerror',e=>errors.push(e.message));
   const setting = async (tab, enabled) => tab.evaluate(async enabled => {
     const body = enabled ? {enabled, confirm: fullAccess.warning_id} : {enabled};
     renderFullAccess((await api('full-access', body)).full_access);
   }, enabled);
   await tabA.goto(base); await tabA.locator('.chat-row').first().waitFor();
   await setting(tabA, true);
   await tabA.evaluate(()=>document.querySelector('.chat-row .row-main').click());
   await tabA.locator('.message').first().waitFor();
   const chat = await tabA.evaluate(()=>state.id);
   await tabA.locator('#permission-button').click();
   await tabA.locator('[data-mode="full-access"]').click();
   await tabA.waitForFunction(()=>document.getElementById('permission-button').classList.contains('full-access'));
   const cdp = await context.newCDPSession(tabA);
   await cdp.send('Page.setWebLifecycleState',{state:'frozen'});
   await tabB.goto(base); await tabB.locator('.chat-row').first().waitFor();
   await setting(tabB, false);
   await setting(tabB, true);
   await cdp.send('Page.setWebLifecycleState',{state:'active'});
   const sent = tabA.waitForRequest(request=>request.method()==='POST' && /\/continue$/.test(new URL(request.url()).pathname));
   await tabA.locator('#message').fill('After the cycle');
   await tabA.waitForFunction(()=>!document.getElementById('send').disabled);
   await tabA.locator('#send').click();
   await sent;
   const turn = await tabB.evaluate(async id => (await api(`tasks/${encodeURIComponent(id)}`)).task, chat);
   assert.equal(turn.permission_mode,'default','asleep tab: the chat stays at Default');
   assert.equal(turn.turns.at(-1).permission_mode,'default','asleep tab: the message runs with Default');

   // A new chat chosen for Full access sends the time the setting was turned on, as the page saw it.
   await tabB.evaluate(()=>selectChat(null));
   await tabB.locator('#permission-button').click();
   await tabB.locator('[data-mode="full-access"]').click();
   await tabB.waitForFunction(()=>document.getElementById('permission-button').classList.contains('full-access'));
   const since = await tabB.evaluate(()=>fullAccess.changed_at);
   const created = tabB.waitForRequest(request=>request.method()==='POST' && /\/tasks$/.test(new URL(request.url()).pathname));
   await tabB.locator('#message').fill('A new chat');
   await tabB.waitForFunction(()=>!document.getElementById('send').disabled);
   await tabB.locator('#send').click();
   const body = (await created).postDataJSON();
   assert.equal(body.permission_mode,'full-access');
   assert.equal(body.full_access_since,since,'new chat: the page sends the time it saw');
   await setting(tabB, false);
   assert.deepEqual(errors,[]);
   await context.close();
  }
  console.log('PASS: Full access warning, cancel, confirm, banner, chat choice and marker, turn off, a Full access chat falls back to Default and stays there when Full access is turned on again, desktop and mobile; a tab asleep through an off and on cycle sends with Default; a new chat sends the time the setting was turned on');
 } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
