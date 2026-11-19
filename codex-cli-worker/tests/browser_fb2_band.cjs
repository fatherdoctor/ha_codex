// The top bands move to the side bar's foot (CX-FB2-BAND), on a phone and a desktop, in light and dark.
// The chat keeps one thin title row; Home Assistant status, the remote connection, the quota with its plan line and
// Settings are rows at the side bar's foot; the send button becomes the stop button while a task runs. Also checked
// inside a host window that sets Home Assistant's warning color, and with no host window. Run with
// tests/web_fixture.py serving the page. Every name, plan and value is synthetic.
// Screenshots go to FB2_SHOTS (default /tmp/codex-fb2-band-qa).
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const base = 'http://127.0.0.1:9137/preview/';
const SHOTS = process.env.FB2_SHOTS || '/tmp/codex-fb2-band-qa';
// Home Assistant's own icon paths (@mdi/js 7.4.47), so a hand-drawn icon in their place fails the check.
const MDI_COG_START = 'M12,15.5A3.5,3.5 0 0,1 8.5,12';
const MDI_STOP = 'M18,18H6V6H18V18Z';
const AMBER_FALLBACK = 'rgb(178, 106, 0)';

/** Set the integration state that the fixture reports, then read the status again. */
async function setIntegration(page, state) {
  await page.evaluate(async value => {
    await fetch('fixture/integration', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({state: value}),
    });
    await loadUsage();
  }, state);
}

/** The text and dot color of a foot row. */
function footRow(page, id) {
  return page.evaluate(selector => {
    const row = document.querySelector(selector);
    return {text: row.textContent.replace(/\s+/g, ' ').trim(), dot: getComputedStyle(row.querySelector('.status-dot')).backgroundColor,
      state: row.dataset.state || ''};
  }, id);
}

/** Open the chats drawer on a phone, so the foot rows can be clicked. */
async function showSidebar(page, phone) {
  if (phone && !(await page.evaluate(() => document.body.classList.contains('sidebar-open'))))
    await page.locator('#phone-history').click();
}

