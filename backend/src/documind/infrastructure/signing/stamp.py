"""Draw completed fields onto the signing snapshot (design §6.3, §6.4).

For each page with fields, reportlab draws an overlay in the page's own user space; it is
added to the page as a Form XObject after the original content, which is wrapped in
``q``/``Q`` so a stray graphics state can't move or clip the overlay. Each field is drawn in
a local frame that is turned with the page's ``/Rotate``, so its content looks upright to the
reader whatever the page rotation.
"""

from __future__ import annotations

import hashlib
import io
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import pikepdf
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen.canvas import Canvas

from documind.domain.errors import ValidationFailedError
from documind.domain.signing.fields import FieldBox, to_pdf_point
from documind.infrastructure.signing import fonts
from documind.infrastructure.signing.snapshot import SnapshotPage

INK = (17 / 255, 24 / 255, 39 / 255)
MAX_FONT = 16.0
MIN_FONT = 5.0
_PAD = 2.0


class StampKind(StrEnum):
    IMAGE = "image"  # signature and initials
    TEXT = "text"  # name, date, text, company
    CHECK = "check"  # a ticked checkbox


@dataclass(frozen=True, slots=True)
class StampItem:
    box: FieldBox
    kind: StampKind
    image: bytes | None = None
    text: str | None = None


@dataclass(frozen=True, slots=True)
class Stamped:
    data: bytes
    sha256: str


def font_size_for(text: str, width: float, height: float) -> float | None:
    """The font size that fits ``text`` on one line in the box, or ``None`` if none does."""
    parts = fonts.runs(text)
    size = min(MAX_FONT, (height - 2 * _PAD) * 0.7)
    if size < MIN_FONT:
        return None
    room = width - 2 * _PAD
    natural = fonts.width(parts, size)
    if natural > room:
        size *= room / natural
    return size if size >= MIN_FONT else None


def _draw_text(c: Canvas, text: str, w: float, h: float) -> None:
    size = font_size_for(text, w, h)
    if size is None:
        raise ValidationFailedError("The text doesn't fit in its field.", code="field_overflow")
    fonts.register()
    ascent, descent = pdfmetrics.getAscentDescent(fonts.REGULAR[0], size)
    baseline = (h - (ascent - descent)) / 2 - descent
    x = _PAD
    c.setFillColorRGB(*INK)
    for run in fonts.runs(text):
        c.setFont(run.font, size)
        c.drawString(x, baseline, run.text)
        x += pdfmetrics.stringWidth(run.text, run.font, size)


def _draw_image(c: Canvas, png: bytes, w: float, h: float) -> None:
    c.drawImage(
        ImageReader(io.BytesIO(png)),
        _PAD,
        _PAD,
        width=w - 2 * _PAD,
        height=h - 2 * _PAD,
        mask="auto",
        preserveAspectRatio=True,
        anchor="sw",
    )


def _draw_check(c: Canvas, w: float, h: float) -> None:
    s = min(w, h) - 2 * _PAD
    x0, y0 = (w - s) / 2, (h - s) / 2
    c.setStrokeColorRGB(*INK)
    c.setLineWidth(max(1.0, s / 8))
    c.setLineCap(1)
    c.setLineJoin(1)
    path = c.beginPath()
    path.moveTo(x0 + s * 0.18, y0 + s * 0.52)
    path.lineTo(x0 + s * 0.42, y0 + s * 0.26)
    path.lineTo(x0 + s * 0.84, y0 + s * 0.78)
    c.drawPath(path, stroke=1, fill=0)


def _overlay(page: SnapshotPage, items: Sequence[StampItem]) -> bytes:
    mx0, my0, mx1, my1 = page.mediabox
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(mx1 - mx0, my1 - my0), invariant=True)
    c.translate(-mx0, -my0)  # draw in the target page's user space
    geometry = page.geometry
    for item in items:
        b = item.box
        # The field's bottom-left corner as the reader sees it, and the reader's axes.
        ox, oy = to_pdf_point(b.x, b.y + b.height, geometry)
        c.saveState()
        c.translate(ox, oy)
        c.rotate(page.rotate)
        if item.kind is StampKind.IMAGE and item.image:
            _draw_image(c, item.image, b.width, b.height)
        elif item.kind is StampKind.TEXT and item.text:
            _draw_text(c, item.text, b.width, b.height)
        elif item.kind is StampKind.CHECK:
            _draw_check(c, b.width, b.height)
        c.restoreState()
    c.showPage()
    c.save()
    return buf.getvalue()


def stamp(snapshot: bytes, pages: Sequence[SnapshotPage], items: Sequence[StampItem]) -> Stamped:
    """The snapshot with every item drawn on it, as a new PDF."""
    by_page: dict[int, list[StampItem]] = defaultdict(list)
    for item in items:
        if not 0 <= item.box.page_index < len(pages):
            raise ValidationFailedError("A field is on a page that doesn't exist.")
        by_page[item.box.page_index].append(item)
    with pikepdf.open(io.BytesIO(snapshot)) as pdf:
        if len(pdf.pages) != len(pages):
            raise ValidationFailedError("The snapshot doesn't match its recorded pages.")
        for index, page_items in sorted(by_page.items()):
            geometry = pages[index]
            target = pdf.pages[index]
            with pikepdf.open(io.BytesIO(_overlay(geometry, page_items))) as overlay:
                form = pdf.copy_foreign(overlay.pages[0].as_form_xobject())
            mx0, my0, _, _ = geometry.mediabox
            name = target.add_resource(form, pikepdf.Name.XObject, prefix="DmSign")
            target.contents_coalesce()
            target.contents_add(pikepdf.Stream(pdf, b"q\n"), prepend=True)
            target.contents_add(
                pikepdf.Stream(pdf, f"Q\nq 1 0 0 1 {mx0:.4f} {my0:.4f} cm {name} Do Q\n".encode())
            )
        out = io.BytesIO()
        pdf.save(out, compress_streams=True)
    data = out.getvalue()
    return Stamped(data=data, sha256=hashlib.sha256(data).hexdigest())
