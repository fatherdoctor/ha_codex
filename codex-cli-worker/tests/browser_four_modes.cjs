// The four permission modes against web_fixture.py: the button offers them in the app's order, and Approve for me is saved and sent.
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
   await page.evaluate(()=>document.querySelector('.chat-row .row-main').click());
   await page.locator('.message').first().waitFor();
   await page.locator('#permission-button').click();
   const modes = await page.locator('#permissions-dialog [data-mode]').evaluateAll(list=>list.map(item=>item.dataset.mode));
   assert.deepEqual(modes,['default','auto-review','full-access','read-only'],'the four modes, in order');
   assert.equal(await page.locator('[data-mode="auto-review"]').isDisabled(),false,'Approve for me can be chosen');
   assert.match(await page.locator('[data-mode="auto-review"]').textContent(),/It does not ask you\./);
   await page.locator('[data-mode="auto-review"]').click();
   await page.waitForFunction(()=>document.getElementById('permission-button').getAttribute('aria-label')==='Approve for me');
   const chat = await page.evaluate(()=>state.id);
   const saved = await page.evaluate(async id => (await api(`tasks/${encodeURIComponent(id)}`)).task, chat);
   assert.equal(saved.permission_mode,'auto-review','the chat saves Approve for me');
   const sent = page.waitForRequest(request=>request.method()==='POST' && /\/continue$/.test(new URL(request.url()).pathname));
   await page.locator('#message').fill('Next message');
   await page.waitForFunction(()=>!document.getElementById('send').disabled);
   await page.locator('#send').click();
   assert.equal((await sent).postDataJSON().permission_mode,'auto-review','the next message sends Auto-review');
   await page.waitForFunction(()=>!document.getElementById('permission-button').disabled, null, {timeout:15000});
   await page.locator('#permission-button').click();
   await page.locator('[data-mode="default"]').click();
   await page.waitForFunction(()=>document.getElementById('permission-button').getAttribute('aria-label')==='Ask for approval');
   assert.deepEqual(errors,[]);
   await context.close();
  }
  console.log('PASS: the Permissions button offers Ask for approval, Approve for me, Full access and Read only in that order; Approve for me is saved on the chat and sent with the next message; desktop and mobile');
 } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
