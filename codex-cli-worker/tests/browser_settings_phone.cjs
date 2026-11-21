// Settings on a phone (400 px) in light and dark: collapsed sections with a status line, button rows that fit,
// and the ChatGPT code countdown. On a phone and a desktop: one window size, one arrow size, even spacing. The Home
// Assistant look (CX-FB2-THEME): inside a host window that sets theme variables, the fields, buttons and font
// take them; with no host window, each part falls back to the page's own palette. Run with tests/web_fixture.py
// serving the page. Every code, name and color is synthetic.
// Screenshots go to SETTINGS_SHOTS (default /tmp/codex-settings-qa).
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const SHOTS = process.env.SETTINGS_SHOTS || '/tmp/codex-settings-qa';
const CODE = 'TEST-0000';
const github = {
  state: 'ok', kind: 'app', account: {login: 'synthetic-owner'}, client_id_source: 'built_in',
  repositories: [{full_name: 'synthetic-owner/house', private: true, push: true}],
  expected_repositories: ['synthetic-owner/house'], missing: [], extra: [],
  permissions: {contents: 'write', metadata: 'read'}, permission_problem: null, expires_at: null,
  connected_at: '2026-11-16T12:00:00Z', last_check: {at: '2026-11-16T13:00:00Z', state: 'ok', reason: ''},
  last_test: null, revoke_url: 'https://github.com/settings/apps/authorizations',
  flow: {status: 'idle', user_code: '', verification_uri: '', expires_at: null, error: ''},
};
const waiting = {
  status: 'waiting_for_user', pending: true, process_running: true, login_id: 'L1', user_code: CODE,
  verification_url: 'https://auth.openai.com/codex/device', qr_path: '', expires_in: 900,
  expires_at: '2026-11-16T13:15:00Z', codex_login: {status_ok: false, has_auth_file: false, message: ''},
};
const failed = {
  ...waiting, status: 'failed', pending: false, process_running: false, user_code: '', expires_in: null,
  error: 'The code expired before the sign-in was approved. Get a new code and enter it within its time.',
};

/** Every shown button in a settings button row fits the dialog, is not taller than wide, and matches its row. */
async function checkButtonRows(page) {
  const rows = await page.evaluate(() => {
    const dialog = document.querySelector('#settings').getBoundingClientRect();
    return [...document.querySelectorAll('#settings .settings-actions')].filter(row => row.offsetParent).map(row => ({
      id: row.closest('section')?.id || '',
      dialogRight: dialog.right,
      buttons: [...row.querySelectorAll(':scope > button')].filter(b => b.offsetParent).map(b => {
        const r = b.getBoundingClientRect();
        return {text: b.textContent, left: r.left, right: r.right, top: Math.round(r.top), width: r.width, height: r.height};
      }),
    }));
  });
  assert.ok(rows.some(row => row.buttons.length >= 4), 'the GitHub row with four buttons is checked');
  for (const row of rows) {
    const byLine = new Map();
    for (const button of row.buttons) {
      assert.ok(button.right <= row.dialogRight + 0.5, `${row.id} "${button.text}" runs past the panel edge`);
      assert.ok(button.width >= button.height, `${row.id} "${button.text}" is taller than wide`);
      byLine.set(button.top, [...(byLine.get(button.top) || []), button.width]);
    }
    for (const widths of byLine.values())
      assert.ok(Math.max(...widths) - Math.min(...widths) <= 1, `${row.id} buttons in one line differ: ${widths}`);
  }
}

/** The settings window's size, to compare before and after a section opens. */
const windowSize = page => page.evaluate(() => {
  const r = document.querySelector('#settings').getBoundingClientRect();
  return [Math.round(r.width), Math.round(r.height)];
});

/**
 * Every arrow has one size, and the space from each divider line to the header under it is the same. A closed
 * header has that same space down to the next divider; an open section's last element has 16 px. Sizes are in
 * CSS pixels, to 1 px.
 */
