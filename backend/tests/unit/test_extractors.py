import io
import zipfile
from collections.abc import Sequence

import docx
import pymupdf
import pytest

from documind.domain.documents.files import DocumentFormat
from documind.domain.processing.errors import PermanentProcessingError
from documind.domain.processing.model import BlockKind, ExtractedDocument
from documind.domain.processing.normalize import normalize_document
from documind.infrastructure.processing.extractors import (
    CompositeExtractor,
    ExtractionLimits,
    OcrOptions,
    find_tessdata,
)

extractor = CompositeExtractor()


def blocks(doc: ExtractedDocument) -> list[tuple[int | None, str, str, int | None]]:
    return [(p.page_number, b.kind.value, b.text, b.level) for p in doc.pages for b in p.blocks]


def make_pdf(
    pages: Sequence[Sequence[tuple[str, float, bool]]],
    *,
    toc: list[list[object]] | None = None,
    title: str = "",
) -> bytes:
    pdf = pymupdf.open()
    for page_lines in pages:
        page = pdf.new_page()
        y = 72.0
        for text, size, bold in page_lines:
            page.insert_text((72, y), text, fontsize=size, fontname="helv" if not bold else "hebo")
            y += size * 2.2
    if toc:
        pdf.set_toc(toc)
    if title:
        pdf.set_metadata({"title": title, "author": "Ada"})
    return pdf.tobytes()


BODY = "This agreement renews automatically for one year terms."


class TestPdf:
    def test_pages_headings_and_metadata(self) -> None:
        data = make_pdf(
            [
                [("Master Services Agreement", 22, True), ("Renewal", 15, True), (BODY, 11, False)],
                [("Termination", 15, True), ("Either party may terminate with notice.", 11, False)],
            ],
            title="Halcyon MSA",
        )
        doc = extractor.extract(data, DocumentFormat.PDF)
        assert doc.title == "Halcyon MSA"
        assert doc.metadata["author"] == "Ada"
        assert doc.page_count == 2
        assert blocks(doc) == [
            (1, "heading", "Master Services Agreement", 1),
            (1, "heading", "Renewal", 2),
            (1, "paragraph", BODY, None),
            (2, "heading", "Termination", 2),
            (2, "paragraph", "Either party may terminate with notice.", None),
        ]

    def test_outline_defines_headings(self) -> None:
        data = make_pdf(
            [[("Scope of work", 11, False), (BODY, 11, False)]], toc=[[1, "Scope of work", 1]]
        )
        doc = extractor.extract(data, DocumentFormat.PDF)
        assert blocks(doc)[0] == (1, "heading", "Scope of work", 1)

    @pytest.mark.skipif(find_tessdata() is None, reason="Tesseract language data not installed")
    def test_scanned_pages_are_ocrd(self) -> None:
        source = pymupdf.open()
        text_page = source.new_page()
        text_page.insert_text((72, 100), "Termination requires ninety days notice.", fontsize=16)
        image = text_page.get_pixmap(dpi=200)
        scanned = pymupdf.open()
        page = scanned.new_page(width=text_page.rect.width, height=text_page.rect.height)
        page.insert_image(page.rect, pixmap=image)
        doc = extractor.extract(scanned.tobytes(), DocumentFormat.PDF)
        assert doc.pages[0].ocr is True
        assert "ninety days notice" in doc.pages[0].blocks[0].text
        assert "ocr_applied:pages=1" in doc.warnings

    def test_scanned_page_without_ocr_is_flagged(self) -> None:
        source = pymupdf.open()
        image = source.new_page().get_pixmap(dpi=50)
        scanned = pymupdf.open()
        scanned.new_page().insert_image(pymupdf.Rect(0, 0, 100, 100), pixmap=image)
        doc = CompositeExtractor(ocr=OcrOptions(enabled=False)).extract(
            scanned.tobytes(), DocumentFormat.PDF
        )
        assert "no_text_layer:page=1" in doc.warnings

    def test_password_protected(self) -> None:
        pdf = pymupdf.open()
        pdf.new_page().insert_text((72, 72), "secret")
        aes256 = pymupdf.PDF_ENCRYPT_AES_256  # type: ignore[attr-defined]
        data = pdf.tobytes(encryption=aes256, user_pw="u", owner_pw="o")
        with pytest.raises(PermanentProcessingError) as exc:
            extractor.extract(data, DocumentFormat.PDF)
        assert exc.value.code == "encrypted_file"

    def test_corrupt(self) -> None:
        with pytest.raises(PermanentProcessingError) as exc:
            extractor.extract(b"%PDF-1.7\nthis is not really a pdf", DocumentFormat.PDF)
        assert exc.value.code in {"corrupt_file", "empty_document"}

    def test_page_limit(self) -> None:
        data = make_pdf([[("x", 11, False)]] * 3)
        with pytest.raises(PermanentProcessingError) as exc:
            CompositeExtractor(limits=ExtractionLimits(max_pages=2)).extract(
                data, DocumentFormat.PDF
            )
        assert exc.value.code == "too_large"

    def test_running_headers_removed_after_normalization(self) -> None:
        pages = [
            [
                ("ACME CONFIDENTIAL", 9, False),
                (f"Body of page {i}.", 11, False),
                (f"Page {i} of 3", 9, False),
            ]
            for i in range(1, 4)
        ]
        doc = normalize_document(extractor.extract(make_pdf(pages), DocumentFormat.PDF))
        assert [b.text for p in doc.pages for b in p.blocks] == [
            f"Body of page {i}." for i in range(1, 4)
        ]