(async () => {
  fs.mkdirSync(SHOTS, {recursive: true});
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage']});
  try {
    for (const [width, height] of [[1280, 900], [390, 844]]) for (const theme of ['light', 'dark']) {
      const phone = width < 700;
      const context = await browser.newContext({viewport: {width, height}, colorScheme: theme});
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(base);
      await page.locator('.chat-row').first().waitFor();
      await setIntegration(page, 'ok');

      // 1. The two top bands are gone: no strip, no chip, no Status button, no gear and no Stop task in the header.
      for (const gone of ['.connection-strip', '#connection-chip', '#header-status', '#header-settings', '#strip-pair',
        '.header-actions', '.chat-header #cancel'])
        assert.equal(await page.locator(gone).count(), 0, `${gone} is gone`);
      const header = await page.locator('.chat-header').boundingBox();
      if (phone) assert.equal(header, null, 'a phone shows its slim row, not the title row');
      else {
        assert.ok(header.height <= 44, `the title row is thin (${header.height} px)`);
        assert.deepEqual(await page.evaluate(() => [...document.querySelector('.chat-header').children]
          .filter(node => node.getBoundingClientRect().width > 1).map(node => node.id)), ['chat-title']);
      }

      // 2. The side bar's foot, top to bottom: Codex, GitHub, Remote, quota, Settings.
      assert.deepEqual(await page.evaluate(() => [...document.querySelector('.sidebar-footer').children]
        .filter(node => !node.hidden).map(node => node.id)),
      ['codex-status-row', 'github-status-row', 'remote-status-row', 'usage', 'usage-plan', 'settings-button']);
      // The quota bars show; the studio layout used to hide them.
      assert.ok((await page.locator('#usage').boundingBox())?.height > 0, 'the quota bars show');
      // One way into Settings: no other gear or Settings button on the page.
      assert.equal(await page.locator('button:has-text("⚙")').count(), 0);
      assert.equal(await page.getByRole('button', {name: /settings/i}).count(), 1);
      // The gear and the stop square are Material Design Icons.
      const masks = await page.evaluate(() => ['.mdi-cog', '.mdi-stop'].map(selector => {
        const style = getComputedStyle(document.querySelector(selector));
        return decodeURIComponent(style.maskImage || style.webkitMaskImage);
      }));
      assert.ok(masks[0].includes(MDI_COG_START), 'the Settings row uses mdiCog');
      assert.ok(masks[1].includes(MDI_STOP), 'the stop button uses mdiStop');

      // 3. The Codex, GitHub and Remote rows: one or two words and a dot; each opens Settings at its part.
      await page.locator('#codex-status-summary').filter({hasText: /^Ready$/}).waitFor({state: 'attached'});
      await page.locator('#remote-status-summary').filter({hasText: /^(Ready|Off|Unavailable)$/}).waitFor({state: 'attached'});
      await page.locator('#github-status-summary').filter({hasNotText: 'Loading'}).waitFor({state: 'attached'});
      let row = await footRow(page, '#codex-status-row');
      assert.equal(row.text, 'Codex · Ready');
      assert.equal(row.state, 'ready');
      assert.match((await footRow(page, '#remote-status-row')).text, /^Remote · (Ready|Off|Unavailable)$/, 'the machine name stays off the row');
      await page.evaluate(() => renderLogin({codex_login: {status_ok: false}}));
      row = await footRow(page, '#codex-status-row');
      assert.equal(row.text, 'Codex · Not signed in');
      assert.equal(row.dot, AMBER_FALLBACK, 'with no host window the amber dot uses the page fallback');
      await page.route('**/auth/status', route => route.fulfill({status: 500, json: {ok: false}}));
      await page.evaluate(() => loadLogin().catch(() => {}));
      assert.equal((await footRow(page, '#codex-status-row')).text, 'Codex · Unavailable');
      assert.equal((await footRow(page, '#codex-status-row')).state, 'error');
      await page.unroute('**/auth/status');
      await page.evaluate(() => loadLogin());
      for (const [id, part] of [['codex-status-row', 'chatgpt-section'], ['github-status-row', 'github-section'],
        ['remote-status-row', 'app-pairing-section']]) {
        await showSidebar(page, phone);
        await page.locator(`#${id}`).click();
        await page.locator('#settings[open]').waitFor();
        // Settings reads every part first, then opens the one asked for.
        await page.locator(`#${part} > .settings-body`).waitFor();
        const open = await page.evaluate(() => [...document.querySelectorAll('#accounts-section, #accounts-section > .settings-body > section')]
          .filter(section => section.querySelector(':scope > :is(h3, h4) > .settings-toggle')?.getAttribute('aria-expanded') === 'true')
          .map(section => section.id));
        assert.ok(open.includes('accounts-section') && open.includes(part), `${id} opens ${part} (${open})`);
        assert.equal(await page.locator(`#${part} > .settings-body`).isVisible(), true, `${part} shows`);
        await page.locator('#close-settings').click();
      }
      // No Status window is left: its Context and Usage fields had no source, and the quota shows below.
      assert.equal(await page.locator('#status-dialog').count(), 0);

      // 4. The plan line: plan type, weekly and 5-hour reset times as text; Unknown for a window not reported.
      const shown = await page.evaluate(() => {
        renderUsage({status: 'ok', weekly_percent: '32', weekly_reset_at: '2026-11-23T15:00:00Z',
          five_hour_percent: '80', five_hour_reset_at: '2026-11-17T19:30:00Z'}, 'pro');
        const both = document.getElementById('usage-plan').textContent;
        renderUsage({status: 'ok', weekly_percent: '32', weekly_reset_at: '2026-11-23T15:00:00Z'}, null);
        return [both, document.getElementById('usage-plan').textContent, document.getElementById('usage-five-hour').textContent];
      });
      assert.match(shown[0], /^Pro plan · Weekly resets \S+ .+ · 5h resets \S+ .+$/);
      assert.match(shown[1], /^Plan unknown · Weekly resets /);
      assert.equal(shown[2], 'Unknown');
      await page.evaluate(() => loadUsage());

      // 5. Manage remote connection in the Remote part opens the window that holds Get a pairing code.
      await showSidebar(page, phone);
      await page.locator('#remote-status-row').click();
      await page.locator('#remote-connection-open').click();
      await page.locator('#connection-dialog[open]').waitFor();
      assert.equal(await page.locator('#remote-control-pair').count(), 1);
      await page.locator('[data-close="connection-dialog"]').click();
      await showSidebar(page, phone);
      await page.screenshot({path: path.join(SHOTS, `foot-${theme}-${width}.png`)});
      if (phone) await page.locator('#close-sidebar').click();

      // 6. While a task runs, the send button is the stop button, and a draft or a file in the box keeps it there.
      await page.locator(phone ? '#phone-new' : '#new-chat').click();
      await page.locator('#message').fill('Take your time and list the synthetic porch lights.');
      await page.locator('#send').click();
      await page.locator('#cancel:not([hidden])').waitFor();
      assert.equal(await page.locator('#send').isHidden(), true, 'send hides while the stop button shows');
      assert.equal(await page.evaluate(() => document.getElementById('cancel').closest('#composer') !== null), true);
      const [stopBox, composeBox] = [await page.locator('#cancel').boundingBox(), await page.locator('#composer').boundingBox()];
      assert.ok(stopBox.x + stopBox.width <= composeBox.x + composeBox.width && stopBox.y >= composeBox.y, 'stop sits in the prompt box');
      await page.screenshot({path: path.join(SHOTS, `stop-${theme}-${width}.png`)});
      const stopShows = async (why) => {
        assert.equal(await page.locator('#cancel').isVisible(), true, `stop stays in view ${why}`);
        assert.equal(await page.locator('#cancel').isEnabled(), true, `stop stays usable ${why}`);
        assert.equal(await page.locator('#send').isHidden(), true, `send stays hidden ${why}`);
      };
      await page.locator('#message').fill('wait, not the synthetic porch');
      await stopShows('with a draft typed');
      await page.locator('#message').press('Enter');
      await stopShows('after Enter with a draft');
      await page.locator('#message').fill('');
      await page.locator('#file-input').setInputFiles({name: 'synthetic dot.png', mimeType: 'image/png',
        buffer: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg==', 'base64')});
      await page.locator('.pending-file').waitFor();
      await stopShows('with a file attached');
      await page.locator('#message').fill('wait, not the synthetic porch');
      const cancelled = page.waitForRequest(request => /\/tasks\/[^/]+\/cancel$/.test(request.url()));
      await page.locator('#cancel').click();
      await cancelled;
      await page.locator('#cancel[hidden]').waitFor({state: 'attached'});
      assert.equal(await page.locator('#send').isVisible(), true);
      await page.evaluate(() => fetch('fixture/release', {method: 'POST'}));

      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.deepEqual(errors, []);
      console.log(`PASS ${theme}/${width}: bands gone, foot rows in order, one Settings, MDI icons, Codex, GitHub and Remote rows open their parts, plan line, remote window, stop button`);
      await context.close();
    }

    // 7. Inside a host window that sets Home Assistant's warning color, the amber dot takes it.
    const hostContext = await browser.newContext({viewport: {width: 1280, height: 900}});
    const host = await hostContext.newPage();
    await host.route(base + 'band-host', route => route.fulfill({contentType: 'text/html', body:
      `<!doctype html><home-assistant style="--warning-color:#ff9800;--primary-color:#03a9f4"></home-assistant>` +
      `<iframe src="${base}" style="width:100%;height:860px"></iframe>`}));
    await host.goto(base + 'band-host');
    const frame = host.frames().find(item => item.url() === base);
    await frame.locator('.chat-row').first().waitFor();
    await frame.evaluate(() => renderLogin({codex_login: {status_ok: false}}));
    const hosted = await footRow(frame, '#codex-status-row');
    assert.equal(hosted.text, 'Codex · Not signed in');
    assert.equal(hosted.dot, 'rgb(255, 152, 0)', 'the amber dot follows the host warning color');
    console.log('PASS host window: the amber dot takes --warning-color');
    await hostContext.close();
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