async function checkLayout(page) {
  const layout = await page.evaluate(() => {
    const arrows = [...document.querySelectorAll('#settings .settings-title')].filter(t => t.offsetParent).map(t => {
      const style = getComputedStyle(t, '::before');
      // One Material Design Icons arrow: the same 16 px box, drawn from an SVG mask.
      return `${style.width} ${style.height} ${style.maskImage.includes('svg') || style.webkitMaskImage.includes('svg')}`;
    });
    const sections = [...document.querySelectorAll('#settings section')].filter(s => s.offsetParent);
    // A divider is a section's top border; the last section also has one at its bottom.
    const dividers = [...sections.map(s => s.getBoundingClientRect().top), sections.at(-1).getBoundingClientRect().bottom];
    const above = [];
    const closed = [];
    const below = [];
    for (const section of sections) {
      const toggle = section.querySelector(':scope > :is(h3, h4) > .settings-toggle');
      if (!toggle) continue;
      const header = toggle.getBoundingClientRect();
      above.push(Math.round(header.top - section.getBoundingClientRect().top));
      const body = section.querySelector(':scope > .settings-body');
      if (body.hidden) {
        closed.push(Math.round(Math.min(...dividers.filter(d => d >= header.bottom - 0.5)) - header.bottom));
        continue;
      }
      if (section.id === 'accounts-section') continue;
      const last = [...body.children].filter(c => c.offsetParent && c.getBoundingClientRect().height).at(-1);
      // Space from the last thing in the open section to the section's end, where the next divider is.
      below.push([section.id, Math.round(section.getBoundingClientRect().bottom - last.getBoundingClientRect().bottom)]);
    }
    // Every visible <details> in Settings and Scheduled runs uses the same arrow, not the browser's triangle.
    for (const summary of document.querySelectorAll('#settings details > summary')) {
      if (!summary.offsetParent) continue;
      const style = getComputedStyle(summary, '::before');
      arrows.push(`${style.width} ${style.height} ${style.maskImage.includes('svg') || style.webkitMaskImage.includes('svg')}`);
      if (getComputedStyle(summary).listStyleType !== 'none') arrows.push(`triangle on ${summary.textContent}`);
    }
    return {arrows, above, closed, below};
  });
  assert.equal(new Set(layout.arrows).size, 1, `one arrow size: ${layout.arrows}`);
  assert.ok(layout.arrows.every(arrow => arrow.endsWith(' true')), `every arrow is an SVG icon: ${layout.arrows}`);
  assert.ok(layout.above.every(gap => Math.abs(gap - layout.above[0]) <= 1), `divider to header: ${layout.above}`);
  assert.ok(layout.closed.every(gap => Math.abs(gap - layout.above[0]) <= 1),
    `closed header to the next divider: ${layout.closed}, divider to header: ${layout.above[0]}`);
  for (const [id, gap] of layout.below) assert.ok(Math.abs(gap - 16) <= 1, `${id}: ${gap} px above the next divider`);
}

/** The computed look of one field, one settings row, the two kinds of button, a close icon and the page font. */
const themeLook = target => target.evaluate(() => {
  const css = (selector, property, pseudo) => {
    const element = document.querySelector(selector);
    return element ? getComputedStyle(element, pseudo).getPropertyValue(property).trim() : `missing ${selector}`;
  };
  return {
    font: getComputedStyle(document.body).fontFamily,
    // A list field takes the same font as the page (the model list of the schedule editor).
    selectFont: css('#schedule-model', 'font-family'),
    // A text field and a text button of the schedule editor.
    fieldFill: css('#schedule-name', 'background-color'),
    fieldLine: css('#schedule-name', 'border-bottom-color'),
    // The Add-on configuration row at the foot of Settings: a name and a description line.
    rowHeight: document.querySelector('.ha-row').getBoundingClientRect().height,
    // The form's submit button, the filled kind next to the text button below.
    filled: css('#schedule-form button[type=submit]', 'background-color'),
    filledText: css('#schedule-form button[type=submit]', 'color'),
    textButton: css('#schedule-delete', 'color'),
    textButtonFill: css('#schedule-delete', 'background-color'),
    // Every close button of Settings and its dialogs is the mdiClose icon, with no text glyph.
    closeIcons: [...document.querySelectorAll('#close-settings, dialog.studio-sheet .dialog-header .icon')].map(b =>
      `${b.textContent.trim() === ''} ${getComputedStyle(b, '::before').maskImage.includes('svg') || getComputedStyle(b, '::before').webkitMaskImage.includes('svg')}`),
  };
});

/** Open the sections that hold a field and both kinds of button. */
async function openThemedSections(target) {
  for (const id of ['scheduled-work'])
    await target.locator(`#${id} > h3 .settings-toggle`).click();
}

