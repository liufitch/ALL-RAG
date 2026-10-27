from __future__ import annotations

import io
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
from rag_modules.parsing.ocr import OcrEngine, PaddleOcrEngine
from rag_modules.parsing.ocr_cleaning import clean_ocr_lines, normalize_ocr_text
from rag_modules.parsing.pdf_rendering import PdfPageRenderer
from rag_modules.parsing.text_parser import normalize_text
from rag_modules.parsing.warnings import BoundedWarningCollector, parser_warning_summary


class PdfParser:
    """优先提取 PDF 文本层，空白页面可回退到 OCR。"""

    source_type = "pdf"

    def __init__(
        self,
        ocr_engine: OcrEngine | None = None,
        page_renderer: object | None = None,
        parser_settings=None,
    ) -> None:
        self._settings = parser_settings or settings.parser
        self._ocr_engine = ocr_engine or (
            PaddleOcrEngine(
                device=self._settings.ocr_device,
                language=self._settings.ocr_language,
                model_dir=self._settings.ocr_model_dir,
            )
            if self._settings.ocr_enabled
            else None
        )
        self._page_renderer = page_renderer

    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        payload = stream.read()
        reader = _read_pdf(io.BytesIO(payload))
        if reader.is_encrypted and not _decrypt_without_password(reader):
            raise DocumentParseError(
                "PDF_ENCRYPTED", "The PDF is encrypted and cannot be decrypted."
            )

        try:
            page_count = len(reader.pages)
        except (OSError, PdfReadError, ValueError, KeyError, TypeError) as error:
            raise _malformed_pdf_error() from error
        if page_count > self._settings.max_pdf_pages:
            raise DocumentParseError(
                "PDF_PAGE_LIMIT_EXCEEDED",
                "The PDF exceeds the configured page limit.",
            )

        blocks: list[ParsedBlock] = []
        page_results: list[tuple[str, int, object]] = []
        ocr_disabled_pages = 0
        renderer = self._page_renderer
        warnings = BoundedWarningCollector[ParserWarning](
            self._settings.max_warnings_per_document,
            parser_warning_summary,
        )
        ocr_line_count = 0
        ocr_page_heights: dict[int, float] = {}
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                extracted = page.extract_text() or ""
            except (OSError, PdfReadError, ValueError, KeyError, TypeError) as error:
                raise _malformed_pdf_error() from error
            paragraphs = _pdf_paragraphs(extracted)
            if not paragraphs:
                if self._ocr_engine is None and not self._settings.ocr_enabled:
                    ocr_disabled_pages += 1
                    warnings.add(_empty_page_warning(page_number))
                    page_results.append(("empty", page_number, ()))
                    continue
                try:
                    engine = self._ocr_engine
                    if renderer is None:
                        renderer = PdfPageRenderer(payload, self._settings)
                    image = renderer.render(payload, page_number)
                    if getattr(image, "height", 0) > 0:
                        ocr_page_heights[page_number] = float(image.height)
                    lines = engine.recognize(image)
                    ocr_line_count += len(lines)
                    if ocr_line_count > self._settings.ocr_max_lines_per_document:
                        raise DocumentParseError(
                            "OCR_LINE_LIMIT_EXCEEDED",
                            "The OCR result exceeds the configured line limit.",
                        )
                    page_results.append(("ocr", page_number, tuple(lines)))
                except DocumentParseError as error:
                    if error.code == "OCR_LINE_LIMIT_EXCEEDED":
                        raise
                    page_results.append(("ocr_error", page_number, error.code))
                continue
            page_results.append(("text", page_number, tuple(paragraphs)))

        ocr_pages = [
            (page_number, lines)
            for kind, page_number, lines in page_results
            if kind == "ocr"
        ]
        repeated_lines = _repeated_boundary_lines(ocr_pages, ocr_page_heights)
        has_other_page_content = any(
            kind == "text" or (kind == "ocr" and bool(value))
            for kind, _, value in page_results
        )
        ocr_page_count = 0
        for kind, page_number, value in page_results:
            if kind == "text":
                blocks.extend(
                    ParsedBlock("paragraph", paragraph, {"page": page_number})
                    for paragraph in value
                )
                continue
            if kind == "ocr_error":
                if not has_other_page_content:
                    code = str(value)
                    message = (
                        "OCR dependencies are not installed."
                        if code == "OCR_DEPENDENCY_MISSING"
                        else "The OCR engine could not process this PDF."
                    )
                    raise DocumentParseError(code, message, retryable=code == "OCR_FAILED")
                warnings.add(
                    ParserWarning(
                        "OCR_PAGE_FAILED",
                        "OCR could not process one PDF page.",
                        {"page": page_number, "error_code": value},
                    )
                )
                continue
            if kind != "ocr":
                continue
            cleaned = clean_ocr_lines(
                value,
                page_number=page_number,
                settings=self._settings,
                repeated_lines=repeated_lines.get(page_number, set()),
            )
            if not cleaned.blocks:
                warnings.add(_empty_page_warning(page_number))
                continue
            ocr_page_count += 1
            blocks.extend(cleaned.blocks)
            if cleaned.repeated_header_footer_removed:
                warnings.add(
                    ParserWarning(
                        "OCR_REPEATED_HEADER_FOOTER_REMOVED",
                        "Repeated OCR header or footer text was removed.",
                        {"page": page_number},
                    )
                )

        if not blocks:
            if ocr_disabled_pages:
                raise DocumentParseError(
                    "OCR_NOT_ENABLED", "This PDF requires OCR to extract its pages."
                )
            raise DocumentParseError(
                "PDF_NO_EXTRACTABLE_TEXT", "The PDF contains no extractable text."
            )
        return ParsedDocument(
            document_id=context.document_id,
            filename=context.filename,
            source_type=self.source_type,
            blocks=tuple(blocks),
            metadata={
                "page_count": page_count,
                **({"ocr": True, "ocr_page_count": ocr_page_count} if ocr_page_count else {}),
            },
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


def _empty_page_warning(page_number: int) -> ParserWarning:
    return ParserWarning(
        "PDF_EMPTY_PAGE",
        "The PDF page contains no extractable text.",
        {"page": page_number},
    )


def _repeated_boundary_lines(
    pages: list[tuple[int, tuple]], page_heights: dict[int, float]
) -> dict[int, set[str]]:
    """只移除位于页面上下边缘、且跨页重复的短文本。缺少坐标时保留正文。"""
    counts: dict[str, int] = {}
    candidates_by_page: dict[int, set[str]] = {}
    for page_number, lines in pages:
        height = page_heights.get(page_number)
        candidates: set[str] = set()
        if height:
            for line in lines[:1] + lines[-1:]:
                text = normalize_ocr_text(line.text)
                if not text or len(text) > 80 or not line.box:
                    continue
                top = min(point[1] for point in line.box)
                bottom = max(point[1] for point in line.box)
                if bottom <= height * 0.1 or top >= height * 0.9:
                    candidates.add(text)
        candidates_by_page[page_number] = candidates
        for text in candidates:
            counts[text] = counts.get(text, 0) + 1
    repeated = {text for text, count in counts.items() if count >= 2}
    return {page: candidates & repeated for page, candidates in candidates_by_page.items()}
