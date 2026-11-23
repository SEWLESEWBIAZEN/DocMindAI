"""Document extractors (blocking; run them in a worker thread).

Each returns an ExtractedDocument with page numbers, headings, metadata and
warnings. Inputs are untrusted: sizes, page counts and archive expansion are
bounded, and malformed files raise PermanentProcessingError.
"""

from __future__ import annotations

import glob
import io
import logging
import re
import statistics
import zipfile
from dataclasses import dataclass
from typing import Any, Protocol

import docx
import pymupdf
from docx.document import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph

from documind.domain.documents.files import DocumentFormat
from documind.domain.processing.errors import PermanentProcessingError
from documind.domain.processing.model import Block, BlockKind, ExtractedDocument, ExtractedPage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    max_pages: int = 2000
    max_chars: int = 20_000_000
    docx_max_uncompressed_bytes: int = 250 * 1024 * 1024
    docx_max_compression_ratio: int = 100
    docx_max_entries: int = 5000


@dataclass(frozen=True, slots=True)
class OcrOptions:
    enabled: bool = True
    languages: str = "eng"
    dpi: int = 300
    # Pages with fewer extracted characters than this (and with images) are OCR'd.
    min_chars_per_page: int = 25
    tessdata_dir: str | None = None


def find_tessdata() -> str | None:
    matches = sorted(glob.glob("/usr/share/tesseract-ocr/*/tessdata")) + sorted(
        glob.glob("/usr/share/tessdata")
    )
    return matches[-1] if matches else None


def _check_size(chars: int, limits: ExtractionLimits) -> None:
    if chars > limits.max_chars:
        raise PermanentProcessingError(
            "The document contains too much text to process.", code="too_large"
        )


# --------------------------------------------------------------------------- PDF


