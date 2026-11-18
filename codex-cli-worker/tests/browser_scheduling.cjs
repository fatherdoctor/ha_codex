// Synthetic schedule editor (opened from a chat's menu and from Settings) and mailbox UI against the disposable web fixture.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
(async () => {
  const browser = await chromium.launch({headless:true});
  try {
    for (const width of [390, 1100]) {
      const page = await browser.newPage({viewport:{width,height:844}});
      const errors=[]; page.on('pageerror', e=>errors.push(e.message));
      let schedules=[], runs=[], mailbox=[], created, edits=0, newTokens=0, mailDeletes=0, acknowledgements=[], refusal='', approvedRevision;
      await page.route('**/schedules**', async route => {
        const req=route.request(), path=new URL(req.url()).pathname.replace('/preview/','');
        if (path.startsWith('assets/')) return route.continue();
        const data=req.postDataJSON();
        if (req.method()==='GET') return route.fulfill({json:{ok:true,timezone:'America/Chicago',schedules,runs:[...runs,{created:1803859200,status:'waiting_for_approval',task_id:'sample-task',error:''}],mailbox,lead_token_configured:newTokens>0}});
        if (refusal) return route.fulfill({status:400,json:{ok:false,error:refusal}});
        if (path==='schedules') { created=data; schedules=[{...data,id:'sample',approved:true,enabled:true,revision:'revision-one',next_due:1803862800}]; if(data.spec.kind==='once') schedules[0].spec={kind:'once',at:Date.parse(data.spec.at+'-06:00')/1000}; }
        else if(req.method()==='PATCH') { edits++; schedules=[{...schedules[0],...data}]; }
        else if(path.endsWith('/pause')) schedules[0].enabled=false;
        else if(path.endsWith('/resume')) schedules[0].enabled=true;
        else if(path.endsWith('/approve')) { approvedRevision=data.revision; schedules[0].approved=true; schedules[0].enabled=true; }
        else if(path.endsWith('/delete')) schedules=[];
        return route.fulfill({json:{ok:true}});
      });
      await page.route('**/lead-token', route=>{newTokens++;return route.fulfill({json:{ok:true,token:'synthetic-once-only-value'}});});
      await page.route('**/schedule-mailbox**', async route=>{
        if (route.request().method()==='DELETE') { assert.deepEqual(route.request().postDataJSON(), {confirm:true}); assert.equal(new URL(route.request().url()).searchParams.get('mailbox'), 'owner'); mailDeletes++; mailbox=[]; return route.fulfill({json:{ok:true}}); }
        const data=route.request().postDataJSON();
        if (new URL(route.request().url()).pathname.endsWith('/read')) {
          assert.equal(await page.locator('#schedule-mailbox details').count(), mailbox.length);
          acknowledgements.push(data.ids);
          return route.fulfill({json:{ok:true}});
        }
        mailbox.push({id:'mail-'+mailbox.length,parent:route.request().url().endsWith('/reply')?'mail-0':null,envelope:{to:data.to,from:'owner',sent:'2027-03-01T00:00:00.000Z',last_modified:'2027-03-01T00:00:00.000Z',due:data.due,message:data.message},origin:'lead',text:'To: '+data.to+'\nFrom: owner\nSent: 2027-02-28 18:00:00 (America/Chicago)\nLast modified: 2027-02-28 18:00:00 (America/Chicago)\n\n'+data.message});
        return route.fulfill({json:{ok:true}});
      });
      /** On a phone the chat list slides over the chat; open it unless it is open already. */
      async function openList() {
        const conversations=page.getByRole('button',{name:'Open conversations',exact:true});
        const listOpen=await page.evaluate(()=>document.body.classList.contains('sidebar-open'));
        if(await conversations.isVisible() && !listOpen) await conversations.click();
      }
      async function openSettings() {
        await openList();
        await page.getByRole('button',{name:'Open settings',exact:true}).click();
        // Settings sections start collapsed; a section stays open once opened.
        const toggle=page.locator('#scheduled-work .settings-toggle');
        if(await toggle.getAttribute('aria-expanded')==='false') await toggle.click();
      }
      await page.goto('http://127.0.0.1:9137/preview/');
      await page.waitForFunction(() => typeof state !== 'undefined' && document.querySelector('.chat-row'));
      await page.evaluate(() => {
        state.task = {task_id:'synthetic-source',status:'completed',task_source:'lead',turns:[
          {turn_id:'first',message:'Lead request',task_source:'lead'},
          {turn_id:'second',message:'Human continuation'},
          {turn_id:'third',message:'Scheduled request',task_source:'schedule'}]}; renderTask(true);
      });
      assert.deepEqual(await page.locator('.task-source').allTextContents(), ['Started by lead', 'Scheduled run']);
      // Schedule in a chat's menu opens the editor on a new schedule that starts from the chat.
      const chatId=await page.locator('.chat-row [data-menu-for]').first().getAttribute('data-menu-for');
      async function openFromMenu() {
        await openList();
        await page.locator(`.chat-row:has([data-menu-for="${chatId}"])`).hover();
        await page.locator(`[data-menu-for="${chatId}"]`).click();
        await page.locator('#chat-menu').getByRole('menuitem',{name:'Schedule',exact:true}).click();
        await editor.waitFor({state:'visible'});
      }
      const editor=page.locator('#schedule-dialog');
      const save=page.locator('#schedule-save');
      await openFromMenu();
      assert.equal(await page.locator('#schedule-title').textContent(),'Schedule this chat');
      assert.equal(await save.textContent(),'Create schedule');
      assert.notEqual(await page.locator('#schedule-name').inputValue(),'','the name starts as the chat title');
      assert.notEqual(await page.locator('#schedule-prompt').inputValue(),'','the prompt starts as the first message');
      // The editor shows name, prompt, model and trigger; the rest is folded under Advanced settings.
      assert.equal(await page.locator('#schedule-advanced').evaluate(d=>d.open),false);
      assert.equal(await page.locator('#schedule-mode').isVisible(),false);
      assert.equal(await page.locator('#schedule-trigger').inputValue(),'hourly');
      assert.equal(await page.locator('#schedule-time').isVisible(),false);
      assert.equal(await page.locator('#schedule-custom').isVisible(),false);
      assert.equal(await page.locator('#schedule-model option').first().textContent(),'Add-on default');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
      await page.locator('#schedule-name').fill('Sample schedule');
      await page.locator('#schedule-prompt').fill('<img src=x onerror=alert(1)> Inspect sample.');
      await save.click();
      await editor.waitFor({state:'hidden'});
      assert.deepEqual(created.spec,{kind:'interval',seconds:3600});
      assert.equal(created.chat_id,chatId);assert.equal(created.model,null);assert.equal(created.mode,'default');
      // The same menu item now opens that schedule, with its state and its runs under Advanced settings.
      runs=[{created:1803859200,status:'completed',task_id:'sample-run',schedule_id:'sample',error:''}];
      await openFromMenu();
      assert.equal(await page.locator('#schedule-title').textContent(),'Edit schedule');
      assert.equal(await page.locator('#schedule-name').inputValue(),'Sample schedule');
      assert.equal(await page.locator('#schedule-trigger').inputValue(),'hourly');
      await page.locator('#schedule-advanced > summary').click();
      assert.match(await page.locator('#schedule-state').textContent(),/^On · Every hour · /);
      assert.equal(await page.locator('#schedule-own-runs a').count(),1);
      await page.locator('#schedule-toggle').click();
      await page.locator('#schedule-toggle').getByText('Resume',{exact:true}).waitFor();
      assert.match(await page.locator('#schedule-state').textContent(),/^Paused · /);
      await page.locator('#schedule-toggle').click();
      await page.locator('#schedule-toggle').getByText('Pause',{exact:true}).waitFor();
      // Weekly: a day and a time become one cron line; the model and the permissions are sent with it.
      await page.locator('#schedule-trigger').selectOption('weekly');
      assert.equal(await page.locator('#schedule-custom').isVisible(),false);
      await page.locator('#schedule-weekday').selectOption('5');
      await page.locator('#schedule-time').fill('07:05');
      await page.locator('#schedule-model').selectOption({index:1});
      const model=await page.locator('#schedule-model').inputValue();
      await page.locator('#schedule-mode').selectOption('read-only');
      assert.equal(await save.textContent(),'Save changes');
      await save.click();
      await editor.waitFor({state:'hidden'});
      assert.equal(edits,1);assert.equal(schedules[0].spec.expression,'5 7 * * 5');
      assert.equal(schedules[0].model,model);assert.equal(schedules[0].mode,'read-only');assert.equal(schedules[0].chat_id,chatId);
      // A chat that a schedule started opens that schedule too, with the saved timing shown as it was entered.
      schedules[0].chat_id='';runs=[{created:1803859200,status:'completed',task_id:chatId,schedule_id:'sample',error:''}];
      await openFromMenu();
      assert.equal(await page.locator('#schedule-title').textContent(),'Edit schedule');
      assert.equal(await page.locator('#schedule-trigger').inputValue(),'weekly');
      assert.equal(await page.locator('#schedule-weekday').inputValue(),'5');
      assert.equal(await page.locator('#schedule-time').inputValue(),'07:05');
      assert.equal(await page.locator('#schedule-model').inputValue(),model);
      // Daily, then a custom cron line that no preset names.
      await page.locator('#schedule-trigger').selectOption('daily');
      assert.equal(await page.locator('#schedule-weekday').isVisible(),false);
      await save.click();
      await editor.waitFor({state:'hidden'});
      assert.equal(schedules[0].spec.expression,'5 7 * * *');
      await openFromMenu();
      assert.equal(await page.locator('#schedule-trigger').inputValue(),'daily');
      assert.match(await page.locator('#schedule-state').textContent(),/^On · Every day at 07:05 \(America\/Chicago\) · /);
      await page.locator('#schedule-trigger').selectOption('custom');
      await page.locator('#schedule-kind').selectOption('cron');await page.locator('#schedule-value').fill('0 9 * * 1-5');
      await save.click();
      await editor.waitFor({state:'hidden'});
      assert.equal(schedules[0].spec.expression,'0 9 * * 1-5');
      await openFromMenu();
      assert.equal(await page.locator('#schedule-trigger').inputValue(),'custom');
      assert.equal(await page.locator('#schedule-kind').inputValue(),'cron');
      assert.equal(await page.locator('#schedule-value').inputValue(),'0 9 * * 1-5');
      // A refused save stays in the editor and says why.
      refusal='Cron value is out of range.';
      await save.click();
      await page.locator('#schedule-error').getByText('Cron value is out of range.',{exact:true}).waitFor();
      assert.equal(await editor.isVisible(),true);
      refusal='';
      // Custom keeps every older timing: every N hours, and one run at a Home Assistant wall time.
      await page.locator('#schedule-kind').selectOption('hours');await page.locator('#schedule-value').fill('2');
      await save.click();
      await editor.waitFor({state:'hidden'});
      assert.deepEqual(schedules[0].spec,{kind:'interval',seconds:7200});
      await openFromMenu();
      assert.equal(await page.locator('#schedule-kind').inputValue(),'hours');
      assert.equal(await page.locator('#schedule-value').inputValue(),'2');
      await page.locator('#schedule-advanced > summary').click();
      await page.locator('#schedule-delete').click();
      await editor.waitFor({state:'hidden'});
      assert.deepEqual(schedules,[]);
      // The browser zone is deliberately different from HA; submit local wall time without Z.
      runs=[];
      await openFromMenu();
      assert.equal(await page.locator('#schedule-title').textContent(),'Schedule this chat');
      await page.locator('#schedule-name').fill('Local once');
      await page.locator('#schedule-prompt').fill('Complete replacement prompt.');
      await page.locator('#schedule-trigger').selectOption('custom');
      await page.locator('#schedule-kind').selectOption('once');
      await page.locator('#schedule-value').fill('2027-03-12T09:00');
      await save.click();
      await editor.waitFor({state:'hidden'});
      assert.equal(created.spec.at,'2027-03-12T09:00:00');
      // Settings lists every schedule; Edit opens the same editor, on top of Settings.
      await openSettings();
      await page.locator('#schedule-list').getByText('Local once',{exact:true}).waitFor();
      assert.equal(await page.locator('#settings #schedule-form').count(),0,'Settings has no schedule form');
      assert.equal(await page.locator('#schedule-list img').count(),0);
      assert.ok((await page.locator('#schedule-timezone').textContent()).includes('America/Chicago'));
      await page.locator('#schedule-list').getByRole('button',{name:'Edit',exact:true}).click();
      await editor.waitFor({state:'visible'});
      assert.equal(await page.locator('#schedule-kind').inputValue(),'once');
      assert.equal(await page.locator('#schedule-value').inputValue(),'2027-03-12T09:00');
      await editor.getByRole('button',{name:'Cancel',exact:true}).click();
      await page.locator('#schedule-list').getByRole('button',{name:'Pause',exact:true}).click();
      await page.locator('#schedule-list').getByRole('button',{name:'Resume',exact:true}).waitFor();
      // A masked prompt cannot be edited from Settings or from the chat: only read, paused or deleted.
      schedules[0].prompt='Use [redacted]'; schedules[0].prompt_redacted=true; schedules[0].chat_id=chatId;
      await page.evaluate(()=>document.querySelector('#settings').close()); await openSettings();
      await page.locator('#schedule-list').getByText('Prompt contains masked credentials.',{exact:false}).waitFor();
      assert.equal(await page.locator('#schedule-list').getByRole('button',{name:'Edit',exact:true}).isDisabled(),true);
      await page.locator('#close-settings').click();
      await openFromMenu();
      assert.equal(await page.locator('#schedule-masked').isVisible(),true);
      assert.equal(await page.locator('#schedule-prompt').isDisabled(),true);
      assert.equal(await save.isDisabled(),true);
      await page.locator('#schedule-delete').click();
      await editor.waitFor({state:'hidden'});
      assert.deepEqual(schedules,[]);
      created={name:'Sample proposal',prompt:'Sample prompt',mode:'read-only',spec:{kind:'interval',seconds:3600}};
      // An agent proposal must visibly require approval, and its revision accompanies the action.
      schedules=[{...created,id:'sample',approved:false,enabled:false,revision:'current-proposal',next_due:1803862800}];
      await openSettings();
      await page.locator('#schedule-list').getByRole('button',{name:'Edit',exact:true}).click();
      await editor.waitFor({state:'visible'});
      assert.equal(await save.textContent(),'Save and approve');
      await editor.getByRole('button',{name:'Cancel',exact:true}).click();
      await page.locator('#schedule-list').getByRole('button',{name:'Approve this proposal',exact:true}).click();
      await page.locator('#schedule-list').getByRole('button',{name:'Pause',exact:true}).waitFor();
      assert.equal(approvedRevision,'current-proposal');
      await page.locator('#schedule-mail-to').selectOption('codex');
      await page.locator('#schedule-mail-due').fill('2027-03-02T00:00:00Z');
      await page.locator('#schedule-mail-body').fill('<b>Plain sample message</b>');
      await page.locator('#schedule-mail-form button[type=submit]').click();
      await page.locator('#schedule-mailbox').getByText('<b>Plain sample message</b>',{exact:true}).waitFor();
      assert.equal(await page.locator('#schedule-mailbox b').count(),0);
      assert.deepEqual(await page.locator('#schedule-mailbox > div > strong').allTextContents(), ['Started by lead', 'To', 'From', 'Sent', 'Last modified', 'Due', 'Message']);
      assert.ok((await page.locator('#schedule-mailbox').innerText()).includes('(America/Chicago)'));
      await page.locator('#schedule-mailbox details summary').click();
      assert.ok((await page.locator('#schedule-mailbox details pre').innerText()).startsWith('To: codex\nFrom: owner\nSent:'));
      await page.locator('#schedule-mail-reader').selectOption('lead');
      await page.waitForResponse(response => new URL(response.url()).searchParams.get('mailbox') === 'lead');
      await page.locator('#schedule-mail-reader').selectOption('owner');
      await page.waitForResponse(response => new URL(response.url()).searchParams.get('mailbox') === 'owner');
      await page.locator('#schedule-mailbox').getByRole('button',{name:'Reply',exact:true}).click();
      await page.locator('#schedule-mail-body').fill('A sample reply');
      const replyRead = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/schedule-mailbox/read')
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
      console.log(`PASS ${width}px: schedule editor from a chat's menu (hourly, daily, weekly, custom), HA-zone one-off entry, masked prompt read-only, proposal approval, literal rendering, in-page mailbox confirmation/cancel/close, pending status and token cleanup.`);
    }
  } finally {await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
