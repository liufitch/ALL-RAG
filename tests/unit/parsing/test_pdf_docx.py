import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

import rag_modules.parsing.docx_parser as docx_parser_module
from rag_modules.config.settings import settings
from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.docx_parser import DocxParser
from rag_modules.parsing.models import DocumentParseError
from rag_modules.parsing.pdf_parser import PdfParser


FIXTURE_DIRECTORY = Path(__file__).parents[2] / "fixtures" / "documents"


@pytest.fixture
def fixture_bytes():
    def load(name: str) -> bytes:
        return (FIXTURE_DIRECTORY / name).read_bytes()

    return load


def _pdf_with_empty_pages_then_text(empty_pages: int, text_pdf: bytes) -> bytes:
    output = io.BytesIO()
    writer = PdfWriter()
    for _ in range(empty_pages):
        writer.add_blank_page(width=72, height=72)
    writer.add_page(PdfReader(io.BytesIO(text_pdf)).pages[0])
    writer.write(output)
    return output.getvalue()


def test_pdf_parser_preserves_page_numbers_and_normalizes_wrapped_text(fixture_bytes):
    """Dropping page metadata or line-wrap cleanup loses source context."""
    parsed = PdfParser().parse(
        io.BytesIO(fixture_bytes("two-pages.pdf")),
        ParseContext("doc-1", "two-pages.pdf"),
    )

    assert [(block.text, block.metadata) for block in parsed.blocks] == [
        ("Alphabeta", {"page": 1}),
        ("Second page", {"page": 2}),
    ]
    assert parsed.warnings == ()


def test_pdf_parser_warns_for_empty_pages_but_keeps_text_from_other_pages(fixture_bytes):
    """Treating one blank page as document failure loses extractable content."""
    parsed = PdfParser().parse(
        io.BytesIO(fixture_bytes("text-and-empty-page.pdf")),
        ParseContext("doc-1", "mixed.pdf"),
    )

    assert [(block.text, block.metadata) for block in parsed.blocks] == [
        ("Retained text", {"page": 1})
    ]
    assert [(warning.code, warning.metadata) for warning in parsed.warnings] == [
        ("PDF_EMPTY_PAGE", {"page": 2})
    ]


@pytest.mark.parametrize(
    ("empty_pages", "omitted_count"),
    [
        pytest.param(3, 1, id="exact-limit"),
        pytest.param(5, 3, id="over-limit"),
    ],
)
def test_pdf_parser_bounds_empty_page_warnings_and_keeps_later_text(
    monkeypatch, fixture_bytes, empty_pages, omitted_count
):
    """An empty-page run must not bypass the per-document warning cap."""
    monkeypatch.setattr(settings.parser, "max_warnings_per_document", 3)
    payload = _pdf_with_empty_pages_then_text(
        empty_pages, fixture_bytes("two-pages.pdf")
    )

    parsed = PdfParser().parse(io.BytesIO(payload), ParseContext("d", "mixed.pdf"))

    assert [(block.text, block.metadata) for block in parsed.blocks] == [
        ("Alphabeta", {"page": empty_pages + 1})
    ]
    assert [warning.code for warning in parsed.warnings] == [
        "PDF_EMPTY_PAGE",
        "PDF_EMPTY_PAGE",
        "WARNINGS_TRUNCATED",
    ]
    assert [warning.metadata for warning in parsed.warnings[:2]] == [
        {"page": 1},
        {"page": 2},
    ]
    summary = parsed.warnings[-1]
    assert summary.message == "Additional warnings were omitted."
    assert summary.metadata == {"omitted_count": omitted_count}
    assert "page" not in summary.metadata


def test_scanned_pdf_reports_specific_error(fixture_bytes):
    """Returning success for a no-text PDF would conceal the OCR boundary."""
    with pytest.raises(DocumentParseError) as error:
        PdfParser().parse(
            io.BytesIO(fixture_bytes("image-only.pdf")),
            ParseContext("d", "scan.pdf"),
        )

    assert error.value.code == "PDF_NO_EXTRACTABLE_TEXT"
    assert error.value.retryable is False


