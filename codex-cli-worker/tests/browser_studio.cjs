// Quiet Studio against web_fixture.py. All pairing data is deliberately invalid.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const base = 'http://127.0.0.1:9137/preview/';
const phase = process.env.CX_UI_PHASE || 'after';
const output = process.env.CODEX_CHAT_QA_DIR || require('node:path').join(require('node:os').tmpdir(), 'codex-studio-qa');
fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser = await chromium.launch({headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 try {
  for (const width of [1024,390,1440]) for (const theme of ['light','dark']) {
   const context = await browser.newContext({viewport:{width,height:width===390?844:1000},colorScheme:theme,acceptDownloads:true});
   const page = await context.newPage();
   const errors=[]; page.on('pageerror',e=>errors.push(e.message));
   let remote = {enabled:true,available:true,state:'ready',configured_name:'Home Assistant',machine_name:'Home Assistant',message:'Connected',log:[]};
   await page.route('**/remote-control',r=>r.fulfill({json:{ok:true,remote_control:remote}}));
   await page.route('**/remote-control/pair', async r=>{
    const url='https://chatgpt.com/codex/pair?pairing_code=SAMPLE-NOT-VALID';
    remote={...remote,pairing_code:'DEMO-ONLY',pairing_url:url};
    // QR bytes come from the actual production encoder in a synthetic fixture.
    const result=await page.request.post(base+'fixture/studio-pairing');
    if(result.ok()) remote.pairing_qr=(await result.json()).qr;
    return r.fulfill({json:{ok:true,remote_control:remote}});
   });
   await page.goto(base);
   await page.locator('.chat-row').first().waitFor();
   if(phase==='before') {
    await page.screenshot({path:`${output}/before-${theme}-${width}.png`});
    assert.deepEqual(errors,[]);await context.close();continue;
   }
   await page.locator('#connection-summary').filter({hasText:'Ready as'}).waitFor();
   assert.equal(await page.locator('#greeting').isVisible(),true);
   assert.equal(await page.locator('#permission-button').getAttribute('aria-label'),'Default permissions');
   const compose=await page.locator('#composer').boundingBox();
   assert.ok(compose.y+compose.height<=page.viewportSize().height && compose.y>page.viewportSize().height/2);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
   if(width===390) assert.equal(await page.locator('.chat-header #menu').isVisible(),false);
   await page.screenshot({path:`${output}/after-${theme}-${width}.png`});
   await page.locator('#permission-button').click();
   assert.equal(await page.locator('.permission-option:disabled').count(),2);
   await page.locator('[data-mode="read-only"]').click();
   assert.equal(await page.locator('#permission-button').textContent(),'Read only⌄');
   await page.locator('#permission-button').click();
   await page.locator('[data-mode="default"]').click();
   await page.locator('#attach').click();
   assert.equal(await page.locator('#upload-document').isVisible(),true);
   assert.equal(await page.getByRole('menuitem',{name:'Camera',exact:true}).count(),0);
   await page.locator('#document-input').setInputFiles({name:'sample.md',mimeType:'text/markdown',buffer:Buffer.from('Synthetic attachment\n')});
   await page.locator('.pending-name').filter({hasText:'sample.md'}).waitFor();
   await page.locator('#message').fill('Review the attached synthetic document');
   const sent=page.waitForRequest(r=>r.url()===base+'tasks'&&r.method()==='POST');
   await page.locator('#send').click();
   const payload=(await sent).postDataJSON();
   assert.equal(payload.permission_mode,'default');assert.equal(payload.attachments[0].kind,'file');
   await page.locator('.document-attachment').waitFor();
   assert.equal(await page.locator('.document-attachment').textContent(),'sample.md · 1 KB ↓');
   // The fixture never invokes Codex; its synthetic worker completes this turn.
   await page.waitForFunction(()=>!document.getElementById('permission-button').disabled);
   await page.locator(width===390?'#phone-thread-actions':'#thread-actions').click();
   await page.locator('[data-action="status"]').click();
   assert.match(await page.locator('#status-details').textContent(),/Usage and resetUnavailable/);
   await page.locator('[data-close="status-dialog"]').click();
   const taskId = await page.evaluate(()=>state.id);
   const threadId = (await (await page.request.get(base+'tasks/'+taskId)).json()).task.session_id;
   await page.evaluate(()=>{window.studioCopied=null;Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.studioCopied=text;}}});});
   await page.locator(width===390?'#phone-thread-actions':'#thread-actions').click();
   await page.locator('[data-action="copy-id"]').click();
   await page.waitForFunction(expected=>window.studioCopied===expected,threadId);
   await page.locator(width===390?'#phone-thread-actions':'#thread-actions').click();
   await page.locator('[data-action="archive"]').click();
   await page.waitForFunction(()=>state.id===null);
   assert.equal((await (await page.request.get(base+'tasks/'+taskId)).json()).task.archived,true);
   if(width===390) await page.locator('#phone-history').click();
   await page.locator('#archive-list').click();
   await page.locator(`.row-main[data-task-id="${taskId}"]`).click();
   await page.waitForFunction(expected=>state.id===expected&&!state.loading,taskId);
   await page.locator(width===390?'#phone-thread-actions':'#thread-actions').click();
   await page.getByRole('menuitem',{name:'Restore chat',exact:true}).click();
   await page.waitForFunction(()=>state.id===null);
   const restored=(await (await page.request.get(base+'tasks/'+taskId)).json()).task;
   assert.equal(restored.archived,false);
   assert.equal(restored.turns[0].prompt_attachments[0].name,'sample.md');
   if(width===390) await page.locator('#phone-history').click();
   await page.locator('#archive-list').click();
   if(width===390) await page.locator('#close-sidebar').click();

   await page.locator('#header-settings').click();
   assert.equal(await page.locator('#agents').isVisible(),false);
   await page.locator('.advanced summary').click();
   await page.locator('#workspace-open').click();
   await page.waitForFunction(()=>!document.getElementById('agents').disabled);
   await page.locator('#agents').fill('Synthetic workspace rules.');
   await page.locator('#save-agents').click();
   await page.locator('#workspace-result').filter({hasText:'saved'}).waitFor();
   await page.locator('[data-close="workspace-dialog"]').click();
   // Five real state shapes, all from a synthetic intercepted status response.
   for (const state of ['off','starting','ready','reconnecting','error']) {
    remote={...remote,enabled:state!=='off',state,message:state==='error'?'Synthetic connection error':'Connected',pairing_code:'',pairing_url:''};
    await page.locator('#connection-chip').click();
    await page.waitForFunction(expected=>document.body.dataset.connection===expected,state);
    await page.screenshot({path:`${output}/connection-${state}-${theme}-${width}.png`});
    await page.locator('[data-close="connection-dialog"]').click();
   }
   remote={...remote,enabled:true,state:'ready',message:'Connected'};
   await page.evaluate(()=>pollConnection());
   await page.locator('#strip-pair').click();
   await page.locator('.pairing-continue').waitFor();
   assert.equal(await page.locator('.pairing-continue').getAttribute('href'),'https://chatgpt.com/codex/pair?pairing_code=SAMPLE-NOT-VALID');
   await page.locator('#copy-pairing-code').click();
   await page.waitForFunction(()=>/Copied|Code selected/.test(document.getElementById('copy-pairing-code').textContent));
   assert.match(await page.locator('#copy-pairing-code').textContent(), /Copied|Code selected/);
   await page.locator('.pairing-qr').waitFor();
   assert.ok((await page.locator('.pairing-qr').boundingBox()).width>150);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
   await page.screenshot({path:`${output}/pairing-${theme}-${width}.png`});
   await page.locator('.pairing-qr').screenshot({path:`${output}/qr-${theme}-${width}.png`});
   assert.deepEqual(errors,[]);
   console.log(`PASS ${phase} ${theme}/${width}: greeting, pinned composer, permission modes, file delivery, status, separate rules editor, five connection states, app URL and QR`);
   await context.close();
  }
  const hostContext = await browser.newContext({viewport:{width:1024,height:900}});
  const hostPage = await hostContext.newPage();
  await hostPage.route(base+'theme-host', route=>route.fulfill({contentType:'text/html',body:`<!doctype html><home-assistant style="--primary-color:#6344ff;--primary-background-color:#18212a;--card-background-color:#223344;--primary-text-color:#ddeeff"></home-assistant><script>document.querySelector('home-assistant').hass={themes:{darkMode:true}};<\/script><iframe src="${base}" style="width:100%;height:850px"></iframe>`}));
  await hostPage.goto(base+'theme-host');
  const frame=hostPage.frames().find(f=>f.url()===base);
  await frame.waitForFunction(()=>document.documentElement.dataset.theme==='dark');
  assert.equal(await frame.evaluate(()=>getComputedStyle(document.body).backgroundColor),'rgb(24, 33, 42)');
  assert.equal(await frame.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue('--primary-color').trim()),'#6344ff');
  await hostPage.evaluate(()=>{const h=document.querySelector('home-assistant');h.hass.themes.darkMode=false;h.style.setProperty('--primary-background-color','#edf2f7');});
  await frame.waitForFunction(()=>document.documentElement.dataset.theme==='light');
  assert.equal(await frame.evaluate(()=>getComputedStyle(document.body).backgroundColor),'rgb(237, 242, 247)');
  console.log('PASS same-origin HA theme tokens and dark-mode changes cross the iframe boundary');
  await hostContext.close();
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
