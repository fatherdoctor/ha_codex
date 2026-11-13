// Address source, first-run read guidance and stale-state clearing on the local fixture.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  for (const width of [390,1100]) {
   const page=await browser.newPage({viewport:{width,height:844}});const errors=[];
   page.on('pageerror',error=>errors.push(error.message));
   let address={url:'http://homeassistant:80',source:'discovery',error:''};
   await page.route('**/remote-control',route=>route.fulfill({json:{ok:true,remote_control:{
    enabled:true,available:true,state:'ready',message:'Connected',configured_name:'Sample relay',
    core_address:address,read_tools:'Read-only entity tools are offered; no Core API token is needed.',log:[]}}}));
   async function settings() {
    const menu=page.getByRole('button',{name:'Open conversations',exact:true});if(await menu.isVisible())await menu.click();
    await page.getByRole('button',{name:'Open settings',exact:true}).click();
    await page.getByRole('button',{name:'Manage remote connection',exact:true}).click();
   }
   await page.goto('http://127.0.0.1:9137/preview/');await settings();
   await page.waitForFunction(()=>document.querySelector('#remote-core-address').textContent.includes(':80'));
   assert.ok((await page.locator('#remote-core-address').textContent()).includes('discovery'));
   assert.ok((await page.locator('#remote-read-tools').textContent()).includes('no Core API token'));
   await page.locator('[data-close=connection-dialog]').click();
   address={url:null,source:'unavailable',error:'<img src=x onerror=alert(1)> Discovery unavailable.'};
   await settings();await page.waitForFunction(()=>document.querySelector('#remote-core-address').textContent.includes('unavailable'));
   assert.equal(await page.locator('#remote-core-address img').count(),0);
   assert.ok(!(await page.locator('#remote-core-address').textContent()).includes(':80'));
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
   assert.deepEqual(errors,[]);await page.close();
   console.log(`PASS ${width}px: discovered address/source, token-free read guidance, stale URL removal and literal error rendering.`);
  }
 }finally{await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
