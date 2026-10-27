from __future__ import annotations

from functools import lru_cache

from rag_modules.parsing.csv_parser import CsvParser
from rag_modules.parsing.docx_parser import DocxParser
from rag_modules.parsing.image_parser import ImageParser
from rag_modules.parsing.markdown_parser import MarkdownParser
from rag_modules.parsing.ocr import PaddleOcrEngine
from rag_modules.parsing.pdf_parser import PdfParser
from rag_modules.parsing.registry import ParserRegistry
from rag_modules.parsing.text_parser import TextParser
from rag_modules.parsing.xls_parser import XlsParser
from rag_modules.parsing.xlsx_parser import XlsxParser
from rag_modules.config.settings import settings


@lru_cache(maxsize=1)
def get_parser_registry() -> ParserRegistry:
    """每个进程仅构建一次完整的允许格式解析器分派表。"""
    parser_settings = settings.parser
    ocr_engine = (
        PaddleOcrEngine(
            device=parser_settings.ocr_device,
            language=parser_settings.ocr_language,
            model_dir=parser_settings.ocr_model_dir,
        )
        if parser_settings.ocr_enabled
        else None
    )
    image_parser = ImageParser(ocr_engine, parser_settings=parser_settings)
    return ParserRegistry(
        {
            ".txt": TextParser(),
            ".md": MarkdownParser(),
            ".pdf": PdfParser(ocr_engine, parser_settings=parser_settings),
            ".docx": DocxParser(),
            ".xls": XlsParser(),
            ".xlsx": XlsxParser(),
            ".csv": CsvParser(),
            ".png": image_parser,
            ".jpg": image_parser,
            ".jpeg": image_parser,
        }
    )
