from __future__ import annotations

import re
from typing import BinaryIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from rag_modules.config.settings import settings
from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import (
    DocumentParseError,
    ParsedBlock,
    ParsedDocument,
    ParserWarning,
)
from rag_modules.parsing.text_parser import normalize_text
from rag_modules.parsing.warnings import BoundedWarningCollector, parser_warning_summary


class PdfParser:
    """Extract text-layer paragraphs from a PDF without OCR."""

    source_type = "pdf"

    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        reader = _read_pdf(stream)
        if reader.is_encrypted and not _decrypt_without_password(reader):
            raise DocumentParseError(
                "PDF_ENCRYPTED", "The PDF is encrypted and cannot be decrypted."
            )

        try:
            page_count = len(reader.pages)
        except (OSError, PdfReadError, ValueError, KeyError, TypeError) as error:
            raise _malformed_pdf_error() from error
        if page_count > settings.parser.max_pdf_pages:
            raise DocumentParseError(
                "PDF_PAGE_LIMIT_EXCEEDED",
                "The PDF exceeds the configured page limit.",
            )

        blocks: list[ParsedBlock] = []
        warnings = BoundedWarningCollector[ParserWarning](
            settings.parser.max_warnings_per_document,
            parser_warning_summary,
        )
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                extracted = page.extract_text() or ""
            except (OSError, PdfReadError, ValueError, KeyError, TypeError) as error:
                raise _malformed_pdf_error() from error
            paragraphs = _pdf_paragraphs(extracted)
            if not paragraphs:
                warnings.add(
                    ParserWarning(
                        "PDF_EMPTY_PAGE",
                        "The PDF page contains no extractable text.",
                        {"page": page_number},
                    )
                )
                continue
            blocks.extend(
                ParsedBlock("paragraph", paragraph, {"page": page_number})
                for paragraph in paragraphs
            )

        if not blocks:
            raise DocumentParseError(
                "PDF_NO_EXTRACTABLE_TEXT", "The PDF contains no extractable text."
            )
        return ParsedDocument(
            document_id=context.document_id,
            filename=context.filename,
            source_type=self.source_type,
            blocks=tuple(blocks),
            metadata={"page_count": page_count},
            warnings=warnings.result(),
        )


def _read_pdf(stream: BinaryIO) -> PdfReader:
    try:
        return PdfReader(stream, strict=False)
    except (OSError, PdfReadError, ValueError, KeyError, TypeError) as error:
        raise _malformed_pdf_error() from error


def _decrypt_without_password(reader: PdfReader) -> bool:
    try:
        return bool(reader.decrypt(""))
    except (OSError, PdfReadError, ValueError, KeyError, TypeError):
        return False


def _pdf_paragraphs(text: str) -> list[str]:
    return [
        paragraph
        for raw_paragraph in re.split(r"\n[ \t]*\n", normalize_text(text))
        if (paragraph := _normalize_pdf_paragraph(raw_paragraph))
    ]


def _normalize_pdf_paragraph(paragraph: str) -> str:
    lines = [line.strip() for line in paragraph.split("\n") if line.strip()]
    joined: list[str] = []
    for line in lines:
        if joined and joined[-1].endswith("-"):
            joined[-1] = joined[-1][:-1] + line
        else:
            joined.append(line)
    return re.sub(r"[ \t]+", " ", " ".join(joined)).strip()


def _malformed_pdf_error() -> DocumentParseError:
    return DocumentParseError("PDF_MALFORMED", "The PDF is malformed or unreadable.")
