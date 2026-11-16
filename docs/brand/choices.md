# Four original mark concepts

[Open the comparison sheet](choices.png) at 100% to inspect the native 32 px samples. Each row shows a square icon and a landscape logo on light and dark backgrounds. The comparison uses 256 × 256 icons and 320 × 128 logos. Small samples are 32 × 32 and 80 × 32. Each mark also has an editable SVG source under [brand/concepts](../../brand/concepts/).

A, Gate: a clear boundary with one deliberate route and a square endpoint. It suggests controlled execution. The icon has a broad frame; the landscape version extends the route. Choose this for a firm, technical identity.

B, Fold: a work surface with one contrasting diagonal fold. It suggests useful work on a document or configuration. Its large solid areas read well at small sizes. Choose this for a quiet, compact identity.

C, Beacon: a central pillar, a cap and two pairs of short side bars. It suggests steady guidance. Its open spaces keep the silhouette distinct from a page or a conversation. Choose this for a calm, helpful identity.

D, Relay: two offset panels with opposite corner cuts. It suggests an exchange between a person and a tool. The separated panels retain a clear gap at 32 px. Choose this for a conversation-focused identity.

All four use original geometry. They contain no product name, wordmark, borrowed logo or third-party image. Light and dark pairs share the same paths. Their two flat colors change with the background. The marks have transparent backgrounds and tight bounds. The comparison sheet supplies the theme backgrounds; they are not baked into the assets.

These are proposals for selection. The installed integration images, store icon, store logo and README remain unchanged. After selection, the approved direction needs its 512 px integration exports, store sizes and README header. The product-name decision must precede any wordmark.

Home Assistant's [current brand rules](https://contributors.home-assistant.io/core/integrations/brands/) specify square 256/512 px icons, transparent trimmed PNGs and landscape logos with a 128–256 px short side. They place custom integration images inside that integration's brand directory. The proposal directory is not an installed integration directory. Shipping the chosen set is a later step.

The [source prompt and settings](sources/generation-prompt.txt) record the image exploration. The final flat masters are hand-authored SVGs. The image render retained glow, so its pixels are not used in the assets. Run `bash docs/brand/sources/export.sh` from the repository root to reproduce the PNGs and sheet with Inkscape, ImageMagick and Python. The source marks use no fonts; sheet labels use DejaVu Sans.

Original vector sources: The Backshop. The repository's MIT terms apply. No option is selected by this proposal.
