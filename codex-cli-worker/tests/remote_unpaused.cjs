// Remote Control is hidden in this version: the worker marks the chat page's root (server.mark_remote_paused) and the
// page hides every remote part. The browser checks of those parts, as they work when Remote Control returns, call
// `await unpauseRemote(page)` before `page.goto` to load the page without the mark (as remote_unpaused.py does for
// the Python tests). browser_remote_paused.cjs checks the marked page.
async function unpauseRemote(page) {
  await page.route(url => url.pathname.endsWith('/preview/'), async route => {
    const response = await route.fetch();
    await route.fulfill({response, body: (await response.text()).replace(' data-remote-paused', '')});
  });
}
module.exports = {unpauseRemote};