class PdfExtractor:
    def __init__(self, limits: ExtractionLimits, ocr: OcrOptions) -> None:
        self._limits = limits
        self._ocr = ocr
        self._tessdata = ocr.tessdata_dir or find_tessdata()

    def extract(self, data: bytes) -> ExtractedDocument:
        try:
            pdf = pymupdf.open(stream=data, filetype="pdf")  # type: ignore[no-untyped-call]
        except Exception as exc:
            raise PermanentProcessingError(
                "The PDF is damaged and can't be read.", code="corrupt_file"
            ) from exc
        with pdf:
            if pdf.needs_pass:
                raise PermanentProcessingError(
                    "The PDF is password-protected. Upload an unlocked copy.", code="encrypted_file"
                )
            if pdf.page_count == 0:
                raise PermanentProcessingError("The PDF has no pages.", code="empty_document")
            if pdf.page_count > self._limits.max_pages:
                raise PermanentProcessingError(
                    f"The PDF has more than {self._limits.max_pages} pages.", code="too_large"
                )
            return self._extract(pdf)

    def _extract(self, pdf: Any) -> ExtractedDocument:
        warnings: list[str] = []
        toc = {
            (int(page), self._key(title)): int(level)
            for level, title, page in pdf.get_toc(simple=True)
        }
        raw_pages = [self._page_blocks(page) for page in pdf]
        body_size = self._body_font_size(raw_pages)
        sizes = sorted(
            {
                round(b["size"], 1)
                for page in raw_pages
                for b in page
                if b["size"] > body_size * 1.15
            },
            reverse=True,
        )

        pages: list[ExtractedPage] = []
        ocr_failed = False
        total_chars = 0
        for index, page in enumerate(pdf):
            number = index + 1
            blocks_raw = raw_pages[index]
            text_chars = sum(len(b["text"].strip()) for b in blocks_raw)
            ocr_used = False
            if text_chars < self._ocr.min_chars_per_page and page.get_images():
                if self._ocr.enabled and self._tessdata:
                    try:
                        textpage = page.get_textpage_ocr(
                            language=self._ocr.languages,
                            dpi=self._ocr.dpi,
                            full=True,
                            tessdata=self._tessdata,
                        )
                        blocks_raw = self._page_blocks(page, textpage=textpage)
                        ocr_used = True
                    except Exception:
                        logger.warning("OCR failed on page", extra={"page": number}, exc_info=True)
                        ocr_failed = True
                elif text_chars == 0:
                    warnings.append(f"no_text_layer:page={number}")
            blocks: list[Block] = []
            for raw in blocks_raw:
                text = raw["text"].strip()
                if not text:
                    continue
                level = toc.get((number, self._key(text)))
                if level is None and not ocr_used:
                    level = self._heading_level(raw, body_size, sizes)
                kind = BlockKind.HEADING if level else BlockKind.PARAGRAPH
                blocks.append(Block(kind, text, level))
                total_chars += len(text)
            _check_size(total_chars, self._limits)
            pages.append(
                ExtractedPage(
                    position=number, page_number=number, blocks=tuple(blocks), ocr=ocr_used
                )
            )

        if not self._tessdata and self._ocr.enabled:
            warnings.append("ocr_unavailable")
        if ocr_failed:
            warnings.append("ocr_failed_on_some_pages")
        doc = ExtractedDocument(
            title=(pdf.metadata or {}).get("title") or None,
            pages=tuple(pages),
            metadata=self._metadata(pdf.metadata or {}),
            warnings=tuple(warnings),
        )
        if doc.ocr_pages:
            doc = ExtractedDocument(
                title=doc.title,
                pages=doc.pages,
                metadata=doc.metadata,
                warnings=(*doc.warnings, f"ocr_applied:pages={','.join(map(str, doc.ocr_pages))}"),
            )
        return doc

    @staticmethod
    def _key(text: str) -> str:
        return re.sub(r"\W+", " ", text).strip().lower()

    @staticmethod
    def _page_blocks(page: Any, textpage: Any = None) -> list[dict[str, Any]]:
        """Text blocks in reading order with their dominant font size and boldness."""
        data = page.get_text("dict", sort=True, textpage=textpage)
        out = []
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            lines, sizes, bold_chars, chars = [], [], 0, 0
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                lines.append("".join(span.get("text", "") for span in spans))
                for span in spans:
                    n = len(span.get("text", "").strip())
                    chars += n
                    sizes.extend([float(span.get("size", 0))] * max(n, 1))
                    if span.get("flags", 0) & 16:  # bold
                        bold_chars += n
            text = "\n".join(lines)
            out.append(
                {
                    "text": text,
                    "size": statistics.median(sizes) if sizes else 0.0,
                    "bold": chars > 0 and bold_chars / chars > 0.6,
                    "lines": len(lines),
                }
            )
        return out

    @staticmethod
    def _body_font_size(pages: list[list[dict[str, Any]]]) -> float:
        weighted: list[float] = []
        for page in pages[:50]:
            for block in page:
                weighted.extend([block["size"]] * max(1, len(block["text"]) // 20))
        return statistics.median(weighted) if weighted else 11.0

    @staticmethod
    def _heading_level(raw: dict[str, Any], body: float, sizes: list[float]) -> int | None:
        text = raw["text"].strip()
        if raw["lines"] > 3 or len(text) > 150 or text.endswith((".", ",", ";", ":")):
            return None
        size = round(raw["size"], 1)
        if size in sizes:
            return min(sizes.index(size) + 1, 6)
        if raw["bold"] and raw["size"] >= body * 0.98 and len(text) <= 100:
            return min(len(sizes) + 1, 6)
        return None

    @staticmethod
    def _metadata(meta: dict[str, Any]) -> dict[str, str]:
        keep = {"author", "subject", "keywords", "creator", "producer", "creationDate", "modDate"}
        return {k: str(v)[:500] for k, v in meta.items() if k in keep and v}


# -------------------------------------------------------------------------- DOCX


class DocxExtractor:
    def __init__(self, limits: ExtractionLimits) -> None:
        self._limits = limits

    def _check_archive(self, data: bytes) -> None:
        """Reject zip bombs before python-docx decompresses anything."""
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
        except zipfile.BadZipFile as exc:
            raise PermanentProcessingError(
                "The DOCX file is damaged.", code="corrupt_file"
            ) from exc
        total = sum(i.file_size for i in infos)
        compressed = max(1, sum(i.compress_size for i in infos))
        if (
            len(infos) > self._limits.docx_max_entries
            or total > self._limits.docx_max_uncompressed_bytes
            or total / compressed > self._limits.docx_max_compression_ratio
        ):
            raise PermanentProcessingError(
                "The DOCX file expands to an unsafe size.", code="unsafe_archive"
            )

    def extract(self, data: bytes) -> ExtractedDocument:
        self._check_archive(data)
        try:
            document: DocxDocument = docx.Document(io.BytesIO(data))
        except Exception as exc:
            raise PermanentProcessingError(
                "The DOCX file is damaged.", code="corrupt_file"
            ) from exc

        pages: list[list[Block]] = [[]]
        page_breaks = 0
        total_chars = 0
        for item in document.iter_inner_content():
            if isinstance(item, Paragraph):
                text = item.text.strip()
                if text:
                    pages[-1].append(self._paragraph_block(item, text))
                    total_chars += len(text)
                # Word records where it last broke pages; hard page breaks too.
                breaks = len(item.rendered_page_breaks) + self._hard_breaks(item)
                for _ in range(breaks):
                    pages.append([])
                    page_breaks += 1
            elif isinstance(item, Table):
                text = self._table_text(item)
                if text:
                    pages[-1].append(Block(BlockKind.TABLE, text))
                    total_chars += len(text)
            _check_size(total_chars, self._limits)

        paginated = page_breaks > 0
        extracted = [
            ExtractedPage(
                position=i + 1, page_number=(i + 1) if paginated else None, blocks=tuple(blocks)
            )
            for i, blocks in enumerate(pages)
            if blocks or paginated
        ]
        props = document.core_properties
        metadata = {
            k: str(v)[:500]
            for k, v in {
                "author": props.author,
                "subject": props.subject,
                "keywords": props.keywords,
            }.items()
            if v
        }
        warnings = ("page_numbers_from_word_layout",) if paginated else ("no_page_numbers",)
        return ExtractedDocument(
            title=(props.title or None),
            pages=tuple(extracted),
            metadata=metadata,
            warnings=warnings,
        )

    @staticmethod
    def _paragraph_block(paragraph: Paragraph, text: str) -> Block:
        style = (paragraph.style.name if paragraph.style is not None else "") or ""
        match = re.match(r"Heading (\d)", style)
        if match:
            return Block(BlockKind.HEADING, text, int(match.group(1)))
        if style == "Title":
            return Block(BlockKind.HEADING, text, 1)
        return Block(BlockKind.PARAGRAPH, text)

    @staticmethod
    def _hard_breaks(paragraph: Paragraph) -> int:
        return sum(
            1
            for br in paragraph._p.iter(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}br"
            )
            if br.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type")
            == "page"
        )

    @staticmethod
    def _table_text(table: Table) -> str:
        rows = []
        for row in table.rows:
            cells: list[str] = []
            for cell in row.cells:
                value = " ".join(cell.text.split())
                if not cells or cells[-1] != value:  # merged cells repeat their text
                    cells.append(value)
            line = " | ".join(cells).strip(" |")
            if line:
                rows.append(line)
        return "\n".join(rows)


# ---------------------------------------------------------------- TXT / Markdown

_ATX = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^(```|~~~)")


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PermanentProcessingError(
            "The text file isn't valid UTF-8.", code="corrupt_file"
        ) from exc


class TextExtractor:
    def __init__(self, limits: ExtractionLimits, *, markdown: bool) -> None:
        self._limits = limits
        self._markdown = markdown

    def extract(self, data: bytes) -> ExtractedDocument:
        text = _decode(data).replace("\r\n", "\n").replace("\r", "\n")
        _check_size(len(text), self._limits)
        blocks = self._markdown_blocks(text) if self._markdown else self._paragraphs(text)
        title = next((b.text for b in blocks if b.kind is BlockKind.HEADING), None)
        return ExtractedDocument(
            title=title,
            pages=(ExtractedPage(position=1, page_number=None, blocks=tuple(blocks)),),
            warnings=("no_page_numbers",),
        )

    @staticmethod
    def _paragraphs(text: str) -> list[Block]:
        return [
            Block(BlockKind.PARAGRAPH, p.strip()) for p in re.split(r"\n\s*\n", text) if p.strip()
        ]

    @staticmethod
    def _markdown_blocks(text: str) -> list[Block]:
        blocks: list[Block] = []
        buffer: list[str] = []
        in_fence = False

        def flush() -> None:
            if buffer and "".join(buffer).strip():
                blocks.append(Block(BlockKind.PARAGRAPH, "\n".join(buffer).strip()))
            buffer.clear()

        lines = text.split("\n")
        for i, line in enumerate(lines):
            if _FENCE.match(line.strip()):
                in_fence = not in_fence
                buffer.append(line)
                continue
            if in_fence:
                buffer.append(line)
                continue
            atx = _ATX.match(line)
            if atx:
                flush()
                blocks.append(Block(BlockKind.HEADING, atx.group(2).strip(), len(atx.group(1))))
                continue
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if line.strip() and re.fullmatch(r"\s*(=+|-+)\s*", nxt) and not buffer:
                blocks.append(Block(BlockKind.HEADING, line.strip(), 1 if "=" in nxt else 2))
                continue
            if (
                re.fullmatch(r"\s*(=+|-+)\s*", line)
                and blocks
                and blocks[-1].kind is BlockKind.HEADING
            ):
                continue  # setext underline already consumed
            if not line.strip():
                flush()
            else:
                buffer.append(line)
        flush()
        return blocks


# --------------------------------------------------------------------- composite


class _Extractor(Protocol):
    def extract(self, data: bytes) -> ExtractedDocument: ...


class CompositeExtractor:
    """Selects the extractor for a validated document format."""

    def __init__(
        self, limits: ExtractionLimits | None = None, ocr: OcrOptions | None = None
    ) -> None:
        limits = limits or ExtractionLimits()
        self._by_format: dict[DocumentFormat, _Extractor] = {
            DocumentFormat.PDF: PdfExtractor(limits, ocr or OcrOptions()),
            DocumentFormat.DOCX: DocxExtractor(limits),
            DocumentFormat.TXT: TextExtractor(limits, markdown=False),
            DocumentFormat.MARKDOWN: TextExtractor(limits, markdown=True),
        }

    def extract(self, data: bytes, fmt: DocumentFormat) -> ExtractedDocument:
        return self._by_format[fmt].extract(data)
