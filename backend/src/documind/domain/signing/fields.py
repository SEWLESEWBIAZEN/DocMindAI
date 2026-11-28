"""Signing fields and their page coordinates (design §3.1, §6.3).

**Stored coordinates** are PDF points in *displayed page space*: the page's CropBox after
its ``/Rotate`` is applied, origin at the top left, y pointing down. The editor converts
from screen pixels by dividing by its zoom, so stored values don't depend on zoom.

:func:`to_pdf_rect` converts a stored rectangle into PDF user space (origin bottom left,
unrotated), which is what the stamping step needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from documind.domain.errors import ValidationFailedError


class FieldType(StrEnum):
    SIGNATURE = "signature"
    INITIALS = "initials"
    FULL_NAME = "full_name"
    DATE_SIGNED = "date_signed"
    TEXT = "text"
    CHECKBOX = "checkbox"
    COMPANY = "company"


#: Filled in by the server, never by the signer.
SERVER_FILLED = frozenset({FieldType.FULL_NAME, FieldType.DATE_SIGNED})
#: Completed with a signature image.
SIGNATURE_TYPES = frozenset({FieldType.SIGNATURE, FieldType.INITIALS})

MIN_SIZE = 8.0  # points
MAX_TEXT = 500


@dataclass(frozen=True, slots=True)
class PageGeometry:
    """One page of the signing snapshot, as recorded when the snapshot was made."""

    index: int
    cropbox: tuple[float, float, float, float]  # x0, y0, x1, y1 in PDF user space
    rotate: int  # 0, 90, 180 or 270

    @property
    def display_size(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.cropbox
        w, h = x1 - x0, y1 - y0
        return (h, w) if self.rotate in (90, 270) else (w, h)


@dataclass(frozen=True, slots=True)
class FieldBox:
    page_index: int
    x: float
    y: float
    width: float
    height: float


def normalise_rotation(value: int) -> int:
    rotation = value % 360
    if rotation not in (0, 90, 180, 270):
        raise ValidationFailedError("Page rotation must be a multiple of 90 degrees.")
    return rotation


def validate_box(box: FieldBox, pages: list[PageGeometry]) -> None:
    """The field must lie inside its page (with a hair of tolerance for rounding)."""
    if not 0 <= box.page_index < len(pages):
        raise ValidationFailedError(
            "A field is on a page that doesn't exist.", code="invalid_field"
        )
    width, height = pages[box.page_index].display_size
    eps = 0.5
    if box.width < MIN_SIZE or box.height < MIN_SIZE:
        raise ValidationFailedError("A field is too small.", code="invalid_field")
    if (
        box.x < -eps
        or box.y < -eps
        or box.x + box.width > width + eps
        or box.y + box.height > height + eps
    ):
        raise ValidationFailedError("A field extends beyond its page.", code="invalid_field")


def to_pdf_point(u: float, v: float, page: PageGeometry) -> tuple[float, float]:
    """A displayed-space point (top-left origin) in PDF user space."""
    cx0, cy0, cx1, cy1 = page.cropbox
    match page.rotate:
        case 0:
            return cx0 + u, cy1 - v
        case 90:
            return cx0 + v, cy0 + u
        case 180:
            return cx1 - u, cy0 + v
        case 270:
            return cx1 - v, cy1 - u
    raise ValidationFailedError("Unsupported page rotation.")


def to_pdf_rect(box: FieldBox, page: PageGeometry) -> tuple[float, float, float, float]:
    """``(x0, y0, x1, y1)`` in PDF user space for a stored displayed-space rectangle."""
    ax, ay = to_pdf_point(box.x, box.y, page)
    bx, by = to_pdf_point(box.x + box.width, box.y + box.height, page)
    return min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)
