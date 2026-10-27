import io
from pathlib import Path
import sys
import types

import pytest
from pypdf import PdfWriter

from rag_modules.config.settings import ParserSettings
from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError
from rag_modules.parsing.ocr import OcrLine
from rag_modules.parsing.pdf_parser import PdfParser
from rag_modules.parsing.pdf_rendering import PdfPageRenderer


FIXTURE_DIRECTORY = Path(__file__).parents[2] / "fixtures" / "documents"


class RecordingOcrEngine:
    def __init__(self, lines=()):
        self.lines = tuple(lines)
        self.calls = []

    def recognize(self, image):
        self.calls.append(image)
        return self.lines


class FakePageRenderer:
    def render(self, payload, page_number):
        return page_number


def test_scanned_pdf_page_is_rendered_and_ocr_text_is_kept():
    engine = RecordingOcrEngine((OcrLine("扫描页", 0.99, None),))
    payload = (FIXTURE_DIRECTORY / "image-only.pdf").read_bytes()

    parsed = PdfParser(engine, FakePageRenderer()).parse(
        io.BytesIO(payload), ParseContext("d", "scan.pdf")
    )

    assert [block.text for block in parsed.blocks] == ["扫描页"]
    assert parsed.metadata["ocr_page_count"] == 1
    assert parsed.blocks[0].metadata["ocr"] is True
    assert engine.calls == [1]


def test_mixed_pdf_only_ocr_calls_empty_page():
    engine = RecordingOcrEngine((OcrLine("扫描内容", 0.99, None),))
    payload = (FIXTURE_DIRECTORY / "text-and-empty-page.pdf").read_bytes()

    parsed = PdfParser(engine, FakePageRenderer()).parse(
        io.BytesIO(payload), ParseContext("d", "mixed.pdf")
    )

    assert [block.text for block in parsed.blocks] == ["Retained text", "扫描内容"]
    assert engine.calls == [2]


def test_text_pdf_page_does_not_call_ocr():
    engine = RecordingOcrEngine((OcrLine("误识别", 0.99, None),))

    parsed = PdfParser(engine, FakePageRenderer()).parse(
        io.BytesIO((FIXTURE_DIRECTORY / "two-pages.pdf").read_bytes()),
        ParseContext("d", "text.pdf"),
    )

    assert [block.text for block in parsed.blocks] == ["Alphabeta", "Second page"]
    assert engine.calls == []


def test_pdf_renderer_rejects_rendered_page_over_pixel_budget(monkeypatch):
    class FakePage:
        def get_size(self):
            return (100, 100)

    fake_module = types.SimpleNamespace(PdfDocument=lambda payload: [FakePage()])
    monkeypatch.setitem(sys.modules, "pypdfium2", fake_module)
    renderer = PdfPageRenderer(
        b"pdf",
        ParserSettings(ocr_max_image_pixels=1_000),
    )

    with pytest.raises(DocumentParseError) as error:
        renderer.render(b"pdf", 1)

    assert error.value.code == "IMAGE_DIMENSION_LIMIT_EXCEEDED"


def test_pdf_parser_removes_repeated_ocr_headers_and_footers():
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    payload_stream = io.BytesIO()
    writer.write(payload_stream)
    engine = RecordingOcrEngine(
        (
            OcrLine("统一页眉", 0.99, ((0, 10), (100, 10), (100, 20), (0, 20))),
            OcrLine("各页正文", 0.99, ((0, 200), (100, 200), (100, 220), (0, 220))),
            OcrLine("统一页脚", 0.99, ((0, 780), (100, 780), (100, 790), (0, 790))),
        )
    )

    renderer = types.SimpleNamespace(render=lambda payload, page: types.SimpleNamespace(height=800))
    parsed = PdfParser(engine, renderer).parse(
        io.BytesIO(payload_stream.getvalue()),
        ParseContext("d", "repeated.pdf"),
    )

    assert [block.text for block in parsed.blocks] == ["各页正文", "各页正文"]
    assert [block.metadata["page"] for block in parsed.blocks] == [1, 2]
    assert [warning.code for warning in parsed.warnings] == [
        "OCR_REPEATED_HEADER_FOOTER_REMOVED",
        "OCR_REPEATED_HEADER_FOOTER_REMOVED",
    ]


def _blank_pdf(page_count):
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=600, height=800)
    stream = io.BytesIO()
    writer.write(stream)
    stream.seek(0)
    return stream


def test_pdf_keeps_repeated_body_text_in_middle_of_sparse_pages():
    engine = RecordingOcrEngine((OcrLine(
        "同样的正文", 0.99, ((0, 400), (100, 400), (100, 420), (0, 420)),
    ),))
    renderer = types.SimpleNamespace(render=lambda payload, page: types.SimpleNamespace(height=800))

    parsed = PdfParser(engine, renderer).parse(_blank_pdf(2), ParseContext("d", "body.pdf"))

    assert [block.text for block in parsed.blocks] == ["同样的正文", "同样的正文"]
    assert not parsed.warnings


def test_pdf_only_removes_repeated_text_from_actual_page_margins():
    class Engine:
        def recognize(self, page):
            top = 10 if page == 1 else 400
            return (OcrLine("同一段文字", 0.99, ((0, top), (100, top), (100, top + 10), (0, top + 10))),)

    class Renderer:
        def render(self, payload, page):
            return page

    # Without image geometry, sparse pages must also be preserved conservatively.
    parsed = PdfParser(Engine(), Renderer()).parse(_blank_pdf(2), ParseContext("d", "body.pdf"))
    assert [block.text for block in parsed.blocks] == ["同一段文字", "同一段文字"]


def test_repeated_header_text_still_survives_in_another_pages_body():
    class Engine:
        def recognize(self, image):
            top = 10 if image.page < 3 else 400
            lines = [OcrLine("相同文字", 0.99, ((0, top), (100, top), (100, top + 10), (0, top + 10)))]
            if image.page < 3:
                lines.append(OcrLine(f"正文 {image.page}", 0.99, ((0, 400), (100, 400), (100, 410), (0, 410))))
            return tuple(lines)

    renderer = types.SimpleNamespace(render=lambda payload, page: types.SimpleNamespace(height=800, page=page))
    parsed = PdfParser(Engine(), renderer).parse(_blank_pdf(3), ParseContext("d", "body.pdf"))

    assert [block.text for block in parsed.blocks] == ["正文 1", "正文 2", "相同文字"]


def test_pdf_document_line_budget_stops_recognition_after_overflow():
    class Engine:
        def __init__(self):
            self.calls = []

        def recognize(self, page):
            self.calls.append(page)
            return (OcrLine(f"正文 {page}", 0.99),)

    engine = Engine()
    parser = PdfParser(engine, FakePageRenderer(), ParserSettings(ocr_max_lines_per_document=1))
    with pytest.raises(DocumentParseError) as error:
        parser.parse(_blank_pdf(3), ParseContext("d", "budget.pdf"))

    assert error.value.code == "OCR_LINE_LIMIT_EXCEEDED"
    assert engine.calls == [1, 2]
