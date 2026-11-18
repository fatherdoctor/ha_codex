// Apply the saved text size before the page draws, so the page never jumps
// from one size to another while it loads. chat.js keeps it in step after.
(() => {
  try {
    const size = localStorage.getItem("codex-text-size");
    if (size === "larger" || size === "largest")
      document.documentElement.dataset.textSize = size;
  } catch (_) {
    /* Storage may be disabled: the page keeps the normal size. */
  }
})();