def test_pdf_parser_rejects_encrypted_documents_that_cannot_be_decrypted(fixture_bytes):
    """Parsing password-protected bytes without a successful decrypt is unsafe."""
    with pytest.raises(DocumentParseError) as error:
        PdfParser().parse(
            io.BytesIO(fixture_bytes("encrypted.pdf")), ParseContext("d", "locked.pdf")
        )

    assert error.value.code == "PDF_ENCRYPTED"
    assert error.value.retryable is False


def test_pdf_parser_reports_corrupt_pdf_with_stable_error(fixture_bytes):
    """Leaking a library-specific read error breaks the parser boundary."""
    with pytest.raises(DocumentParseError) as error:
        PdfParser().parse(
            io.BytesIO(fixture_bytes("corrupt.pdf")), ParseContext("d", "broken.pdf")
        )

    assert error.value.code == "PDF_MALFORMED"
    assert error.value.retryable is False


def test_pdf_parser_enforces_configured_page_limit(monkeypatch, fixture_bytes):
    """Reading pages beyond the configured ceiling permits oversized documents."""
    monkeypatch.setattr(settings.parser, "max_pdf_pages", 1)

    with pytest.raises(DocumentParseError) as error:
        PdfParser().parse(
            io.BytesIO(fixture_bytes("two-pages.pdf")),
            ParseContext("d", "two-pages.pdf"),
        )

    assert error.value.code == "PDF_PAGE_LIMIT_EXCEEDED"
    assert error.value.retryable is False


def test_docx_preserves_headings_lists_tables_and_source_order(fixture_bytes):
    """Walking separate paragraph and table collections would reorder DOCX content."""
    parsed = DocxParser().parse(
        io.BytesIO(fixture_bytes("heading-table.docx")), ParseContext("d", "x.docx")
    )

    assert [(block.block_type, block.text, block.metadata) for block in parsed.blocks] == [
        ("heading", "产品表", {"heading_path": ["产品表"]}),
        ("paragraph", "先写段落", {"heading_path": ["产品表"]}),
        ("table_row", "产品：A；价格：100", {"heading_path": ["产品表"]}),
        ("list_item", "核对库存", {"heading_path": ["产品表"]}),
        ("paragraph", "结束", {"heading_path": ["产品表"]}),
    ]


def test_docx_heading_levels_replace_only_their_same_or_deeper_ancestors(fixture_bytes):
    """Incorrect heading-stack trimming attaches later text to the wrong section."""
    parsed = DocxParser().parse(
        io.BytesIO(fixture_bytes("nested-headings.docx")), ParseContext("d", "nested.docx")
    )

    assert [(block.text, block.metadata["heading_path"]) for block in parsed.blocks] == [
        ("总览", ["总览"]),
        ("范围", ["总览", "范围"]),
        ("范围内容", ["总览", "范围"]),
        ("后续", ["总览", "后续"]),
        ("后续内容", ["总览", "后续"]),
    ]


def test_docx_parser_normalizes_malformed_ooxml_inside_a_valid_zip(fixture_bytes):
    """Leaking lxml XML errors would violate the parser's stable error boundary."""
    with pytest.raises(DocumentParseError) as error:
        DocxParser().parse(
            io.BytesIO(fixture_bytes("malformed-ooxml.docx")),
            ParseContext("d", "malformed.docx"),
        )

    assert error.value.code == "DOCX_MALFORMED"
    assert error.value.retryable is False


def test_docx_parser_does_not_mislabel_internal_extraction_type_errors(
    monkeypatch, fixture_bytes
):
    """A formatter or body-walk defect must remain debuggable, not look malformed."""

    def fail_body_extraction(_document):
        raise TypeError("injected extraction bug")

    monkeypatch.setattr(docx_parser_module, "_body_blocks", fail_body_extraction)

    with pytest.raises(TypeError, match="injected extraction bug"):
        DocxParser().parse(
            io.BytesIO(fixture_bytes("heading-table.docx")), ParseContext("d", "x.docx")
        )
