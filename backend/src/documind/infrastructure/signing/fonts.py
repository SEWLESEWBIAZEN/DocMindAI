"""Bundled fonts and per-character fallback (see ``fonts/README.md``).

No single font covers every script, so text is split into runs and each run is drawn with
the first font in the family that has all of its characters. Scripts that need shaping or
right-to-left layout are refused: drawing them glyph by glyph would put a wrong-looking
name on a signed document.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from documind.domain.errors import ValidationFailedError

FONT_DIR = Path(__file__).parent / "fonts"

_FILES = {
    "DejaVuSans": "DejaVuSans.ttf",
    "DejaVuSans-Bold": "DejaVuSans-Bold.ttf",
    "NotoSansEthiopic": "NotoSansEthiopic-Regular.ttf",
    "NotoSansEthiopic-Bold": "NotoSansEthiopic-Bold.ttf",
    "GreatVibes": "GreatVibes-Regular.ttf",
}

#: Fonts tried in order for each character.
REGULAR = ("DejaVuSans", "NotoSansEthiopic")
BOLD = ("DejaVuSans-Bold", "NotoSansEthiopic-Bold")
#: Typed signatures: the script font where it has the character, plain text otherwise.
SCRIPT = ("GreatVibes", *REGULAR)

# Scripts whose letters change shape with their neighbours or run right to left.
_NEEDS_SHAPING = (
    (0x0590, 0x08FF),  # Hebrew, Arabic, Syriac, Thaana, NKo, Samaritan, Mandaic
    (0x0900, 0x0DFF),  # Indic scripts
    (0x0E00, 0x0FFF),  # Thai, Lao, Tibetan
    (0x1000, 0x109F),  # Myanmar
    (0x1780, 0x17FF),  # Khmer
    (0xA8E0, 0xA8FF),  # Devanagari Extended
    (0xFB1D, 0xFDFF),  # Hebrew and Arabic presentation forms
    (0xFE70, 0xFEFF),  # Arabic presentation forms B
)


def path(name: str) -> Path:
    return FONT_DIR / _FILES[name]


@cache
def _font(name: str) -> TTFont:
    font = TTFont(name, str(path(name)))
    pdfmetrics.registerFont(font)
    return font


def register() -> None:
    """Register every bundled font with reportlab (idempotent)."""
    for name in _FILES:
        _font(name)


@cache
def _coverage(name: str) -> frozenset[int]:
    return frozenset(_font(name).face.charToGlyph)


@dataclass(frozen=True, slots=True)
class Run:
    font: str
    text: str


def normalise(text: str) -> str:
    """NFC, with control and formatting characters (including bidi overrides) refused."""
    text = unicodedata.normalize("NFC", text)
    for ch in text:
        if unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs"):
            raise ValidationFailedError(
                "The text contains control characters.", code="unsupported_characters"
            )
    return text


def runs(text: str, family: tuple[str, ...] = REGULAR) -> list[Run]:
    """Split ``text`` into runs, each drawable with one font of ``family``."""
    text = normalise(text)
    out: list[Run] = []
    for ch in text:
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _NEEDS_SHAPING):
            raise ValidationFailedError(
                "Text in this script can't be drawn on a signed document yet.",
                code="unsupported_characters",
            )
        if ch.isspace():
            font = out[-1].font if out else family[0]
            ch = " "
        else:
            font = next((f for f in family if cp in _coverage(f)), "")
            if not font:
                raise ValidationFailedError(
                    f"The character {ch!r} can't be drawn on a signed document.",
                    code="unsupported_characters",
                )
        if out and out[-1].font == font:
            out[-1] = Run(font, out[-1].text + ch)
        else:
            out.append(Run(font, ch))
    return out


def check_drawable(text: str, family: tuple[str, ...] = REGULAR) -> str:
    """Raise if ``text`` can't be drawn; returns the normalised text."""
    runs(text, family)
    return normalise(text)


def width(parts: list[Run], size: float) -> float:
    return sum(pdfmetrics.stringWidth(r.text, _font(r.font).fontName, size) for r in parts)


def markup(text: str, family: tuple[str, ...] = REGULAR) -> str:
    """Paragraph markup that switches font for runs the first font can't draw."""
    return "".join(
        escape(r.text) if r.font == family[0] else f'<font name="{r.font}">{escape(r.text)}</font>'
        for r in runs(text, family)
    )
