// Remote-control status warnings against a disposable worker fixture, never a relay.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    const page = await browser.newPage({viewport:{width:390,height:844}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let state = {
      enabled:true, available:true, machine_name:'', configured_name:'Home Assistant',
      message:'Connecting without a custom machine name…',
      name_warning:'Machine-name namespace unavailable; starting without a custom machine name.',
      policy_warning:'Codex reported that a request was downgraded to read-only by the managed policy.',
      log:['Managed policy: a relay request may fall back to read-only.'],
      config_findings:[{file:'example.yaml',line:1,reason:'a change to a block that holds a refused action',undone:'restored',recovery_copy:'/data/remote_control_undo/example.yaml.fixture'}],
      config_findings_count:1,
    };
    await page.route('**/remote-control', route => route.fulfill({json:{ok:true,remote_control:state}}));
    await page.goto('http://127.0.0.1:9137/preview/');
    await page.getByRole('button',{name:'Open conversations',exact:true}).click();
    await page.getByRole('button',{name:'Open settings',exact:true}).click();
    await page.locator('#remote-control-name-warning').waitFor({state:'visible'});
    assert.match(await page.locator('#remote-control-name-warning').textContent(),/without a custom/);
    assert.match(await page.locator('#remote-control-policy-warning').textContent(),/was downgraded to read-only/);
    assert.match(await page.locator('#remote-control-note').textContent(),/Not every relay/);
    assert.match(await page.locator('#remote-control-findings-list').textContent(),/restored/);
    assert.match(await page.locator('#remote-control-findings-list').textContent(),/replaced version saved at \/data\/remote_control_undo/);
    assert.equal(await page.locator('#remote-control-start').isVisible(),false);
    state = {...state,name_warning:'<img src=x onerror=alert(1)>',policy_warning:'',config_findings:[{file:'automations.yaml',line:1,reason:'a planted action',undone:'',undo_note:'Home Assistant manages this file; review without automatic undo.'}],config_findings_count:1};
    await page.getByRole('button',{name:'Close settings',exact:true}).click();
    await page.getByRole('button',{name:'Open conversations',exact:true}).click();
    await page.getByRole('button',{name:'Open settings',exact:true}).click();
    await page.waitForFunction(() => document.querySelector('#remote-control-name-warning').textContent.startsWith('<img'));
    assert.equal(await page.locator('#remote-control-name-warning img').count(),0);
    assert.equal(await page.locator('#remote-control-policy-warning').isVisible(),false);
    assert.match(await page.locator('#remote-control-findings-list').textContent(),/without automatic undo/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
    assert.deepEqual(errors,[]);
    console.log('PASS: remote-control name failure, observed downgrade, honest fallback note, persisted undo and safe narrow-screen rendering.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode=1;});
