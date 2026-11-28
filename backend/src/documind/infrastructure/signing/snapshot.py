"""The signing snapshot (design §6.2).

The source version is never changed. The snapshot is a cleaned, flattened copy that every
recipient sees and that the signed PDF is stamped onto:

- encrypted PDFs are refused, including those with only an owner password;
- existing digital signatures are detected and, because flattening invalidates them, only
  accepted after the sender confirms;
- form fields and annotations with an appearance are flattened into the page content, so the
  document can't change after it is sent; links without an appearance stay;
- JavaScript, launch and form-submission actions, document open actions and embedded files
  are removed: signers can't see them in the portal;
- each page's MediaBox, CropBox and ``/Rotate`` are written onto the page itself (they can be
  inherited from the page tree) and recorded, because field coordinates are relative to them.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from typing import Any

import pikepdf

from documind.domain.errors import ConflictError, ValidationFailedError
from documind.domain.signing.fields import PageGeometry, normalise_rotation

MAX_PAGES = 500
_INHERITABLE = ("/MediaBox", "/CropBox", "/Rotate", "/Resources")
_UNSAFE_ACTIONS = frozenset(
    {"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/ResetForm", "/Rendition", "/Sound"}
)

Box = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class SnapshotPage:
    index: int
    mediabox: Box
    cropbox: Box
    rotate: int

    @property
    def geometry(self) -> PageGeometry:
        return PageGeometry(index=self.index, cropbox=self.cropbox, rotate=self.rotate)

    def as_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "mediabox": list(self.mediabox),
            "cropbox": list(self.cropbox),
            "rotate": self.rotate,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SnapshotPage:
        return cls(
            index=int(data["index"]),
            mediabox=_box(data["mediabox"]),
            cropbox=_box(data["cropbox"]),
            rotate=int(data["rotate"]),
        )


@dataclass(frozen=True, slots=True)
class Snapshot:
    data: bytes
    sha256: str
    pages: tuple[SnapshotPage, ...]
    #: The source had digital signatures; the sender confirmed they would be invalidated.
    had_signatures: bool
    #: What cleaning removed, for the ``snapshot.created`` event.
    removed: dict[str, int] = field(default_factory=dict)


def _box(values: Any) -> Box:
    x0, y0, x1, y1 = (float(v) for v in values)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _unsupported(message: str) -> ValidationFailedError:
    return ValidationFailedError(message, code="unsupported_pdf")


def _inherited(page: pikepdf.Dictionary, key: str) -> Any:
    node: Any = page
    seen: set[tuple[int, int]] = set()
    while isinstance(node, pikepdf.Dictionary):
        if key in node:
            return node[key]
        og = node.objgen
        if og in seen:  # a malformed tree that loops
            return None
        seen.add(og)
        node = node.get("/Parent")
    return None


def _walk_fields(fields: Any, depth: int = 0) -> Any:
    if depth > 32 or not isinstance(fields, pikepdf.Array):
        return
    for f in fields:
        if isinstance(f, pikepdf.Dictionary):
            yield f
            yield from _walk_fields(f.get("/Kids"), depth + 1)


def has_signatures(pdf: pikepdf.Pdf) -> bool:
    root = pdf.Root
    if "/Perms" in root:  # certification (DocMDP) or usage-rights signatures
        return True
    form = root.get("/AcroForm")
    if isinstance(form, pikepdf.Dictionary):
        if int(form.get("/SigFlags", 0)) & 1:
            return True
        if any(f.get("/FT") == "/Sig" for f in _walk_fields(form.get("/Fields"))):
            return True
    for page in pdf.pages:
        for annot in page.obj.get("/Annots", pikepdf.Array()):
            if isinstance(annot, pikepdf.Dictionary) and annot.get("/FT") == "/Sig":
                return True
    return False


def _strip_active_content(pdf: pikepdf.Pdf) -> dict[str, int]:
    removed = dict.fromkeys(
        ("document_actions", "javascript", "embedded_files", "actions", "form_fields"), 0
    )
    root = pdf.Root
    for key in ("/OpenAction", "/AA"):
        if key in root:
            del root[key]
            removed["document_actions"] += 1
    names = root.get("/Names")
    if isinstance(names, pikepdf.Dictionary):
        for key, label in (("/JavaScript", "javascript"), ("/EmbeddedFiles", "embedded_files")):
            if key in names:
                del names[key]
                removed[label] += 1
    for page in pdf.pages:
        obj = page.obj
        if "/AA" in obj:
            del obj["/AA"]
            removed["actions"] += 1
        annots = obj.get("/Annots")
        if not isinstance(annots, pikepdf.Array):
            continue
        keep = pikepdf.Array()
        for annot in annots:
            if not isinstance(annot, pikepdf.Dictionary):
                continue
            subtype = annot.get("/Subtype")
            if subtype in ("/FileAttachment", "/Sound", "/Movie", "/Screen"):
                removed["embedded_files"] += 1
                continue
            if subtype == "/Widget":  # a form field with no appearance; the form is gone
                removed["form_fields"] += 1
                continue
            if "/AA" in annot:
                del annot["/AA"]
                removed["actions"] += 1
            action = annot.get("/A")
            if isinstance(action, pikepdf.Dictionary) and action.get("/S") in _UNSAFE_ACTIONS:
                del annot["/A"]
                removed["actions"] += 1
            keep.append(annot)
        obj["/Annots"] = keep
    return {k: v for k, v in removed.items() if v}


def _fix_geometry(pdf: pikepdf.Pdf) -> tuple[SnapshotPage, ...]:
    pages: list[SnapshotPage] = []
    for index, page in enumerate(pdf.pages):
        obj = page.obj
        values = {key: _inherited(obj, key) for key in _INHERITABLE}
        if values["/MediaBox"] is None:
            raise _unsupported(f"Page {index + 1} has no size.")
        media = _box(values["/MediaBox"])
        crop = _box(values["/CropBox"]) if values["/CropBox"] is not None else media
        # The CropBox is clipped to the MediaBox (PDF 32000-1 §14.11.2).
        crop = (
            max(crop[0], media[0]),
            max(crop[1], media[1]),
            min(crop[2], media[2]),
            min(crop[3], media[3]),
        )
        if crop[2] - crop[0] < 1 or crop[3] - crop[1] < 1:
            raise _unsupported(f"Page {index + 1} has no visible area.")
        try:
            rotate = normalise_rotation(int(values["/Rotate"] or 0))
        except (TypeError, ValueError, ValidationFailedError):
            raise _unsupported(f"Page {index + 1} has an unsupported rotation.") from None
        obj["/MediaBox"] = pikepdf.Array(media)
        obj["/CropBox"] = pikepdf.Array(crop)
        obj["/Rotate"] = rotate
        if values["/Resources"] is not None and "/Resources" not in obj:
            obj["/Resources"] = values["/Resources"]
        pages.append(SnapshotPage(index=index, mediabox=media, cropbox=crop, rotate=rotate))
    return tuple(pages)


def build_snapshot(data: bytes, *, confirm_invalidates_signatures: bool = False) -> Snapshot:
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except pikepdf.PasswordError:
        raise _unsupported("Password-protected PDFs can't be sent for signature.") from None
    except (pikepdf.PdfError, ValueError):
        raise _unsupported("This file isn't a readable PDF.") from None
    with pdf:
        if pdf.is_encrypted:
            raise _unsupported(
                "Encrypted PDFs can't be sent for signature. Save an unencrypted copy first."
            )
        count = len(pdf.pages)
        if count == 0:
            raise _unsupported("This PDF has no pages.")
        if count > MAX_PAGES:
            raise _unsupported(f"PDFs with more than {MAX_PAGES} pages can't be sent yet.")
        signed = has_signatures(pdf)
        if signed and not confirm_invalidates_signatures:
            raise ConflictError(
                "This PDF is already digitally signed. Sending it for signature removes those "
                "signatures from the copy that is signed.",
                code="pdf_has_signatures",
            )
        try:
            pdf.generate_appearance_streams()
            pdf.flatten_annotations("all")
        except pikepdf.PdfError:
            raise _unsupported("This PDF's form fields couldn't be flattened.") from None
        if "/AcroForm" in pdf.Root:
            del pdf.Root["/AcroForm"]
        if "/Perms" in pdf.Root:
            del pdf.Root["/Perms"]
        removed = _strip_active_content(pdf)
        pages = _fix_geometry(pdf)
        out = io.BytesIO()
        pdf.save(out, compress_streams=True)
    snapshot = out.getvalue()
    return Snapshot(
        data=snapshot,
        sha256=hashlib.sha256(snapshot).hexdigest(),
        pages=pages,
        had_signatures=signed,
        removed=removed,
    )
