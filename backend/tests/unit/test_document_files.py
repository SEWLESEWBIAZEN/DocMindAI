import asyncio
import io
import struct
import zipfile

import pytest

from documind.application.ports import ScannerUnavailableError
from documind.domain.documents.files import (
    DocumentFormat,
    clean_title,
    detect,
    sanitize_filename,
    title_from_filename,
)
from documind.domain.errors import UnsupportedMediaTypeError, ValidationFailedError
from documind.infrastructure.security.malware import ClamAVScanner, NoopMalwareScanner
from documind.infrastructure.storage.s3_provider import content_disposition

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
WORD_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"


def make_docx(
    *, content_type: str = WORD_MAIN, extra: dict[str, bytes] | None = None, document: bool = True
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<Types><Override PartName="/word/document.xml" '
            f'ContentType="{content_type}"/></Types>',
        )
        if document:
            z.writestr("word/document.xml", "<w:document/>")
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def check(data: bytes, name: str) -> DocumentFormat:
    return detect(io.BytesIO(data), name).format


class TestDetection:
    @pytest.mark.parametrize(
        ("data", "name", "fmt"),
        [
            (PDF, "report.pdf", DocumentFormat.PDF),
            (b"\n\n" + PDF, "REPORT.PDF", DocumentFormat.PDF),
            (make_docx(), "contract.docx", DocumentFormat.DOCX),
            ("Héllo wörld ✓\n".encode(), "notes.txt", DocumentFormat.TXT),
            (b"\xef\xbb\xbf# Title\n", "readme.md", DocumentFormat.MARKDOWN),
            (b"# Title", "guide.markdown", DocumentFormat.MARKDOWN),
        ],
    )
    def test_accepts_valid_files(self, data: bytes, name: str, fmt: DocumentFormat) -> None:
        assert check(data, name) is fmt

    @pytest.mark.parametrize(
        "name", ["malware.exe", "page.html", "image.png", "noext", "x.docm", "x.pdf.exe"]
    )
    def test_rejects_unsupported_extensions(self, name: str) -> None:
        with pytest.raises(UnsupportedMediaTypeError):
            check(PDF, name)

    @pytest.mark.parametrize(
        ("data", "name"),
        [
            (b"MZ\x90\x00 windows executable", "invoice.pdf"),  # exe renamed to .pdf
            (make_docx(), "report.pdf"),  # docx renamed to .pdf
            (PDF, "contract.docx"),  # pdf renamed to .docx
            (b"PK\x03\x04 not really a zip", "contract.docx"),
            (make_docx(document=False), "contract.docx"),  # zip without Word parts
            (make_docx(content_type="application/vnd.ms-excel.sheet.main+xml"), "x.docx"),
            ("ascii".encode("utf-16"), "notes.txt"),  # not UTF-8
            (b"\xff\xfe\xfd invalid", "notes.md"),
            (b"text\x00with nul", "notes.txt"),
        ],
    )
    def test_rejects_content_that_does_not_match(self, data: bytes, name: str) -> None:
        with pytest.raises(UnsupportedMediaTypeError):
            check(data, name)

    @pytest.mark.parametrize(
        "docx",
        [
            make_docx(extra={"word/vbaProject.bin": b"macro"}),
            make_docx(content_type="application/vnd.ms-word.document.macroEnabled.main+xml"),
        ],
    )
    def test_rejects_macro_enabled_documents(self, docx: bytes) -> None:
        with pytest.raises(UnsupportedMediaTypeError, match="Macro"):
            check(docx, "contract.docx")

    def test_rejects_empty_file(self) -> None:
        with pytest.raises(ValidationFailedError, match="empty"):
            check(b"", "empty.txt")

    def test_utf8_split_across_chunks(self) -> None:
        data = ("a" * (1024 * 1024 - 1) + "€" * 10).encode()  # "€" straddles the chunk boundary
        assert check(data, "big.txt") is DocumentFormat.TXT

    def test_file_position_is_reset(self) -> None:
        f = io.BytesIO(PDF)
        detect(f, "a.pdf")
        assert f.tell() == 0