(async () => {
  fs.mkdirSync(SHOTS, {recursive: true});
  const browser = await chromium.launch({headless: true});
  try {
    for (const colorScheme of ['light', 'dark']) {
      const page = await browser.newPage({viewport: {width: 400, height: 860}, colorScheme});
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      let auth = waiting;
      const posts = [];
      await page.route('**/auth/status', route => route.fulfill({json: {ok: true, auth}}));
      await page.route(/\/auth\/(cancel|start)$/, route => {
        posts.push(new URL(route.request().url()).pathname.replace(/^.*\/auth\//, ''));
        return route.fulfill({json: {ok: true, auth}});
      });
      await page.route('**/github', route => route.fulfill({json: {ok: true, github}}));
      const openSettings = async () => {
        const conversations = page.getByRole('button', {name: 'Open conversations', exact: true});
        if (await conversations.isVisible()) await conversations.click();
        await page.getByRole('button', {name: 'Open settings', exact: true}).click();
      };
      await page.goto('http://127.0.0.1:9137/preview/');
      await openSettings();
      // Each section fills its header status when its data arrives.
      await page.waitForFunction(() => [...document.querySelectorAll('#settings .settings-status')].every(s => s.textContent !== ''));

      // Every section starts collapsed, with its title and status in the header.
      const headers = await page.evaluate(() => [...document.querySelectorAll('#settings .settings-toggle')].map(t => ({
        title: t.querySelector('.settings-title').textContent, status: t.querySelector('.settings-status').textContent,
        open: t.getAttribute('aria-expanded'), attention: t.closest('section').classList.contains('needs-attention')})));
      assert.ok(headers.every(h => h.open === 'false'), 'every section starts collapsed');
      // Every header has a status. The fixture has no Home Assistant, so the schedules wait for its time zone.
      // Accounts and connections names only the part that needs the owner: ChatGPT waits for its code.
      assert.deepEqual(headers.map(({title, status, attention}) => ({title, status, attention})), [
        {title: 'Accounts and connections', status: 'ChatGPT signing in: enter the code', attention: true},
        {title: 'ChatGPT account', status: 'Signing in: enter the code', attention: true},
        {title: 'GitHub', status: 'Connected as synthetic-owner, 1 repository', attention: false},
        {title: 'ChatGPT app pairing', status: 'Off', attention: false},
        {title: 'Full access', status: 'Off', attention: false},
        {title: 'Text size', status: 'Normal', attention: false},
        {title: 'Scheduled runs', status: 'Paused: time zone unavailable', attention: true},
        {title: 'Workspace instructions', status: 'AGENTS.md', attention: false},
        {title: 'Advanced', status: 'Browser permissions: none remembered', attention: false},
        {title: 'Browser permissions', status: 'None remembered', attention: false},
      ]);
      assert.equal(await page.locator('#remote-control-section .settings-toggle').count(), 0, 'own dialog stays open');
      await page.screenshot({path: path.join(SHOTS, `settings-collapsed-${colorScheme}-400.png`)});

      const collapsedSize = await windowSize(page);
      await checkLayout(page);

      // Open Accounts and connections, then its three parts: the code shows its countdown, and the button rows
      // fit the phone. The app pairing part has its own collapsed header, like the other two.
      await page.locator('#accounts-section > h3 .settings-toggle').click();
      assert.equal(await page.getByRole('button', {name: 'Manage remote connection', exact: true}).isVisible(), false);
      for (const id of ['chatgpt-section', 'github-section', 'app-pairing-section'])
        await page.locator(`#${id} .settings-toggle`).click();
      assert.equal(await page.getByRole('button', {name: 'Manage remote connection', exact: true}).isVisible(), true);
      // The window keeps its size and scrolls inside.
      assert.deepEqual(await windowSize(page), collapsedSize);
      assert.equal(await page.locator('#settings').evaluate(d => d.scrollHeight > d.clientHeight), true);
      await checkLayout(page);
      const expiry = page.locator('#login-card .signin-expiry');
      assert.match(await expiry.textContent(), /^The code expires in 1[45]:\d\d\.$/);
      // A later status reply sets the time again, as when a phone wakes from sleep with the page's count behind.
      auth = {...waiting, expires_in: 300};
      await page.waitForFunction(() => /^The code expires in [45]:\d\d\.$/.test(
        document.querySelector('#login-card .signin-expiry').textContent), null, {timeout: 6000});
      auth = waiting;
      await checkButtonRows(page);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.equal(await page.evaluate(() => {
        const dialog = document.querySelector('#settings');
        return dialog.scrollWidth <= dialog.clientWidth;
      }), true, 'nothing scrolls sideways in the settings panel');
      await page.screenshot({path: path.join(SHOTS, `settings-open-${colorScheme}-400.png`), fullPage: true});
      await page.locator('#github-section').screenshot({path: path.join(SHOTS, `settings-github-${colorScheme}-400.png`)});
      await page.locator('#chatgpt-section').screenshot({path: path.join(SHOTS, `settings-chatgpt-${colorScheme}-400.png`)});
      await page.locator('#accounts-section').screenshot({path: path.join(SHOTS, `settings-accounts-${colorScheme}-400.png`)});

      // A failed sign-in says why in the section and its header, and the button offers a new code.
      auth = failed;
      await page.locator('#close-settings').click();
      await openSettings();
      await page.waitForFunction(() => document.querySelector('#chatgpt-section .settings-status').textContent === 'Sign-in failed');
      assert.match(await page.locator('#login-status').textContent(), /did not finish\. The code expired before/);
      assert.equal(await page.locator('#login').textContent(), 'Get a new code');
      await checkButtonRows(page);
      await page.locator('#chatgpt-section').screenshot({path: path.join(SHOTS, `settings-chatgpt-failed-${colorScheme}-400.png`)});
      assert.deepEqual(errors, []);
      await page.close();
    }

    // A desktop window: one size whatever opens, one arrow size, even spacing, in light and dark.
    for (const colorScheme of ['light', 'dark']) {
      const page = await browser.newPage({viewport: {width: 1280, height: 860}, colorScheme});
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      await page.route('**/github', route => route.fulfill({json: {ok: true, github}}));
      await page.goto('http://127.0.0.1:9137/preview/');
      await page.getByRole('button', {name: 'Open settings', exact: true}).click();
      await page.waitForFunction(() => [...document.querySelectorAll('#settings .settings-status')].every(s => s.textContent !== ''));
      const size = await windowSize(page);
      await checkLayout(page);
      await page.screenshot({path: path.join(SHOTS, `settings-collapsed-${colorScheme}-1280.png`)});
      for (const id of ['accounts-section', 'app-pairing-section', 'workspace-section']) {
        await page.locator(`#${id} > :is(h3, h4) .settings-toggle`).click();
        assert.deepEqual(await windowSize(page), size, `the window keeps its size after ${id} opens`);
      }
      await checkLayout(page);
      await page.screenshot({path: path.join(SHOTS, `settings-open-${colorScheme}-1280.png`)});
      assert.deepEqual(errors, []);
      await page.close();
    }

    // The Home Assistant look with no host window: the page's own palette, never an empty or transparent part.
    for (const colorScheme of ['light', 'dark']) {
      const page = await browser.newPage({viewport: {width: 1280, height: 860}, colorScheme});
      const errors = []; page.on('pageerror', e => errors.push(e.message));
      await page.goto('http://127.0.0.1:9137/preview/');
      await page.getByRole('button', {name: 'Open settings', exact: true}).click();
      await openThemedSections(page);
      const look = await themeLook(page);
      assert.match(look.font, /^Inter,/, 'the page font with no Home Assistant font');
      assert.equal(look.selectFont, look.font, 'a list field takes the page font');
      assert.ok(look.rowHeight >= 72, `a two-line settings row is 72 px or more: ${look.rowHeight}`);
      assert.equal(look.filled, 'rgb(0, 154, 199)', 'the filled button takes the page accent');
      assert.equal(look.filledText, 'rgb(255, 255, 255)');
      assert.equal(look.textButton, 'rgb(0, 154, 199)', 'a text button is in the accent color');
      assert.equal(look.textButtonFill, 'rgba(0, 0, 0, 0)', 'a text button has no fill');
      for (const part of ['fieldFill', 'fieldLine'])
        assert.ok(!['', 'rgba(0, 0, 0, 0)'].includes(look[part]), `${part} has a color: ${look[part]}`);
      assert.ok(look.closeIcons.length >= 4 && look.closeIcons.every(icon => icon === 'true true'), `close icons: ${look.closeIcons}`);
      await page.locator('#scheduled-work').screenshot({path: path.join(SHOTS, `theme-standalone-${colorScheme}-1280.png`)});
      assert.deepEqual(errors, []);
      await page.close();
    }

    // Inside a host window that sets Home Assistant's theme variables, the same parts take them, on a phone and a
    // desktop; a variable the next theme drops goes back to the page's own value.
    for (const width of [400, 1280]) {
      const host = await browser.newPage({viewport: {width, height: 860}});
      const errors = []; host.on('pageerror', e => errors.push(e.message));
      const variables = '--primary-color:#6344ff;--primary-background-color:#18212a;--card-background-color:#223344;'
        + '--primary-text-color:#ddeeff;--secondary-text-color:#99aabb;--divider-color:#334455;'
        + '--ha-font-family-body:Synthetic Sans,sans-serif;--input-fill-color:#2a3b4c;--input-idle-line-color:#8899aa;'
        + '--ha-color-fill-primary-loud-resting:#5522ee;--ha-color-on-primary-loud:#fefefe';
      await host.route('http://127.0.0.1:9137/preview/theme-host', route => route.fulfill({contentType: 'text/html', body:
        `<!doctype html><home-assistant style="${variables}"></home-assistant>`
        + `<script>document.querySelector('home-assistant').hass={themes:{darkMode:true}};<\/script>`
        + '<iframe src="http://127.0.0.1:9137/preview/" style="width:100%;height:840px;border:0"></iframe>'}));
      await host.goto('http://127.0.0.1:9137/preview/theme-host');
      const frame = host.frameLocator('iframe');
      const page = host.frames().find(f => f.url() === 'http://127.0.0.1:9137/preview/');
      await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark');
      const conversations = frame.getByRole('button', {name: 'Open conversations', exact: true});
      if (await conversations.isVisible()) await conversations.click();
      await frame.getByRole('button', {name: 'Open settings', exact: true}).click();
      await openThemedSections(page);
      const look = await themeLook(page);
      assert.match(look.font, /^"?Synthetic Sans"?, sans-serif$/, `Home Assistant's font: ${look.font}`);
      assert.equal(look.selectFont, look.font, `a list field takes Home Assistant's font: ${look.selectFont}`);
      assert.equal(look.fieldFill, 'rgb(42, 59, 76)', 'the field fill is --input-fill-color');
      assert.equal(look.fieldLine, 'rgb(136, 153, 170)', 'the field line is --input-idle-line-color');
      assert.equal(look.filled, 'rgb(85, 34, 238)', 'the filled button is --ha-color-fill-primary-loud-resting');
      assert.equal(look.filledText, 'rgb(254, 254, 254)');
      assert.equal(look.textButton, 'rgb(99, 68, 255)', 'a text button is --primary-color');
      assert.ok(look.closeIcons.every(icon => icon === 'true true'), `close icons: ${look.closeIcons}`);
      await page.locator('#settings').screenshot({path: path.join(SHOTS, `theme-host-${width}.png`)});
      // The next theme has no field fill and no font: those parts go back to the page's own values.
      await host.evaluate(() => {
        const element = document.querySelector('home-assistant');
        element.style.removeProperty('--input-fill-color');
        element.style.removeProperty('--ha-font-family-body');
      });
      await page.waitForFunction(() => !document.documentElement.style.getPropertyValue('--input-fill-color'));
      const after = await themeLook(page);
      assert.match(after.font, /^Inter,/);
      assert.notEqual(after.fieldFill, 'rgb(42, 59, 76)');
      assert.deepEqual(errors, []);
      await host.close();
    }

    // An expired ChatGPT code says so and Get a new code cancels the old sign-in and starts a new one.
    const page = await browser.newPage({viewport: {width: 400, height: 860}});
    const posts = [];
    let auth = {...waiting, expires_in: 1};
    await page.route('**/auth/status', route => route.fulfill({json: {ok: true, auth}}));
    await page.route(/\/auth\/(cancel|start)$/, route => {
      posts.push(new URL(route.request().url()).pathname.replace(/^.*\/auth\//, ''));
      // A new sign-in has its own id and code.
      if (posts.at(-1) === 'start') auth = {...waiting, login_id: 'L2', user_code: 'TEST-0001'};
      return route.fulfill({json: {ok: true, auth}});
    });
    await page.route('**/github', route => route.fulfill({json: {ok: true, github}}));
    await page.goto('http://127.0.0.1:9137/preview/');
    const banner = page.locator('#signin-banner');
    await banner.getByRole('button', {name: 'Get a new code', exact: true}).waitFor({state: 'visible', timeout: 5000});
    assert.equal(await banner.locator('.signin-expiry').textContent(), 'This code has expired. Get a new code to sign in.');
    await banner.getByRole('button', {name: 'Get a new code', exact: true}).click();
    await page.waitForFunction(() => /expires in/.test(document.querySelector('#signin-banner .signin-expiry')?.textContent || ''));
    assert.deepEqual(posts, ['cancel', 'start']);
    await page.close();
    // The sign-in cards on smaller phones: they scroll to the link, and the QR code starts folded.
    await require('./browser_signin_phone.cjs')(browser);
    // The signing-in state of those cards shows the link, the code and Cancel only.
    await require('./browser_signin_state.cjs')(browser);
    console.log(`PASS: settings at 400 px in light and dark: collapsed sections with status headers, button rows that fit, code countdown, expiry and a new code; at 400 and 1280 px one window size, one arrow size and even spacing; the Home Assistant look with and without a host theme, and a dropped theme variable falls back. Screenshots: ${SHOTS}`);
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
