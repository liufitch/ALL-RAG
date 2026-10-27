import io

import pytest

from rag_modules.config.settings import ParserSettings
from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.image_parser import ImageParser
from rag_modules.parsing.models import DocumentParseError
from rag_modules.parsing.ocr import OcrLine


PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


class FakeOcrEngine:
    def __init__(self, lines):
        self.lines = tuple(lines)

    def recognize(self, image):
        return self.lines


class FakeImage:
    width = 120
    height = 80


def test_image_parser_decodes_png_and_emits_ocr_metadata():
    parsed = ImageParser(
        FakeOcrEngine((OcrLine("hello", 0.98, None),)),
        decoder=lambda stream: FakeImage(),
    ).parse(io.BytesIO(PNG_BYTES), ParseContext("doc-1", "scan.png"))

    assert parsed.blocks[0].text == "hello"
    assert parsed.blocks[0].metadata["ocr"] is True
    assert parsed.metadata["ocr_page_count"] == 1
    assert parsed.metadata["image_width"] == 120


def test_image_parser_rejects_pixel_budget_overflow():
    settings = ParserSettings(ocr_max_image_pixels=100)

    with pytest.raises(DocumentParseError) as error:
        ImageParser(
            FakeOcrEngine((OcrLine("hello", 0.98, None),)),
            decoder=lambda stream: FakeImage(),
            settings=settings,
        ).parse(io.BytesIO(PNG_BYTES), ParseContext("doc-1", "scan.png"))

    assert error.value.code == "IMAGE_DIMENSION_LIMIT_EXCEEDED"


def test_image_parser_reports_no_usable_ocr_text():
    with pytest.raises(DocumentParseError) as error:
        ImageParser(
            FakeOcrEngine((OcrLine("...", 0.99, None),)),
            decoder=lambda stream: FakeImage(),
        ).parse(io.BytesIO(PNG_BYTES), ParseContext("doc-1", "scan.png"))

    assert error.value.code == "OCR_NO_EXTRACTABLE_TEXT"


def test_image_parser_requires_ocr_to_be_enabled_when_no_engine_is_injected():
    with pytest.raises(DocumentParseError) as error:
        ImageParser(
            decoder=lambda stream: FakeImage(),
            settings=ParserSettings(ocr_enabled=False),
        ).parse(io.BytesIO(PNG_BYTES), ParseContext("doc-1", "scan.png"))

    assert error.value.code == "OCR_NOT_ENABLED"