class TestFilenames:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("report.pdf", "report.pdf"),
            ("../../etc/passwd.txt", "passwd.txt"),
            ("..\\..\\windows\\system32\\evil.pdf", "evil.pdf"),
            ("/abs/path/file.md", "file.md"),
            ("bad\x00name\r\n.pdf", "badname .pdf"),
            ("tab\tseparated.md", "tab separated.md"),
            ("rtl\u202eexe.pdf", "rtlexe.pdf"),  # bidi override (format char) removed
            ("...hidden.txt", "hidden.txt"),
            ("  spaced   out .pdf ", "spaced out .pdf"),
            ("", "document"),
            (None, "document"),
            ("../", "document"),
        ],
    )
    def test_sanitize(self, raw: str | None, expected: str) -> None:
        assert sanitize_filename(raw) == expected

    def test_long_names_keep_extension(self) -> None:
        name = sanitize_filename("x" * 400 + ".pdf")
        assert len(name) == 255
        assert name.endswith(".pdf")

    def test_titles(self) -> None:
        assert title_from_filename("Q3 Board Report.pdf") == "Q3 Board Report"
        assert clean_title("  Annual\treport  ") == "Annual report"
        with pytest.raises(ValidationFailedError):
            clean_title("   ")


class TestContentDisposition:
    def test_ascii(self) -> None:
        assert (
            content_disposition("report.pdf")
            == "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
        )

    def test_unicode_and_quotes_cannot_break_header(self) -> None:
        header = content_disposition('Rapport "final"; drop.pdf')
        assert header.startswith('attachment; filename="Rapport _final__ drop.pdf"')
        assert "filename*=UTF-8''Rapport%20%22final%22%3B%20drop.pdf" in header
        assert "Résumé" not in content_disposition("Résumé.pdf").split(";")[1]


class TestMalwareScanners:
    async def test_noop(self) -> None:
        result = await NoopMalwareScanner().scan(io.BytesIO(b"x"))
        assert result.clean
        assert result.scanner == "none"

    @staticmethod
    async def _fake_clamd(reply: bytes) -> tuple[asyncio.Server, int, list[bytes]]:
        received: list[bytes] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            assert await reader.readexactly(10) == b"zINSTREAM\x00"
            data = b""
            while True:
                (size,) = struct.unpack(">I", await reader.readexactly(4))
                if size == 0:
                    break
                data += await reader.readexactly(size)
            received.append(data)
            writer.write(reply)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        return server, server.sockets[0].getsockname()[1], received

    @pytest.mark.parametrize(
        ("reply", "clean", "signature"),
        [
            (b"stream: OK\x00", True, None),
            (b"stream: Eicar-Test-Signature FOUND\x00", False, "Eicar-Test-Signature"),
        ],
    )
    async def test_clamav_protocol(self, reply: bytes, clean: bool, signature: str | None) -> None:
        server, port, received = await self._fake_clamd(reply)
        async with server:
            payload = b"A" * 200_000  # several INSTREAM chunks
            result = await ClamAVScanner("127.0.0.1", port, 5).scan(io.BytesIO(payload))
        assert received == [payload]
        assert result.clean is clean
        assert result.signature == signature

    async def test_clamav_error_reply_fails_closed(self) -> None:
        server, port, _ = await self._fake_clamd(b"INSTREAM size limit exceeded. ERROR\x00")
        async with server:
            with pytest.raises(ScannerUnavailableError):
                await ClamAVScanner("127.0.0.1", port, 5).scan(io.BytesIO(b"x"))

    async def test_clamav_unreachable_fails_closed(self) -> None:
        with pytest.raises(ScannerUnavailableError):
            await ClamAVScanner("127.0.0.1", 1, 2).scan(io.BytesIO(b"x"))
