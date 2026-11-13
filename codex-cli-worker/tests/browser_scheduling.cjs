// Synthetic schedule and mailbox UI against the disposable web fixture.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    for (const width of [390, 1100]) {
      const page = await browser.newPage({viewport:{width,height:844}});
      const errors=[]; page.on('pageerror', e=>errors.push(e.message));
      let schedules=[], mailbox=[], created, edits=0, newTokens=0, mailDeletes=0, acknowledgements=[];
      await page.route('**/schedules**', async route => {
        const req=route.request(), path=new URL(req.url()).pathname.replace('/preview/','');
        if (path.startsWith('assets/')) return route.continue();
        const data=req.postDataJSON();
        if (req.method()==='GET') return route.fulfill({json:{ok:true,timezone:'America/Chicago',schedules,runs:[{created:1803859200,status:'waiting_for_approval',task_id:'sample-task',error:''}],mailbox,lead_token_configured:newTokens>0}});
        if (path==='schedules') { created=data; schedules=[{...data,id:'sample',approved:true,enabled:true,revision:'revision-one',next_due:1803862800}]; if(data.spec.kind==='once') schedules[0].spec={kind:'once',at:Date.parse(data.spec.at+'-06:00')/1000}; }
        else if(req.method()==='PATCH') { edits++; schedules=[{...schedules[0],...data}]; }
        else if(path.endsWith('/pause')) schedules[0].enabled=false;
        else if(path.endsWith('/resume')) schedules[0].enabled=true;
        else if(path.endsWith('/approve')) { schedules[0].approved=true; schedules[0].enabled=true; }
        else if(path.endsWith('/delete')) schedules=[];
        return route.fulfill({json:{ok:true}});
      });
      await page.route('**/lead-token', route=>{newTokens++;return route.fulfill({json:{ok:true,token:'synthetic-once-only-value'}});});
      await page.route('**/schedule-mailbox**', async route=>{
        if (route.request().method()==='DELETE') { mailDeletes++; mailbox=[]; return route.fulfill({json:{ok:true}}); }
        const data=route.request().postDataJSON();
        if (route.request().url().endsWith('/read')) {
          assert.equal(await page.locator('#schedule-mailbox pre').count(), mailbox.length);
          acknowledgements.push(data.ids);
          return route.fulfill({json:{ok:true}});
        }
        mailbox.push({id:'mail-'+mailbox.length,parent:route.request().url().endsWith('/reply')?'mail-0':null,body:data.body,author:'owner',created:1803859200});
        return route.fulfill({json:{ok:true}});
      });
      async function openSettings() {
        const conversations=page.getByRole('button',{name:'Open conversations',exact:true});
        if(await conversations.isVisible()) await conversations.click();
        await page.getByRole('button',{name:'Open settings',exact:true}).click();
      }
      await page.goto('http://127.0.0.1:9137/preview/');
      await openSettings();
      await page.locator('#schedule-list').getByText('No schedules saved.').waitFor();
      await page.locator('#schedule-name').fill('Sample schedule');
      await page.locator('#schedule-prompt').fill('<img src=x onerror=alert(1)> Inspect sample.');
      await page.locator('#schedule-mode').selectOption('read-only');
      await page.locator('#schedule-kind').selectOption('hours');
      await page.locator('#schedule-value').fill('2');
      await page.locator('#schedule-form button[type=submit]').click();
      await page.locator('#schedule-list').getByText('Sample schedule',{exact:true}).waitFor();
      assert.equal(created.spec.seconds,7200);assert.equal(created.mode,'read-only');
      assert.equal(await page.locator('#schedule-list img').count(),0);
      await page.locator('#schedule-list').getByRole('button',{name:'Pause',exact:true}).click();
      await page.locator('#schedule-list').getByRole('button',{name:'Resume',exact:true}).waitFor();
      await page.locator('#schedule-list').getByRole('button',{name:'Resume',exact:true}).click();
      await page.locator('#schedule-list').getByRole('button',{name:'Edit',exact:true}).click();
      await page.locator('#schedule-kind').selectOption('cron');await page.locator('#schedule-value').fill('0 9 * * 1-5');
      await page.locator('#schedule-form button[type=submit]').click();
      await page.waitForFunction(()=>document.querySelector('#schedule-result').textContent==='Schedule saved and approved.');
      assert.equal(edits,1);assert.equal(schedules[0].spec.expression,'0 9 * * 1-5');
      await page.locator('#schedule-list').getByRole('button',{name:'Delete',exact:true}).click();
      await page.locator('#schedule-list').getByText('No schedules saved.').waitFor();
      // The browser zone is deliberately different from HA; submit local wall time without Z.
      await page.locator('#schedule-name').fill('Local once');
      await page.locator('#schedule-prompt').fill('Complete replacement prompt.');
      await page.locator('#schedule-kind').selectOption('once');
      await page.locator('#schedule-value').fill('2027-03-12T09:00');
      await page.locator('#schedule-form button[type=submit]').click();
      await page.locator('#schedule-list').getByText('Local once',{exact:true}).waitFor();
      assert.equal(created.spec.at,'2027-03-12T09:00:00');
      assert.ok((await page.locator('#schedule-timezone').textContent()).includes('America/Chicago'));
      schedules[0].prompt='Use [redacted]'; schedules[0].prompt_redacted=true;
      await page.evaluate(()=>document.querySelector('#settings').close()); await openSettings();
      await page.locator('#schedule-list').getByText('Prompt contains masked credentials.',{exact:false}).waitFor();
      assert.equal(await page.locator('#schedule-list').getByRole('button',{name:'Edit',exact:true}).isDisabled(),true);
      await page.locator('#schedule-list').getByRole('button',{name:'Delete',exact:true}).click();
      await page.locator('#schedule-list').getByText('No schedules saved.').waitFor();
      created={name:'Sample proposal',prompt:'Sample prompt',mode:'read-only',spec:{kind:'interval',seconds:3600}};
      // An agent proposal must visibly require approval, and its revision accompanies the action.
      schedules=[{...created,id:'sample',approved:false,enabled:false,revision:'current-proposal',next_due:1803862800}];
      await page.evaluate(()=>document.querySelector('#settings').close());
      await openSettings();
      await page.locator('#schedule-list').getByRole('button',{name:'Approve this proposal',exact:true}).click();
      await page.locator('#schedule-list').getByRole('button',{name:'Pause',exact:true}).waitFor();
      await page.locator('#schedule-mail-body').fill('<b>Plain sample message</b>');
      await page.locator('#schedule-mail-form button[type=submit]').click();
      await page.locator('#schedule-mailbox').getByText('<b>Plain sample message</b>',{exact:true}).waitFor();
      assert.equal(await page.locator('#schedule-mailbox b').count(),0);
      await page.locator('#schedule-mailbox').getByRole('button',{name:'Reply',exact:true}).click();
      await page.locator('#schedule-mail-body').fill('A sample reply');
      const replyRead = page.waitForResponse(response => response.url().endsWith('/schedule-mailbox/read')
        && response.request().postDataJSON().ids.length === 2);
      await page.locator('#schedule-mail-form button[type=submit]').click();
      await page.locator('#schedule-mailbox').getByText('A sample reply',{exact:true}).waitFor();
      await replyRead;
      assert.deepEqual(acknowledgements.at(-1), ['mail-0', 'mail-1']);
      assert.equal(mailbox[1].parent,'mail-0');
      await page.locator('#schedule-mailbox').getByRole('button',{name:'Clear mailbox',exact:true}).click();
      await page.waitForFunction(()=>document.querySelector('#schedule-mail-clear-confirmation') || document.querySelector('#schedule-mailbox').textContent.includes('No local messages.'));
      assert.equal(mailDeletes,0,'The first tap must not delete messages');
      const confirmation=page.locator('#schedule-mail-clear-confirmation');
      await confirmation.getByRole('button',{name:'Cancel',exact:true}).click();
      await page.locator('#schedule-mailbox').getByRole('button',{name:'Clear mailbox',exact:true}).waitFor();
      assert.equal(mailDeletes,0); assert.equal(mailbox.length,2);
      await page.locator('#schedule-mailbox').getByRole('button',{name:'Clear mailbox',exact:true}).click();
      await confirmation.waitFor();
      await page.locator('#close-settings').click(); await openSettings();
      await page.locator('#schedule-mailbox').getByRole('button',{name:'Clear mailbox',exact:true}).waitFor();
      assert.equal(await confirmation.count(),0,'Closing settings cancels pending confirmation');
      assert.equal(mailDeletes,0);
      await page.locator('#schedule-mailbox').getByRole('button',{name:'Clear mailbox',exact:true}).click();
      await confirmation.getByRole('button',{name:'Clear all messages',exact:true}).click();
      await page.locator('#schedule-mailbox').getByText('No local messages.').waitFor();
      assert.equal(mailDeletes,1);
      await page.locator('#lead-token-create').click();
      await page.locator('#lead-token-result').waitFor({state:'visible'});
      assert.equal(await page.locator('#lead-token-value').getAttribute('type'),'password');
      assert.equal(await page.locator('#lead-token-value').inputValue(),'synthetic-once-only-value');
      assert.equal(await page.evaluate(()=>JSON.stringify(localStorage).includes('synthetic-once-only-value')),false);
      assert.ok((await page.locator('#schedule-runs').textContent()).includes('waiting for approval'));
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.locator('#close-settings').click();
      await page.waitForFunction(()=>document.querySelector('#lead-token-value').value==='');
      assert.deepEqual(errors,[]);
      await page.close();
      console.log(`PASS ${width}px: schedule CRUD, HA-zone one-off entry, masked Edit disabled, proposal approval, literal rendering, in-page mailbox confirmation/cancel/close, pending status and token cleanup.`);
    }
  } finally {await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
