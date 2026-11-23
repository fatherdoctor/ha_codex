# Show math in chat replies as typeset math

Design only. No code changes. Estimate: 0.5 point for this note; the build plan below is about 1.5 points.

## The problem

Codex writes equations in LaTeX between `\( ... \)` (in a sentence) or `\[ ... \]` and `$$ ... $$` (on their own
lines). Since 0.4.0 the chat page keeps these delimiters: an equation in a sentence shows as inline code, and a
display equation shows as a code block (`markdown.js`, the `inlineMath` and `displayMath` extensions). The text is
correct, but a person who asks for the quadratic formula sees `\[ x = \frac{-b \pm \sqrt{b^2-4ac}}{2a} \]` in a
monospace box. The ChatGPT desktop app shows a typeset fraction and square root. A newcomer expects that.

## Recommendation

Typeset each math span with a bundled copy of [KaTeX](https://katex.org/) (MIT), in its **MathML-only output
mode**. KaTeX turns the LaTeX into MathML elements, and the browser draws them with its own math support. This
mode needs no KaTeX style sheet and no font files: it is one JavaScript file in `web/assets/vendor/`, next to
marked. Codex is told nothing new: it already writes the delimiters that the page reads.

When KaTeX cannot read a formula, when the formula is very long, or when the browser has no MathML support, the
page shows the formula as it does today (inline code or a code block). So no reply looks worse than in 0.4.0.

## Options considered

| Option | For | Against |
|---|---|---|
| A. KaTeX, MathML-only output (recommended) | One script file; no fonts or style sheet; widely used and reviewed; options to turn off links and limit macro expansion; output follows the page text color and size, so light and dark themes work | Drawing quality depends on the browser's MathML support (current Chrome, Edge, Firefox and Safari have it; very old Android WebViews do not, and those fall back to code) |
| B. KaTeX, HTML and MathML output | The same look on every browser | Needs the KaTeX style sheet and about sixty font files in the image; more to vendor and to pin |
| C. Temml (MIT, a MathML-only fork of KaTeX) | Smaller than KaTeX | Fewer users and reviews; a second math library to watch if a later card wants HTML output |
| D. MathJax | The most complete LaTeX support | Much larger; more than this page needs |
| E. No renderer; tell Codex to write plain Unicode (x = (−b ± √(b² − 4ac)) / 2a) | No new code | Changes how Codex writes, which is the owner's choice in AGENTS.md, not the add-on's (parity rule); fractions and matrices read badly |

## How it works

1. **Find the spans.** Keep the two marked extensions that exist today. Give their tokens their own types
   (`inlineMath`, `displayMath`) and keep the formula without its delimiters in the token.
2. **Typeset.** For each token, call `katex.render(formula, element, options)` into a new `span` (inline) or `div`
   (display). `katex.render` builds DOM nodes; the build step checks that the vendored file does this without
   `innerHTML`, so the page keeps its rule that no message text becomes markup. Options:
   - `output: "mathml"`: MathML only, no fonts.
   - `displayMode`: true for `\[ \]` and `$$ $$`, false for `\( \)`.
   - `trust: false`: `\href`, `\url`, `\includegraphics`, `\htmlClass` and similar commands show as text and make
     no link, image or class. This keeps the image rule of `markdown.js` (a message never loads a remote image).
   - `maxExpand` at its default (1000) and `maxSize` at a few em, so a macro loop or a huge `\rule` stops at once.
   - `throwOnError: true`, caught: an error gives the fallback.
3. **Fallback.** Show the original text, delimiters kept, as inline code or a code block, exactly as 0.4.0 does.
   The fallback is used when KaTeX throws, when the formula is longer than 2,000 characters, or when the browser
   has no MathML support (`typeof MathMLElement === "undefined"`).
4. **Layout.** Display math is centered, at 1.1 times the text size, with horizontal scroll when it is wider than
   the message (phone width). Inline math keeps the line height of the text around it. MathML uses the current
   text color, so it follows the Home Assistant theme with no extra rule.
5. **Copy and share.** Share already writes the chat as Markdown from the saved text, so a shared chat keeps the
   LaTeX source. KaTeX puts the source in the MathML as `<annotation encoding="application/x-tex">`, so tools that
   read MathML can get it back. A copy by mouse selection gives the browser's text of the math, which can differ
   from the source. That is the same as the ChatGPT app; no copy button is added (answers have had none since 0.3.4).
6. **What Codex emits.** No change. Single dollar signs (`$x$`) are not read as math, because replies use them for
   money ("$5 and $10"). The starter AGENTS.md gets no new instruction.

## Failure cases

| What can go wrong | How the design catches it |
|---|---|
| A formula KaTeX does not support (an unknown command, a package Codex assumes) | `throwOnError` is caught; the span shows as code, as in 0.4.0 |
| A message tries to make a link or load an image through math (`\href`, `\includegraphics`) | `trust: false`; the command shows as text. A browser test checks that a math span contains no `a` or `img` element |
| A macro that expands without end, or a very large size, freezes the page | `maxExpand` and `maxSize` stop it; formulas over 2,000 characters are not typeset at all |
| Many formulas make a long reply slow to show | Each formula is short and KaTeX is fast; the whole message still sits under the 300 ms parse budget of `markdown.js`, and the browser test times a reply with 200 formulas |
| An old phone WebView has no MathML support | The `MathMLElement` check sends every span to the code fallback |
| Text that only looks like math (`\(` inside a code block, prices with `$`) | Code blocks and code spans are read before the math extensions, as today; single `$` is never math |
| A wide matrix or a long equation overflows on a phone | Display math scrolls sideways inside the message |
| Dark theme makes math unreadable | MathML draws in the current text color; the browser test checks both themes |
| The vendored file changes without review | The version and SHA-256 are pinned in `tests/test_fork_pins.py`, as the fork pins the Codex CLI; moving the pin is its own pull request |
| A KaTeX security fix comes out | The pin test names the version, so a reader can check it against KaTeX advisories; the update is a pin pull request. Nothing watches KaTeX releases today (nor marked); a watcher is out of scope here |

## Limits the add-on adds (parity rule)

None on Codex. The 2,000-character cap, `trust: false`, `maxExpand` and `maxSize` limit only how the page draws a
reply that Codex already wrote. They exist because the page shows untrusted text; the sandbox and AGENTS.md act on
what Codex does, not on what the page draws, so they cannot carry these. A formula over the cap still shows in full,
as code.

## Build plan

This is the step-2 card `CX-MATH` (vendored KaTeX). Each step is for one build run; the whole card is about 1.5
points.

| Step | Work | Points |
|---|---|---|
| 1 | Vendor the newest KaTeX release: `web/assets/vendor/katex.min.js` and its MIT license file; check the license, the size (target: under 300 KB uncompressed) and that `katex.render` builds nodes with no `innerHTML` in the MathML path; pin version and SHA-256 in `tests/test_fork_pins.py`; load it with `defer` next to marked | 0.4 |
| 2 | `markdown.js`: token types `inlineMath` and `displayMath`, the render call with the options above, the fallback, the 2,000-character cap and the `MathMLElement` check | 0.5 |
| 3 | `chat.css`: display math centered, 1.1 em, horizontal scroll; inline math line height | 0.1 |
| 4 | Browser check `tests/browser_math.cjs` on the fixture: the quadratic formula gives a `math` element (inline and display); `\href{https://example.com}{x}` gives no `a`; an unknown command and a 2,001-character formula fall back to code; a phone-width page scrolls a wide matrix; dark theme; 200 formulas under the budget. Each check fails on the code of today | 0.4 |
| 5 | `DOCS.md` (what math looks like, the fallback) and a changelog line under the unreleased version | 0.1 |