def make_docx(*, breaks: bool = False) -> bytes:
    d = docx.Document()
    d.core_properties.title = "Vendor Policy"
    d.add_heading("Vendor Policy", level=1)
    d.add_paragraph("Renewals above $50,000 require Legal review.")
    d.add_heading("Approvals", level=2)
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Amount", "Approver"
    table.cell(1, 0).text, table.cell(1, 1).text = "> $50k", "Legal"
    if breaks:
        d.add_page_break()
        d.add_heading("Appendix", level=1)
        d.add_paragraph("Second page text.")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


class TestDocx:
    def test_headings_tables_and_title(self) -> None:
        doc = extractor.extract(make_docx(), DocumentFormat.DOCX)
        assert doc.title == "Vendor Policy"
        assert blocks(doc) == [
            (None, "heading", "Vendor Policy", 1),
            (None, "paragraph", "Renewals above $50,000 require Legal review.", None),
            (None, "heading", "Approvals", 2),
            (None, "table", "Amount | Approver\n> $50k | Legal", None),
        ]
        assert "no_page_numbers" in doc.warnings

    def test_page_breaks_give_page_numbers(self) -> None:
        doc = extractor.extract(make_docx(breaks=True), DocumentFormat.DOCX)
        assert doc.page_count == 2
        assert blocks(doc)[-1] == (2, "paragraph", "Second page text.", None)

    def test_zip_bomb_rejected_before_parsing(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("word/document.xml", b"\0" * (20 * 1024 * 1024))
        with pytest.raises(PermanentProcessingError) as exc:
            extractor.extract(buf.getvalue(), DocumentFormat.DOCX)
        assert exc.value.code == "unsafe_archive"


class TestText:
    def test_markdown_structure(self) -> None:
        md = "\n".join(
            [
                "Guide Title",
                "===========",
                "",
                "Intro paragraph",
                "spanning lines.",
                "",
                "## Setup",
                "```bash",
                "# not a heading",
                "make setup",
                "```",
                "",
                "Sub Section",
                "-----------",
                "Done.",
            ]
        )
        doc = extractor.extract(md.encode(), DocumentFormat.MARKDOWN)
        assert doc.title == "Guide Title"
        assert [(b.kind.value, b.text.split("\n")[0], b.level) for b in doc.pages[0].blocks] == [
            ("heading", "Guide Title", 1),
            ("paragraph", "Intro paragraph", None),
            ("heading", "Setup", 2),
            ("paragraph", "```bash", None),
            ("heading", "Sub Section", 2),
            ("paragraph", "Done.", None),
        ]

    def test_plain_text_paragraphs(self) -> None:
        doc = extractor.extract("﻿First para.\r\n\r\nSecond\r\npara.".encode(), DocumentFormat.TXT)
        assert [b.text for b in doc.pages[0].blocks] == ["First para.", "Second\npara."]
        assert doc.pages[0].page_number is None
        assert doc.pages[0].blocks[0].kind is BlockKind.PARAGRAPH

    def test_size_limit(self) -> None:
        with pytest.raises(PermanentProcessingError):
            CompositeExtractor(limits=ExtractionLimits(max_chars=10)).extract(
                b"x" * 100, DocumentFormat.TXT
            )
