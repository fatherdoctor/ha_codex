// Pairing card and error refresh against a disposable fixture; codes are synthetic.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    for (const width of [390, 1100]) {
      const page = await browser.newPage({viewport:{width,height:844}});
      const errors=[];page.on('pageerror',e=>errors.push(e.message));
      let fail=false;
      let state={enabled:true,available:true,state:'ready',message:'Available in the ChatGPT app.',machine_name:'Home Assistant',pairing_code:'',pairing_qr:'',log:[]};
      await page.route('**/remote-control',r=>r.fulfill({json:{ok:true,remote_control:state}}));
      await page.route('**/remote-control/pair',r=>{
        state={...state,pairing_code:fail?'':'TEST-0000',pairing_qr:'',log:['A new pairing code is ready on this page.']};
        return r.fulfill({status:fail?409:200,json:{ok:!fail,error:fail?'Codex could not provide a new pairing code.':'',remote_control:state}});
      });
      await page.goto('http://127.0.0.1:9137/preview/');
      const conversations=page.getByRole('button',{name:'Open conversations',exact:true});
      if(await conversations.isVisible()) await conversations.click();
      await page.getByRole('button',{name:'Open settings',exact:true}).click();
      await page.locator('#remote-control-pair').click();
      await page.locator('#remote-control-card .pairing-code').waitFor({state:'visible'});
      assert.equal(await page.locator('#remote-control-card .pairing-code').textContent(),'TEST-0000');
      assert.ok(!(await page.locator('#remote-control-log').textContent()).includes('TEST-0000'));
      fail=true;await page.locator('#remote-control-pair').click();
      await page.waitForFunction(()=>document.querySelector('#settings-result').textContent.includes('could not provide'));
      await page.locator('#remote-control-card .pairing-code').waitFor({state:'detached'});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      assert.deepEqual(errors,[]);await page.close();
    }
    console.log('PASS: page-only synthetic pairing code, honest failure, stale-code refresh and responsive rendering.');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
