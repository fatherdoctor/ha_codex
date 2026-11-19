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
      // The Codex row at the foot of the side bar follows the sign-in; a waiting code needs the owner.
      await page.locator('#codex-status-summary').filter({hasText:/^Signing in$/}).waitFor();
      assert.equal(await page.locator('#codex-status-row').getAttribute('data-state'),'needs-owner');
      // The plan shows under the quota bars, as information only; the access line sits in the ChatGPT account part.
      const access=page.locator('#codex-access');
      await page.waitForFunction(()=>/cannot be confirmed/.test(document.querySelector('#codex-access').textContent));
      assert.match(await page.locator('#usage-plan').textContent(),/^Free plan/);
      assert.doesNotMatch(await page.locator('body').textContent(),/SYNTHETIC_PRIVATE_ACCOUNT/);
      plan=null;
      await page.evaluate(()=>loadUsage());
      await page.locator('#usage-plan').filter({hasText:/^Plan unknown/}).waitFor();
      assert.equal(await access.textContent(),'Codex access cannot be confirmed by a passive read. Plan type is information only.');
      unavailable=true;
      await page.evaluate(()=>loadUsage());
      await page.waitForFunction(()=>document.querySelector('#codex-access').textContent==='Codex access could not be checked.');
      assert.deepEqual(errors,[]);
      console.log(`PASS ${width}px: dated requirement before code, Codex row, informational plan, uncertain access, no stale plan or account text`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
