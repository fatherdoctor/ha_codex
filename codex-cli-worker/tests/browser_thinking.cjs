// Run against web_fixture.py: live summary changes at phone and desktop widths.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'/usr/bin/chromium'});
  for (const width of [320, 1280]) {
    const page = await browser.newPage({viewport:{width,height:900}});
    await page.goto('http://127.0.0.1:9137/preview/');
    await page.locator('[data-task-id="preview-04"]').evaluate(el => el.click());
    await page.locator('#activity').waitFor({state:'attached'});
    await page.evaluate(() => {
      clearTimeout(activityTimer); activityTimer = null;
      state.task.status='running'; state.latestChecks=null;
      state.activity={id:state.id,turnId:'example',seq:1,steps:new Map([[1,{index:1,kind:'reasoning',status:'running',text:'Reviewing the\ninput'}]]),running:true,startedAt:Date.now()-3000,expanded:false,open:new Set()};
      renderActivity();
    });
    assert.equal(await page.locator('.activity-thinking').textContent(), 'Thinking: Reviewing the input');
    assert.match(await page.locator('.activity-toggle').textContent(), /1 step.*3s/);
    await page.evaluate(() => {state.activity.steps.set(1,{index:1,kind:'reasoning',status:'running',text:'Checking the results'});renderActivity()});
    assert.equal(await page.locator('.activity-thinking').textContent(), 'Thinking: Checking the results');
    await page.evaluate(() => {state.activity.steps.set(2,{index:2,kind:'reasoning',status:'completed',text:'Latest summary'});renderActivity()});
    assert.equal(await page.locator('.activity-thinking').textContent(), 'Thinking: Latest summary');
    assert.equal(await page.locator('.activity-thinking').count(), 1);
    assert.match(await page.locator('.activity-toggle').textContent(), /2 steps/);
    await page.evaluate(() => {state.activity.running=false;state.task.status='completed';renderActivity()});
    assert.equal(await page.locator('.activity-thinking').count(), 0);
    await page.evaluate(() => {state.activity.running=true;state.task.status='running';state.activity.steps.clear();state.activity.steps.set(3,{index:3,kind:'command',status:'running',text:'Checking'});renderActivity()});
    assert.equal(await page.locator('.activity-thinking').count(), 0);
    console.log(`${width}px: summary update and replacement; count and time retained; absent after completion or without summary`);
    await page.close();
  }
  await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
