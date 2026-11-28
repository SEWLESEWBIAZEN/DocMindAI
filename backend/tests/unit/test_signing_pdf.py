"""DocuMind Sign PDF engine: snapshot, signature images, stamping, certificate."""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from typing import Any

import pikepdf
import pymupdf  # used here only to render and read the results
import pytest
from PIL import Image, PngImagePlugin
from reportlab.pdfgen.canvas import Canvas

from documind.domain.errors import (
    ConflictError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
    ValidationFailedError,
)
from documind.domain.signing.fields import FieldBox
from documind.infrastructure.signing import fonts
from documind.infrastructure.signing.certificate import (
    DISCLAIMER,
    CertificateData,
    CertificateRecipient,
    render_certificate,
)
from documind.infrastructure.signing.images import (
    MAX_BYTES,
    clean_signature_image,
    render_typed_signature,
)
from documind.infrastructure.signing.snapshot import SnapshotPage, build_snapshot
from documind.infrastructure.signing.stamp import (
    StampItem,
    StampKind,
    font_size_for,
    stamp,
)

# --------------------------------------------------------------------------- helpers


def _pdf(pages: int = 1, size: tuple[float, float] = (600, 800), text: str = "Agreement") -> bytes:
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=size, invariant=True)
    for i in range(pages):
        c.drawString(72, size[1] - 72, f"{text} page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def _edit(data: bytes, fn: object) -> bytes:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        fn(pdf)  # type: ignore[operator]
        out = io.BytesIO()
        pdf.save(out)
    return out.getvalue()


def _rotated(rotate: int, *, inherited: bool = False) -> bytes:
    def change(pdf: pikepdf.Pdf) -> None:
        page = pdf.pages[0].obj
        page["/CropBox"] = pikepdf.Array([10, 20, 590, 780])  # offset from the MediaBox
        if inherited:
            pdf.Root.Pages["/Rotate"] = rotate
            if "/Rotate" in page:
                del page["/Rotate"]
        else:
            page["/Rotate"] = rotate

    return _edit(_pdf(), change)


def _png(width: int = 200, height: int = 100, *, quadrant: bool = True) -> bytes:
    """Transparent, with only the top-left quadrant inked: shows rotation and mirroring."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if quadrant:
        image.paste((0, 0, 0, 255), (0, 0, width // 2, height // 2))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _render(data: bytes, index: int = 0) -> pymupdf.Pixmap:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return doc[index].get_pixmap(dpi=72, alpha=False)


def _dark(pix: pymupdf.Pixmap, u: float, v: float) -> bool:
    r, g, b = pix.pixel(int(u), int(v))[:3]
    return (r + g + b) / 3 < 100


def _text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "".join(page.get_text() for page in doc)


# --------------------------------------------------------------------------- snapshot


def test_snapshot_records_geometry_and_hash() -> None:
    snap = build_snapshot(_pdf(pages=3))
    assert snap.sha256 == hashlib.sha256(snap.data).hexdigest()
    assert len(snap.pages) == 3
    page = snap.pages[0]
    assert page.mediabox == page.cropbox == (0, 0, 600, 800)
    assert page.rotate == 0
    assert SnapshotPage.from_json(page.as_json()) == page
    assert not snap.had_signatures
    assert "Agreement page 2" in _text(snap.data)


@pytest.mark.parametrize("inherited", [False, True])
def test_snapshot_writes_inherited_geometry_onto_each_page(inherited: bool) -> None:
    snap = build_snapshot(_rotated(90, inherited=inherited))
    assert snap.pages[0].rotate == 90
    assert snap.pages[0].cropbox == (10, 20, 590, 780)
    assert snap.pages[0].geometry.display_size == (760, 580)
    with pikepdf.open(io.BytesIO(snap.data)) as pdf:
        page = pdf.pages[0].obj
        assert int(page["/Rotate"]) == 90
        assert [float(v) for v in page["/CropBox"]] == [10, 20, 590, 780]


def test_cropbox_is_clipped_to_the_mediabox() -> None:
    def change(pdf: pikepdf.Pdf) -> None:
        pdf.pages[0].obj["/CropBox"] = pikepdf.Array([-50, -50, 700, 900])

    assert build_snapshot(_edit(_pdf(), change)).pages[0].cropbox == (0, 0, 600, 800)


@pytest.mark.parametrize("user", ["", "open-me"])
def test_encrypted_pdfs_are_refused(user: str) -> None:
    out = io.BytesIO()
    with pikepdf.open(io.BytesIO(_pdf())) as pdf:
        pdf.save(out, encryption=pikepdf.Encryption(owner="owner-secret", user=user))
    with pytest.raises(ValidationFailedError) as err:
        build_snapshot(out.getvalue())
    assert err.value.code == "unsupported_pdf"


@pytest.mark.parametrize("data", [b"", b"not a pdf", b"%PDF-1.7\n%garbage"])
def test_unreadable_files_are_refused(data: bytes) -> None:
    with pytest.raises(ValidationFailedError) as err:
        build_snapshot(data)
    assert err.value.code == "unsupported_pdf"


def _signed(pdf: pikepdf.Pdf) -> None:
    page = pdf.pages[0]
    field = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Annot,
            Subtype=pikepdf.Name.Widget,
            FT=pikepdf.Name.Sig,
            T=pikepdf.String("Signature1"),
            Rect=pikepdf.Array([0, 0, 0, 0]),
            P=page.obj,
        )
    )
    page.obj["/Annots"] = pikepdf.Array([field])
    pdf.Root["/AcroForm"] = pikepdf.Dictionary(Fields=pikepdf.Array([field]), SigFlags=3)


def test_signed_pdfs_need_confirmation() -> None:
    data = _edit(_pdf(), _signed)
    with pytest.raises(ConflictError) as err:
        build_snapshot(data)
    assert err.value.code == "pdf_has_signatures"
    snap = build_snapshot(data, confirm_invalidates_signatures=True)
    assert snap.had_signatures
    with pikepdf.open(io.BytesIO(snap.data)) as pdf:
        assert "/AcroForm" not in pdf.Root
        assert not list(pdf.pages[0].obj.get("/Annots", []))


def test_form_fields_are_flattened_into_the_page() -> None:
    buf = io.BytesIO()
    c = Canvas(buf, pagesize=(600, 800))
    c.drawString(72, 700, "Form")
    c.acroForm.textfield(name="company", value="Acme Holdings", x=72, y=600, width=200)
    c.showPage()
    c.save()
    snap = build_snapshot(buf.getvalue())
    with pikepdf.open(io.BytesIO(snap.data)) as pdf:
        assert "/AcroForm" not in pdf.Root
        assert not [
            a for a in pdf.pages[0].obj.get("/Annots", []) if a.get("/Subtype") == "/Widget"
        ]
    assert "Acme Holdings" in _text(snap.data)  # the value is now part of the page


def test_active_content_is_removed_and_plain_links_stay() -> None:
    def change(pdf: pikepdf.Pdf) -> None:
        js = pikepdf.Dictionary(S=pikepdf.Name.JavaScript, JS=pikepdf.String("app.alert(1)"))
        pdf.Root["/OpenAction"] = js
        pdf.Root["/Names"] = pikepdf.Dictionary(
            JavaScript=pikepdf.Dictionary(Names=pikepdf.Array([pikepdf.String("x"), js]))
        )
        link: dict[str, Any] = {"Type": pikepdf.Name.Annot, "Subtype": pikepdf.Name.Link}
        rect = pikepdf.Array([72, 72, 200, 90])
        pdf.pages[0].obj["/Annots"] = pikepdf.Array(
            [
                pikepdf.Dictionary(**link, Rect=rect, A=js),
                pikepdf.Dictionary(
                    **link,
                    Rect=rect,
                    A=pikepdf.Dictionary(
                        S=pikepdf.Name.URI, URI=pikepdf.String("https://example.com")
                    ),
                ),
            ]
        )

    snap = build_snapshot(_edit(_pdf(), change))
    assert snap.removed["document_actions"] == 1
    assert snap.removed["javascript"] == 1
    assert snap.removed["actions"] == 1
    with pikepdf.open(io.BytesIO(snap.data)) as pdf:
        assert "/OpenAction" not in pdf.Root
        assert "/JavaScript" not in pdf.Root.Names
        annots = list(pdf.pages[0].obj["/Annots"])
        assert len(annots) == 2
        assert "/A" not in annots[0]
        assert annots[1].A.URI == "https://example.com"


def test_the_source_bytes_are_not_changed() -> None:
    source = _rotated(180)
    before = hashlib.sha256(source).hexdigest()
    build_snapshot(source)
    assert hashlib.sha256(source).hexdigest() == before


# --------------------------------------------------------------------------- stamping


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_a_stamp_lands_where_it_was_placed_and_reads_upright(rotate: int) -> None:
    snap = build_snapshot(_rotated(rotate))
    box = FieldBox(page_index=0, x=120, y=150, width=104, height=54)
    result = stamp(snap.data, snap.pages, [StampItem(box, StampKind.IMAGE, image=_png())])
    pix = _render(result.data)
    # The rendered page is the displayed page: CropBox with /Rotate applied.
    assert (pix.width, pix.height) == tuple(int(v) for v in snap.pages[0].geometry.display_size)
    # Only the image's top-left quadrant is inked; it must appear top left inside the field.
    assert _dark(pix, box.x + 20, box.y + 12)
    assert not _dark(pix, box.x + 75, box.y + 12)  # top right
    assert not _dark(pix, box.x + 20, box.y + 42)  # bottom left
    assert not _dark(pix, box.x - 10, box.y + 12)  # outside the field
    assert result.sha256 == hashlib.sha256(result.data).hexdigest()


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_text_and_checkmarks_are_drawn_in_their_fields(rotate: int) -> None:
    snap = build_snapshot(_rotated(rotate))
    name = FieldBox(0, 40, 40, 220, 28)
    check = FieldBox(0, 300, 40, 24, 24)
    result = stamp(
        snap.data,
        snap.pages,
        [
            StampItem(name, StampKind.TEXT, text="Abebe Bikila ፊርማ"),
            StampItem(check, StampKind.CHECK),
        ],
    )
    with pymupdf.open(stream=result.data, filetype="pdf") as doc:
        page = doc[0]
        words = page.get_text("words")
        found = [w for w in words if w[4] == "Abebe"]
        assert found, words
        # The word's displayed bounding box lies inside the field.
        x0, y0, x1, y1 = pymupdf.Rect(found[0][:4]) * page.rotation_matrix
        assert name.x - 1 <= min(x0, x1)
        assert max(x0, x1) <= name.x + name.width + 1
        assert name.y - 1 <= min(y0, y1)
        assert max(y0, y1) <= name.y + name.height + 1
        assert "ፊርማ" in page.get_text()
    pix = _render(result.data)
    inked = [
        _dark(pix, check.x + dx, check.y + dy) for dx in range(4, 21, 2) for dy in range(4, 21, 2)
    ]
    assert any(inked)


def test_original_content_is_kept_and_other_pages_are_untouched() -> None:
    snap = build_snapshot(_pdf(pages=2))
    result = stamp(
        snap.data, snap.pages, [StampItem(FieldBox(1, 72, 300, 150, 30), StampKind.CHECK)]
    )
    text = _text(result.data)
    assert "Agreement page 1" in text
    assert "Agreement page 2" in text
    with pikepdf.open(io.BytesIO(snap.data)) as a, pikepdf.open(io.BytesIO(result.data)) as b:
        assert a.pages[0].obj.Contents.read_bytes() == b.pages[0].obj.Contents.read_bytes()


def test_text_that_does_not_fit_is_refused() -> None:
    assert font_size_for("Ann", 100, 30) is not None
    assert font_size_for("A very long company name " * 10, 60, 20) is None
    snap = build_snapshot(_pdf())
    with pytest.raises(ValidationFailedError) as err:
        stamp(
            snap.data,
            snap.pages,
            [StampItem(FieldBox(0, 10, 10, 40, 12), StampKind.TEXT, text="x" * 200)],
        )
    assert err.value.code == "field_overflow"


def test_stamping_refuses_fields_on_missing_pages() -> None:
    snap = build_snapshot(_pdf())
    with pytest.raises(ValidationFailedError):
        stamp(snap.data, snap.pages, [StampItem(FieldBox(3, 0, 0, 50, 20), StampKind.CHECK)])


# --------------------------------------------------------------------------- fonts


def test_runs_fall_back_per_character() -> None:
    parts = fonts.runs("Ann ፊርማ Zoë")
    assert [p.font for p in parts] == ["DejaVuSans", "NotoSansEthiopic", "DejaVuSans"]
    assert "".join(p.text for p in parts) == "Ann ፊርማ Zoë"
    assert fonts.markup("A&B ፊ") == 'A&amp;B <font name="NotoSansEthiopic">ፊ</font>'


@pytest.mark.parametrize("text", ["مرحبا", "שלום", "नमस्ते", "Ann‮evil", "a\x00b"])
def test_text_that_would_be_drawn_wrongly_is_refused(text: str) -> None:
    with pytest.raises(ValidationFailedError) as err:
        fonts.runs(text)
    assert err.value.code == "unsupported_characters"


def test_text_is_nfc_normalised() -> None:
    assert fonts.check_drawable("Zoë") == "Zoë"


# --------------------------------------------------------------------------- images


def test_signature_images_are_re_encoded_without_metadata() -> None:
    image = Image.new("RGBA", (300, 120), (0, 0, 0, 0))
    image.paste((10, 10, 10, 255), (50, 40, 250, 80))
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", "secret@example.com")
    raw = io.BytesIO()
    image.save(raw, "PNG", pnginfo=info)
    cleaned = clean_signature_image(raw.getvalue())
    assert b"secret@example.com" not in cleaned.png
    assert cleaned.sha256 == hashlib.sha256(cleaned.png).hexdigest()
    out = Image.open(io.BytesIO(cleaned.png))
    assert out.format == "PNG"
    assert not out.info  # no text chunks, EXIF or ICC profile
    assert (cleaned.width, cleaned.height) == (216, 56)  # cropped to the ink plus padding


def test_jpeg_uploads_become_png() -> None:
    image = Image.new("RGB", (200, 80), "white")
    image.paste((0, 0, 0), (20, 20, 180, 60))
    raw = io.BytesIO()
    image.save(raw, "JPEG", exif=Image.Exif())
    assert Image.open(io.BytesIO(clean_signature_image(raw.getvalue()).png)).format == "PNG"


def test_bad_signature_images_are_refused() -> None:
    gif = io.BytesIO()
    Image.new("RGB", (50, 50)).save(gif, "GIF")
    with pytest.raises(UnsupportedMediaTypeError):
        clean_signature_image(gif.getvalue())
    with pytest.raises(PayloadTooLargeError):
        clean_signature_image(b"\x89PNG" + b"0" * MAX_BYTES)
    with pytest.raises(ValidationFailedError) as err:
        clean_signature_image(b"\x89PNG\r\n\x1a\nnot really")
    assert err.value.code == "invalid_signature_image"
    with pytest.raises(ValidationFailedError) as err:
        clean_signature_image(_png(quadrant=False))
    assert err.value.code == "empty_signature"
    big = io.BytesIO()
    Image.new("1", (4000, 300)).save(big, "PNG")  # tiny file, too many pixels
    with pytest.raises(ValidationFailedError):
        clean_signature_image(big.getvalue())


def test_typed_signatures_render_in_the_script_font() -> None:
    sig = render_typed_signature("  Zoë   Ann  ")
    image = Image.open(io.BytesIO(sig.png))
    assert image.format == "PNG"
    assert image.width > image.height > 20
    # Characters the script font lacks fall back to the plain font instead of failing.
    assert render_typed_signature("አበበ").width > 0
    with pytest.raises(ValidationFailedError):
        render_typed_signature("   ")
    with pytest.raises(ValidationFailedError):
        render_typed_signature("مرحبا")


# --------------------------------------------------------------------------- certificate


def _certificate(recipients: int = 2) -> CertificateData:
    at = datetime(2026, 11, 28, 9, 30, tzinfo=UTC)
    return CertificateData(
        agreement_id="7f1c0a52-0000-4000-8000-000000000001",
        title="Mutual NDA — Acme & Globex",
        sender_name="Sara Tesfaye",
        sender_email="sara@example.com",
        routing="sequential",
        source_sha256="a" * 64,
        snapshot_sha256="b" * 64,
        final_sha256="c" * 64,
        sent_at=at,
        completed_at=at,
        chain_head="d" * 64,
        generated_at=at,
        recipients=[
            CertificateRecipient(
                name="አበበ ቢቂላ" if i == 0 else f"Signer {i}",
                email=f"signer{i}@example.com",
                role="signer",
                order=i + 1,
                auth_method="email_link",
                action="signed",
                acted_at=at,
                consented_at=at,
                ip_address="203.0.113.7",
            )
            for i in range(recipients)
        ],
    )


def test_certificate_contains_the_evidence() -> None:
    cert = render_certificate(_certificate())
    assert cert.sha256 == hashlib.sha256(cert.data).hexdigest()
    text = " ".join(_text(cert.data).split())
    for expected in (
        "Completion certificate",
        "Mutual NDA — Acme & Globex",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "Sara Tesfaye <sara@example.com>",
        "አበበ ቢቂላ",
        "signer1@example.com",
        "203.0.113.7",
        "2026-11-28 09:30:00 UTC",
        DISCLAIMER,
    ):
        assert expected in text, expected


def test_long_certificates_carry_the_disclaimer_on_every_page() -> None:
    cert = render_certificate(_certificate(recipients=20))
    with pymupdf.open(stream=cert.data, filetype="pdf") as doc:
        assert doc.page_count > 1
        for page in doc:
            assert DISCLAIMER in " ".join(page.get_text().split())


def test_a_stray_graphics_state_in_the_page_cannot_move_the_stamp() -> None:
    def change(pdf: pikepdf.Pdf) -> None:  # content that leaves a transform behind (no Q)
        pdf.pages[0].contents_add(pikepdf.Stream(pdf, b"q 1 0 0 1 250 -300 cm 0.5 0 0 0.5 0 0 cm"))

    snap = build_snapshot(_edit(_pdf(), change))
    box = FieldBox(0, 120, 150, 104, 54)
    pix = _render(
        stamp(snap.data, snap.pages, [StampItem(box, StampKind.IMAGE, image=_png())]).data
    )
    assert _dark(pix, box.x + 20, box.y + 12)
