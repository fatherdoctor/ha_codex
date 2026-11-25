# Show math in chat replies as typeset math

Design only. No code changes. Estimate: 0.5 point for this note, 0.3 point for its second round; the build plan
below is about 1.7 points.

## The problem

Codex writes equations in LaTeX between `\( ... \)` (in a sentence) or `\[ ... \]` and `$$ ... $$` (on their own
lines). Since 0.4.0 the chat page keeps these delimiters: an equation in a sentence shows as inline code, and a
display equation shows as a code block (`markdown.js`, the `inlineMath` and `displayMath` extensions). The text is
correct, but a person who asks for the quadratic formula sees `\[ x = \frac{-b \pm \sqrt{b^2-4ac}}{2a} \]` in a
monospace box. The ChatGPT desktop app shows a typeset fraction and square root. A newcomer expects that.

## Recommendation

Typeset each math span with a bundled copy of [KaTeX](https://katex.org/) (MIT), in its **MathML-only output
mode**, and bundle **one math font**, [Noto Sans Math](https://fonts.google.com/noto/specimen/Noto+Sans+Math)
(SIL Open Font License 1.1), for the `math` element. KaTeX turns the LaTeX into MathML elements, and the browser
draws them with its own math support. This mode needs no KaTeX style sheet and none of the KaTeX fonts: it is one
JavaScript file and one font file in `web/assets/vendor/`, next to marked. Codex is told nothing new: it already
writes the delimiters that the page reads.

The font is needed because a browser draws a stretchy symbol (the parentheses of `\left( ... \right)` or of a
matrix, a large root sign) from the OpenType MATH table of the font. Without a math font, those symbols stay the
height of plain text: in a Chromium test with no math font, a `\left(` around a fraction was 16 px high while the
fraction was 27 px; with Noto Sans Math it was 43 px. Desktop Windows, macOS and iOS ship a math font, but no one
has shown which font an Android WebView (the Home Assistant app) uses. One bundled font gives every device the same
result. The browser downloads it only when a reply contains math, and then keeps it in its cache.

When KaTeX cannot read a formula, when the formula is very long, when typesetting runs out of time, or when the
browser has no MathML support, the page shows the formula as it does today (inline code or a code block). So no
reply looks worse than in 0.4.0.

## Options considered

| Option | For | Against |
|---|---|---|
| A. KaTeX, MathML-only output, plus one math font (recommended) | One script file and one font file; widely used and reviewed; options to turn off links and limit macro expansion; output follows the page text color and size, so light and dark themes work | Drawing quality depends on the browser's MathML support (current Chrome, Edge, Firefox and Safari have it; very old Android WebViews do not, and those fall back to code); about 270 KB of font to vendor and pin |
| A0. Option A with no font | One file less | Brackets and large roots do not stretch on a device with no math font (see above), and no check of the browser can tell: the `math` element exists, but the drawing is poor |
| B. KaTeX, HTML and MathML output | The same look on every browser | Needs the KaTeX style sheet and about sixty font files in the image; more to vendor and to pin |
| C. Temml (MIT, a MathML-only fork of KaTeX) | Smaller than KaTeX | Fewer users and reviews; a second math library to watch if a later card wants HTML output; needs the same math font as option A |
| D. MathJax | The most complete LaTeX support | Much larger; more than this page needs |
| E. No renderer; tell Codex to write plain Unicode (x = (−b ± √(b² − 4ac)) / 2a) | No new code | Changes how Codex writes, which is the owner's choice in AGENTS.md, not the add-on's (parity rule); fractions and matrices read badly |

## How it works

1. **Find the spans.** Keep the two marked extensions that exist today. Give their tokens their own types
   (`inlineMath`, `displayMath`) and keep the formula without its delimiters in the token.
2. **Typeset.** For each token, call `katex.render(formula, element, options)` into a new `span` (inline) or `div`
   (display). `katex.render` builds DOM nodes; the build step checks that the vendored file does this without
   `innerHTML`, so the page keeps its rule that no message text becomes markup. Options:
   - `output: "mathml"`: MathML only, no KaTeX style sheet or fonts.
   - `displayMode`: true for `\[ \]` and `$$ $$`, false for `\( \)`.
   - `trust: false`: `\href`, `\url`, `\includegraphics`, `\htmlClass` and similar commands make no link, image or
     class. This keeps the image rule of `markdown.js` (a message never loads a remote image). KaTeX shows such a
     command as its source text in its error color, which is red by default; see `errorColor`.
   - `errorColor: "currentColor"`: the source text of a refused command shows in the color of the text around it,
     not in red. A refused command is not an error the reader must act on.
   - `maxExpand` at its default (1000) and `maxSize` at a few em, so a macro loop or a huge `\rule` stops at once.
   - `throwOnError: true`, caught: an error gives the fallback.
3. **Time limit.** The 300 ms budget of `markdown.js` (`MARKDOWN_BUDGET_MS`) covers only the marked lexer in
   `markdownTokens`; the typesetting runs after it, when the elements are built. So the typesetting gets its own
   limit: one deadline of 300 ms per message for all its formulas. Each formula starts only while time is left; a
   formula that would start after the deadline takes the fallback. The page redraws the chat whenever it changes,
   and the token cache does not keep built elements. So a second cache keeps the typeset element of each formula
   (keyed by the formula and the display mode, at most 500 entries, cleared when full, as the token cache is), and a
   redraw inserts a copy of the cached element (`cloneNode(true)`) instead of typesetting again. A formula that fell
   back is cached as a fallback too. In the closer's Chromium test the first render took 190 ms (the cold start of
   the script) and 200 renders after it took 21 ms; the browser check of step 4 times both.
4. **Fallback.** Show the original text, delimiters kept, as inline code or a code block, exactly as 0.4.0 does.
   The fallback is used when KaTeX throws, when the formula is longer than 2,000 characters, when the time limit of
   item 3 is used up, or when the browser has no MathML support (`typeof MathMLElement === "undefined"`).
5. **Font and layout.** `chat.css` declares the bundled font with `@font-face` (family name `Codex Math`, `src`
   the vendored woff2, `font-display: swap`) and sets `math { font-family: "Codex Math", math; }`. The bundled
   font comes first, so every device draws math with the same font; the generic `math` family is only a fallback
   for the moment before the font loads, or if it fails to load. Display math is centered, at 1.1 times the text
   size, with horizontal scroll when it is wider than the message (phone width). Inline math keeps the line height
   of the text around it. MathML uses the current text color, so it follows the Home Assistant theme with no extra
   rule.
6. **Copy and share.** Share already writes the chat as Markdown from the saved text, so a shared chat keeps the
   LaTeX source. KaTeX puts the source in the MathML as `<annotation encoding="application/x-tex">`, so tools that
   read MathML can get it back. A copy by mouse selection gives the browser's text of the math, which can differ
   from the source. That is the same as the ChatGPT app; no copy button is added (answers have had none since 0.3.4).
7. **What Codex emits.** No change. Single dollar signs (`$x$`) are not read as math, because replies use them for
   money ("$5 and $10"). The starter AGENTS.md gets no new instruction.

## Failure cases

| What can go wrong | How the design catches it |
|---|---|
| A formula KaTeX does not support (an unknown command, a package Codex assumes) | `throwOnError` is caught; the span shows as code, as in 0.4.0 |
| A message tries to make a link or load an image through math (`\href`, `\includegraphics`) | `trust: false`; the command shows as text in the text color (`errorColor`). A browser test checks that a math span contains no `a` or `img` element |
| A macro that expands without end, or a very large size, freezes the page | `maxExpand` and `maxSize` stop it; formulas over 2,000 characters are not typeset at all |
| Many formulas make a long reply slow to show | The typesetting has its own 300 ms limit per message (the lexer budget does not cover it); formulas after the limit fall back to code; the element cache stops a redraw from typesetting again. The browser test times a first render and a reply with 200 formulas |
| The device has no math font, so brackets and roots do not stretch | The bundled Noto Sans Math is the first font of the `math` element. The browser test checks that a `\left(` around a fraction is at least as high as the fraction, and that check fails without the `@font-face` rule |
| The bundled font fails to load (an old cache, a slow link) | `font-display: swap` shows the math at once in the generic `math` font; the formula stays readable, only the stretchy symbols are small until the font arrives |
| An old phone WebView has no MathML support | The `MathMLElement` check sends every span to the code fallback |
| Text that only looks like math (`\(` inside a code block, prices with `$`) | Code blocks and code spans are read before the math extensions, as today; single `$` is never math |
| A wide matrix or a long equation overflows on a phone | Display math scrolls sideways inside the message |
| Dark theme makes math unreadable | MathML draws in the current text color; the browser test checks both themes |
| A vendored file changes without review | The version and SHA-256 of `katex.min.js` and of the font are pinned in `tests/test_fork_pins.py`, as the fork pins the Codex CLI; moving a pin is its own pull request |
| A KaTeX security fix comes out | The pin test names the version, so a reader can check it against KaTeX advisories; the update is a pin pull request. Nothing watches KaTeX releases today (nor marked); a watcher is out of scope here |

## Limits the add-on adds (parity rule)

None on Codex. The 2,000-character cap, the 300 ms typesetting limit, `trust: false`, `maxExpand` and `maxSize`
limit only how the page draws a reply that Codex already wrote. They exist because the page shows untrusted text;
the sandbox and AGENTS.md act on what Codex does, not on what the page draws, so they cannot carry these. A formula
over a limit still shows in full, as code.

## Build plan

This is the step-2 card `CX-MATH` (vendored KaTeX). Each step is for one build run; the whole card is about 1.7
points.

| Step | Work | Points |
|---|---|---|
| 1 | Vendor the newest KaTeX release: `web/assets/vendor/katex.min.js` and its MIT license file; check the license, the size (target: under 300 KB uncompressed) and that `katex.render` builds nodes with no `innerHTML` in the MathML path. Vendor Noto Sans Math as one woff2 (about 270 KB) with its OFL 1.1 license file, unchanged. Pin the version and SHA-256 of both files in `tests/test_fork_pins.py`; load KaTeX with `defer` next to marked | 0.5 |
| 2 | `markdown.js`: token types `inlineMath` and `displayMath`, the render call with the options above, the fallback, the 2,000-character cap, the 300 ms typesetting limit per message, the element cache and the `MathMLElement` check | 0.6 |
| 3 | `chat.css`: the `@font-face` rule and `math` font family; display math centered, 1.1 em, horizontal scroll; inline math line height | 0.1 |
| 4 | Browser check `tests/browser_math.cjs` on the fixture: the quadratic formula gives a `math` element (inline and display); a `\left(` around a fraction is at least as high as the fraction; `\href{https://example.com}{x}` gives no `a` and its text has the text color; an unknown command and a 2,001-character formula fall back to code; a phone-width page scrolls a wide matrix; dark theme; the first render and 200 formulas timed, with formulas past the limit shown as code; a redraw does not call `katex.render` again. Each check fails on the code of today, and the bracket check also fails without the `@font-face` rule | 0.4 |
| 5 | `DOCS.md` (what math looks like, the fallback) and a changelog line under the unreleased version | 0.1 |
