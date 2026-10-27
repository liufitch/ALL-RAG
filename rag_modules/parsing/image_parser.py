from __future__ import annotations

from collections.abc import Callable
from typing import BinaryIO

from rag_modules.config.settings import ParserSettings, settings as app_settings
from rag_modules.parsing.base import ParseContext
from rag_modules.parsing.models import DocumentParseError, ParsedBlock, ParsedDocument
from rag_modules.parsing.ocr import OcrEngine, PaddleOcrEngine
from rag_modules.parsing.ocr_cleaning import clean_ocr_lines


class ImageParser:
    """解码单页图片，经 OCR 和标准清洗后生成统一文档块。"""

    source_type = "image"

    def __init__(
        self,
        engine: OcrEngine | None = None,
        *,
        decoder: Callable[[BinaryIO], object] | None = None,
        parser_settings: ParserSettings | None = None,
        settings: ParserSettings | None = None,
    ) -> None:
        resolved_settings = parser_settings or settings or app_settings.parser
        self._engine = engine or (
            PaddleOcrEngine(
                device=resolved_settings.ocr_device,
                language=resolved_settings.ocr_language,
                model_dir=resolved_settings.ocr_model_dir,
            )
            if resolved_settings.ocr_enabled
            else None
        )
        self._decoder = decoder
        self._settings = resolved_settings

    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        if not self._settings.ocr_enabled and self._engine is None:
            raise DocumentParseError("OCR_NOT_ENABLED", "OCR is not enabled.")
        try:
            image = (
                self._decoder(stream)
                if self._decoder is not None
                else _decode_image(stream, self._settings)
            )
            width = int(getattr(image, "width"))
            height = int(getattr(image, "height"))
        except DocumentParseError:
            raise
        except Exception as error:
            raise DocumentParseError(
                "IMAGE_MALFORMED", "The image cannot be decoded."
            ) from error

        if (
            width < 1
            or height < 1
            or width > self._settings.ocr_max_image_width
            or height > self._settings.ocr_max_image_height
            or width * height > self._settings.ocr_max_image_pixels
        ):
            raise DocumentParseError(
                "IMAGE_DIMENSION_LIMIT_EXCEEDED",
                "The image exceeds the configured dimension limit.",
            )

        engine = self._engine or PaddleOcrEngine(
            device=self._settings.ocr_device,
            language=self._settings.ocr_language,
            model_dir=self._settings.ocr_model_dir,
        )
        lines = engine.recognize(image)
        if len(lines) > self._settings.ocr_max_lines_per_document:
            raise DocumentParseError(
                "OCR_LINE_LIMIT_EXCEEDED",
                "The OCR result exceeds the configured line limit.",
            )
        cleaned = clean_ocr_lines(
            lines,
            page_number=1,
            settings=self._settings,
            repeated_lines=set(),
        )
        if not cleaned.blocks:
            raise DocumentParseError(
                "OCR_NO_EXTRACTABLE_TEXT", "OCR did not find usable text."
            )

        blocks = tuple(
            ParsedBlock(
                block.block_type,
                block.text,
                {**block.metadata, "image_width": width, "image_height": height},
            )
            for block in cleaned.blocks
        )
        return ParsedDocument(
            document_id=context.document_id,
            filename=context.filename,
            source_type=self.source_type,
            blocks=blocks,
            metadata={
                "ocr": True,
                "ocr_page_count": 1,
                "ocr_filtered_line_count": cleaned.filtered_line_count,
                "image_width": width,
                "image_height": height,
            },
        )


def _decode_image(stream: BinaryIO, parser_settings: ParserSettings) -> object:
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as error:
        raise DocumentParseError(
            "OCR_DEPENDENCY_MISSING", "Image decoding dependencies are not installed."
        ) from error

    try:
        with Image.open(stream) as source:
            width, height = source.size
            if (
                width > parser_settings.ocr_max_image_width
                or height > parser_settings.ocr_max_image_height
                or width * height > parser_settings.ocr_max_image_pixels
            ):
                raise DocumentParseError(
                    "IMAGE_DIMENSION_LIMIT_EXCEEDED",
                    "The image exceeds the configured dimension limit.",
                )
            source.load()
            return source.convert("RGB")
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as error:
        raise DocumentParseError("IMAGE_MALFORMED", "The image cannot be decoded.") from error
