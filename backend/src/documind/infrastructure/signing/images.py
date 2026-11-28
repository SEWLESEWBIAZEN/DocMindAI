"""Signature images (design §7.3).

Whatever the browser sends is decoded, checked and re-encoded here, so only a plain PNG
produced by Pillow is ever stored or stamped: no metadata, no unexpected format, no image
that decodes to something much larger than it claims.
"""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass
from functools import cache

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from documind.domain.errors import (
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)
from documind.infrastructure.signing import fonts

MAX_BYTES = 1_000_000
MAX_WIDTH, MAX_HEIGHT = 2000, 1000
MIN_SIDE = 10
MAX_TYPED = 100
INK = (17, 24, 39, 255)  # near-black, like a pen
_ALLOWED = frozenset({"PNG", "JPEG"})
_PAD = 8


@dataclass(frozen=True, slots=True)
class SignatureImage:
    png: bytes
    sha256: str
    width: int
    height: int


def _finish(image: Image.Image) -> SignatureImage:
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    png = out.getvalue()
    return SignatureImage(
        png=png, sha256=hashlib.sha256(png).hexdigest(), width=image.width, height=image.height
    )


def _ink_box(image: Image.Image) -> tuple[int, int, int, int] | None:
    """Bounding box of anything darker than paper, ignoring transparent pixels."""
    flat = Image.new("RGBA", image.size, (255, 255, 255, 255))
    flat.alpha_composite(image)
    return flat.convert("L").point(lambda p: 255 if p < 200 else 0).getbbox()


def clean_signature_image(data: bytes) -> SignatureImage:
    """Validate a drawn or uploaded signature and re-encode it as a metadata-free PNG."""
    if len(data) > MAX_BYTES:
        raise PayloadTooLargeError("Signature images must be 1 MB or smaller.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            source = Image.open(io.BytesIO(data))
            if source.format not in _ALLOWED:
                raise UnsupportedMediaTypeError(
                    "Signature images must be PNG or JPEG.", code="invalid_signature_image"
                )
            width, height = source.size  # read from the header, before decoding
            if width > MAX_WIDTH or height > MAX_HEIGHT:
                raise ValidationFailedError(
                    f"Signature images can be at most {MAX_WIDTH} by {MAX_HEIGHT} pixels.",
                    code="invalid_signature_image",
                )
            if width < MIN_SIDE or height < MIN_SIDE:
                raise ValidationFailedError(
                    "The signature image is too small.", code="invalid_signature_image"
                )
            source.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombWarning):
        raise ValidationFailedError(
            "The signature image couldn't be read.", code="invalid_signature_image"
        ) from None
    # A fresh image carries none of the source's metadata (EXIF, ICC, text chunks).
    rgba = source.convert("RGBA")
    clean = Image.new("RGBA", rgba.size)
    clean.paste(rgba)
    box = _ink_box(clean)
    if box is None:
        raise ValidationFailedError("The signature is empty.", code="empty_signature")
    x0, y0, x1, y1 = box
    clean = clean.crop(
        (max(0, x0 - _PAD), max(0, y0 - _PAD), min(width, x1 + _PAD), min(height, y1 + _PAD))
    )
    return _finish(clean)


@cache
def _pil_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(fonts.path(name)), size)


def render_typed_signature(text: str, *, size: int = 120) -> SignatureImage:
    """Draw a typed signature in the bundled script font, falling back per character."""
    text = " ".join(fonts.normalise(text).split())
    if not text:
        raise ValidationFailedError("Type your name to sign.", code="empty_signature")
    if len(text) > MAX_TYPED:
        raise ValidationFailedError("The typed signature is too long.", code="invalid_signature")
    parts = fonts.runs(text, fonts.SCRIPT)
    while True:
        measured = [(_pil_font(r.font, size), r.text) for r in parts]
        width = sum(f.getlength(t) for f, t in measured)
        if width + 2 * _PAD <= MAX_WIDTH or size <= 24:
            break
        size = int(size * 0.8)
    ascent = max(f.getmetrics()[0] for f, _ in measured)
    descent = max(f.getmetrics()[1] for f, _ in measured)
    canvas = Image.new("RGBA", (int(width) + 4 * _PAD, ascent + descent + 2 * _PAD))
    draw = ImageDraw.Draw(canvas)
    x: float = 2 * _PAD
    baseline = _PAD + ascent
    for font, run in measured:
        draw.text((x, baseline), run, font=font, fill=INK, anchor="ls")
        x += font.getlength(run)
    box = _ink_box(canvas)
    if box is not None:
        x0, y0, x1, y1 = box
        canvas = canvas.crop(
            (
                max(0, x0 - _PAD),
                max(0, y0 - _PAD),
                min(canvas.width, x1 + _PAD),
                min(canvas.height, y1 + _PAD),
            )
        )
    return _finish(canvas)
