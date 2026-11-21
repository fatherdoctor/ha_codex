// Check reply geometry with transcript-shaped examples kept apart from the shared chat fixture.
// Run tests/web_fixture.py first; set REPLY_LAYOUT_BASE and REPLY_LAYOUT_CAPTURE.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const fixtures = require('./fixtures/reply-layout/transcripts.json');
const output = process.env.REPLY_LAYOUT_CAPTURE;
const base = process.env.REPLY_LAYOUT_BASE;
if (!base) throw new Error('Set REPLY_LAYOUT_BASE to the local preview URL.');

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.REPLY_LAYOUT_CHROMIUM ? {executablePath: process.env.REPLY_LAYOUT_CHROMIUM} : {})});
  const failures = [];
  try {
    for (const width of [320, 390, 700, 1280]) {
      const page = await browser.newPage({viewport: {width, height: 2000}});
      await page.goto(base);
      await page.locator('[data-task-id="preview-00"]').evaluate(node => node.click());
      await page.locator('#activity').waitFor({state: 'attached'});
      for (const fixture of fixtures) {
        await page.evaluate(data => {
          clearTimeout(activityTimer);
          activityTimer = null;
          const turn = (id, message, summary) => ({
            turn_id: id, message, summary, status: 'completed',
            created_at: '2026-01-01T12:00:00Z', started_at: '2026-01-01T12:00:00Z', completed_at: '2026-01-01T12:00:09Z',
          });
          const turns = [];
          if (data.earlier) turns.push(turn('earlier', data.earlier.message, data.earlier.summary));
          turns.push(turn('fixture', data.prompt, data.answer));
          state.task = {...state.task, status: 'completed', turns};
          const steps = new Map([[0, {index: 0, kind: 'reasoning', status: 'done', text: 'Checking the request'}]]);
          data.interim.forEach((text, n) => steps.set(n + 1, {index: n + 1, kind: 'message', status: 'done', phase: 'commentary', text}));
          state.activity = {id: state.id, turnId: 'fixture', seq: steps.size, steps, running: false,
            startedAt: null, loaded: true, loading: false, expanded: false, open: new Set()};
          renderTask(true);
        }, fixture);
        const exchange = page.locator('#messages .exchange').last();
        const measured = await exchange.evaluate(node => {
          const area = node.closest('#messages');
          const rect = area.getBoundingClientRect();
          const columnLeft = rect.left + parseFloat(getComputedStyle(area).paddingLeft);
          const blocks = [...node.querySelectorAll('#activity-text > .message'), node.querySelector('.answer > .message')];
          const firstX = element => {
            const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT, {
              acceptNode: text => text.data.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP,
            });
            const text = walker.nextNode();
            const range = document.createRange();
            const start = text.data.search(/\S/);
            range.setStart(text, start); range.setEnd(text, start + 1);
            return range.getBoundingClientRect().left;
          };
          const boxes = blocks.map(block => {
            const bounds = block.getBoundingClientRect();
            return {left: firstX(block), top: bounds.top, bottom: bounds.bottom, right: bounds.right};
          });
          const paragraphs = [...node.querySelectorAll('.answer > .message > p')].map(p => {
            const box = p.getBoundingClientRect(); return {top: box.top, bottom: box.bottom};
          });
          return {columnLeft, viewportRight: Math.min(rect.right, innerWidth), boxes, paragraphs,
            metaLeft: firstX(node.querySelector('.turn-meta')),
            workedLeft: node.querySelector('.activity-toggle').getBoundingClientRect().left,
            exchanges: area.querySelectorAll('.exchange').length,
            scrolled: area.scrollLeft, wider: area.scrollWidth > area.clientWidth,
            tables: [...node.querySelectorAll('.md-table')].map(table => ({client: table.clientWidth, scroll: table.scrollWidth}))};
        });
        const label = `item ${fixture.id} at ${width}px`;
        const fail = message => failures.push(`${label}: ${message}`);
        measured.boxes.forEach((box, i) => {
          if (Math.abs(box.left - measured.columnLeft) > 1) fail(`block ${i} starts ${Math.round(box.left - measured.columnLeft)}px from the column edge`);
          if (box.right > measured.viewportRight + 1) fail(`block ${i} extends past the viewport`);
          if (i && box.top - measured.boxes[i - 1].bottom < 8) fail(`gap before block ${i} is under 8px`);
        });
        measured.paragraphs.forEach((p, i) => {
          if (i && p.top - measured.paragraphs[i - 1].bottom < 8) fail(`paragraph gap ${i} is under 8px`);
        });
        if (Math.abs(measured.metaLeft - measured.columnLeft) > 1) fail('completion line is indented');
        if (Math.abs(measured.workedLeft - measured.columnLeft) > 1) fail('work line is indented');
        if (fixture.earlier && measured.exchanges !== 2) fail('the earlier exchange is missing');
        if (measured.scrolled || measured.wider) fail('message column scrolls sideways');
        if (output) {
          fs.mkdirSync(output, {recursive: true});
          await exchange.screenshot({path: path.join(output, `${fixture.id}-${width}.png`), animations: 'disabled'});
        }
      }
      await page.close();
    }
  } finally {
    await browser.close();
  }
  if (failures.length) {
    console.error(failures.join('\n'));
    process.exitCode = 1;
  } else {
    assert.equal(fixtures.length, 5);
    console.log('Five reply fixtures checked at 320, 390, 700 and 1280px.');
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
