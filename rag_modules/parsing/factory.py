from __future__ import annotations

from functools import lru_cache

from rag_modules.parsing.csv_parser import CsvParser
from rag_modules.parsing.docx_parser import DocxParser
from rag_modules.parsing.markdown_parser import MarkdownParser
from rag_modules.parsing.pdf_parser import PdfParser
from rag_modules.parsing.registry import ParserRegistry
from rag_modules.parsing.text_parser import TextParser
from rag_modules.parsing.xls_parser import XlsParser
from rag_modules.parsing.xlsx_parser import XlsxParser


@lru_cache(maxsize=1)
def get_parser_registry() -> ParserRegistry:
    """每个进程仅构建一次完整的允许格式解析器分派表。"""
    return ParserRegistry(
        {
            ".txt": TextParser(),
            ".md": MarkdownParser(),
            ".pdf": PdfParser(),
            ".docx": DocxParser(),
            ".xls": XlsParser(),
            ".xlsx": XlsxParser(),
            ".csv": CsvParser(),
        }
    )
