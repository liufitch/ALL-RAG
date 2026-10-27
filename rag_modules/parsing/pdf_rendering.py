from __future__ import annotations

import math

from rag_modules.config.settings import ParserSettings
from rag_modules.parsing.models import DocumentParseError


class PdfPageRenderer:
    """使用可选 pypdfium2 将 PDF 页面转换为 OCR 图片。"""

    def __init__(self, payload: bytes, settings: ParserSettings) -> None:
        self._payload = payload
        self._settings = settings
        self._document = None

    def render(self, payload: bytes, page_number: int) -> object:
        del payload
        try:
            import pypdfium2 as pdfium
        except ImportError as error:
            raise DocumentParseError(
                "OCR_DEPENDENCY_MISSING", "PDF OCR rendering dependencies are not installed."
            ) from error

        try:
            if self._document is None:
                self._document = pdfium.PdfDocument(self._payload)
            page = self._document[page_number - 1]
            scale = self._settings.ocr_pdf_render_dpi / 72
            width_points, height_points = page.get_size()
            width = math.ceil(width_points * scale)
            height = math.ceil(height_points * scale)
            if (
                width < 1
                or height < 1
                or width > self._settings.ocr_max_image_width
                or height > self._settings.ocr_max_image_height
                or width * height > self._settings.ocr_max_image_pixels
            ):
                raise DocumentParseError(
                    "IMAGE_DIMENSION_LIMIT_EXCEEDED",
                    "The rendered PDF page exceeds the configured dimension limit.",
                )
            bitmap = page.render(scale=scale)
            return bitmap.to_pil()
        except DocumentParseError:
            raise
        except Exception as error:
            raise DocumentParseError(
                "PDF_RENDER_FAILED", "The PDF page could not be rendered for OCR."
            ) from error
