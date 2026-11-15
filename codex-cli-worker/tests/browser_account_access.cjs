// All account metadata and sign-in codes in this test are synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    for (const width of [390, 1100]) {
      const page = await browser.newPage({viewport:{width,height:844}});
      const errors=[]; page.on('pageerror', error=>errors.push(error.message));
      let plan='free', unavailable=false;
      await page.route('**/status', async route => {
        if (new URL(route.request().url()).pathname.endsWith('/auth/status')) {
          return route.fulfill({json:{ok:true,auth:{pending:true,status:'waiting_for_user',
            login_id:'synthetic',user_code:'SYNTHETIC-CODE',codex_login:{status_ok:false}}}});
        }
        if (unavailable) return route.fulfill({status:503,json:{ok:false,error:'Synthetic unavailable'}});
        const response=await route.fetch();
        const data=await response.json();
        data.account_access={state:'unconfirmed',needs_sign_in:false,plan_type:plan,
          message:'Codex access cannot be confirmed by a passive read. Plan type is information only.',
          email:'SYNTHETIC_PRIVATE_ACCOUNT'};
        return route.fulfill({response,json:data});
      });
      await page.goto('http://127.0.0.1:9137/preview/');
      const banner=page.locator('#signin-banner-card');
      await banner.locator('.signin-code').waitFor();
      const beforeCode=await banner.evaluate(card => {
        const requirement=[...card.querySelectorAll('p')].find(p=>p.textContent.includes('Codex access is required'));
        return requirement && Boolean(requirement.compareDocumentPosition(card.querySelector('.signin-code'))
          & Node.DOCUMENT_POSITION_FOLLOWING);
      });
      assert.ok(beforeCode,'The requirement precedes the device code');
      assert.equal(await banner.locator('a[href*="11369540"]').count(),1);
      // The same production handler serves the desktop button and phone menu.
      await page.evaluate(()=>showStudioStatus());
      await page.locator('#status-details dd').filter({hasText:/^free$/}).waitFor();
      assert.match(await page.locator('#status-details').textContent(),/information only/);
      assert.match(await page.locator('#status-details').textContent(),/cannot be confirmed/);
      assert.doesNotMatch(await page.locator('#status-details').textContent(),/SYNTHETIC_PRIVATE_ACCOUNT/);
      await page.locator('[data-close="status-dialog"]').click();
      plan=null;
      await page.evaluate(()=>showStudioStatus());
      await page.locator('#status-details').getByText('Codex access cannot be confirmed by a passive read. Plan type is information only.',{exact:true}).waitFor();
      assert.doesNotMatch(await page.locator('#status-details').textContent(),/information only\)free/);
      await page.locator('[data-close="status-dialog"]').click();
      unavailable=true;
      await page.evaluate(()=>showStudioStatus());
      await page.locator('#status-details').getByText('Codex access could not be checked.',{exact:true}).waitFor();
      assert.deepEqual(errors,[]);
      console.log(`PASS ${width}px: dated requirement before code, informational plan, uncertain access, no stale plan or account text`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
