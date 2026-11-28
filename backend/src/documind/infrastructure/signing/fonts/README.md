# Fonts bundled for DocuMind Sign

Used to draw names, dates and text values onto signed PDFs, the completion certificate, and
typed signatures. Bundled so every image and environment draws the same glyphs.

| File | Font | Licence | Source | Changes |
|---|---|---|---|---|
| `DejaVuSans.ttf`, `DejaVuSans-Bold.ttf` | DejaVu Sans 2.37 | Bitstream Vera licence; DejaVu changes public domain (`LICENSE-DejaVu.txt`) | Debian `fonts-dejavu-core` | None |
| `NotoSansEthiopic-Regular.ttf`, `-Bold.ttf` | Noto Sans Ethiopic | SIL OFL 1.1 (`LICENSE-NotoSansEthiopic.txt`) | npm `@fontsource/noto-sans-ethiopic@5.3.0`, Ethiopic subset | WOFF converted to TTF with fontTools |
| `GreatVibes-Regular.ttf` | Great Vibes | SIL OFL 1.1 (`LICENSE-GreatVibes.txt`) | npm `@fontsource/great-vibes@5.3.0` | Latin and Latin Extended subsets converted from WOFF and merged with fontTools |

Neither OFL font declares a Reserved Font Name, so the converted files keep their names.
Text is drawn run by run with the first font that has each character (`fonts.py`). Scripts that
need shaping or right-to-left layout (for example Arabic, Hebrew, Indic scripts) are refused
rather than drawn incorrectly.
