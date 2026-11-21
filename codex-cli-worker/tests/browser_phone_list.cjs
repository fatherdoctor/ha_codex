// The phone chat list: a tap on a chat keeps the side bar open, a tap on the page to its right closes it, the selected
// chat shows its menu button where its date was, the targets are 44 px or more, and a desktop window is unchanged.
// Run with tests/web_fixture.py serving the page. Every name is synthetic.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const errors = [];
    const phone = await browser.newPage({viewport: {width: 390, height: 844}});
    phone.on('pageerror', e => errors.push(e.message));
    await phone.goto('http://127.0.0.1:9137/preview/');
    await phone.locator('.chat-row').nth(1).waitFor({state: 'attached'});
    await phone.getByRole('button', {name: 'Open conversations', exact: true}).first().click();
    const open = () => phone.evaluate(() => document.body.classList.contains('sidebar-open'));
    assert.equal(await open(), true, 'the side bar opens');

    await phone.locator('.chat-row .row-main').nth(1).click();
    assert.equal(await open(), true, 'a tap on a chat keeps the side bar open');
    const state = await phone.evaluate(() => {
      const visible = el => getComputedStyle(el).visibility !== 'hidden';
      const rows = [...document.querySelectorAll('.chats-card .chat-row')];
      const current = rows.filter(r => r.classList.contains('current'));
      const others = rows.filter(r => !r.classList.contains('current') && r.querySelector('.row-meta > span:last-child'));
      const menu = current[0]?.querySelector('.row-menu');
      return {
        current: current.length,
        currentDate: current[0] ? visible(current[0].querySelector('.row-meta > span:last-child')) : null,
        otherDates: others.map(r => visible(r.querySelector('.row-meta > span:last-child'))),
        menuOpacity: menu ? getComputedStyle(menu).opacity : null,
        menuEvents: menu ? getComputedStyle(menu).pointerEvents : null,
        menuBox: menu ? menu.getBoundingClientRect().toJSON() : null,
        rowBoxes: rows.map(r => r.querySelector('.row-main').getBoundingClientRect().height),
      };
    });
    assert.equal(state.current, 1, 'one chat is selected');
    assert.equal(state.currentDate, false, 'the selected chat hides its date');
    assert.ok(state.otherDates.length && state.otherDates.every(Boolean), 'other chats keep their date');
    assert.equal(state.menuOpacity, '1', 'the selected chat shows its menu button');
    assert.equal(state.menuEvents, 'auto', 'the menu button takes taps');
    assert.ok(state.menuBox.width >= 44 && state.menuBox.height >= 44, 'the menu button is 44 px or more');
    assert.ok(state.rowBoxes.every(h => h >= 44), `every chat row is 44 px or more: ${state.rowBoxes}`);

    await phone.locator('.chat-row.current .row-menu').click();
    await phone.locator('#chat-menu').waitFor({state: 'visible'});
    await phone.keyboard.press('Escape');

    await phone.mouse.click(380, 600);
    assert.equal(await open(), false, 'a tap on the page right of the side bar closes it');
    assert.deepEqual(errors, []);

    const desktop = await browser.newPage({viewport: {width: 1280, height: 860}});
    await desktop.goto('http://127.0.0.1:9137/preview/');
    await desktop.locator('.chat-row').first().waitFor({state: 'attached'});
    const desk = await desktop.evaluate(() => {
      const menu = document.querySelector('.chats-card .chat-row.current .row-menu') || document.querySelector('.chats-card .row-menu');
      return menu ? getComputedStyle(menu).opacity : '0';
    });
    assert.equal(desk, '0', 'on a desktop the menu button still shows only on hover');
    console.log('phone chat list holds');
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e); process.exit(1); });
