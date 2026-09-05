from __future__ import annotations

from typing import BinaryIO, Mapping

from rag_modules.parsing.base import ParseContext, Parser
from rag_modules.parsing.models import DocumentParseError, ParsedDocument
from rag_modules.upload_formats import SUPPORTED_UPLOAD_EXTENSIONS


class ParserRegistry:
    """根据允许的扩展名范围，将源数据流分派给对应解析器。"""

    def __init__(self, parsers: Mapping[str, Parser]):
        self._parsers = {
            self._normalize_extension(extension): parser
            for extension, parser in parsers.items()
        }

    @property
    def registered_extensions(self) -> set[str]:
        """返回已配置解析器分派表的副本，供诊断使用。"""
        return set(self._parsers)

    @staticmethod
    def _normalize_extension(extension: str) -> str:
        normalized = extension.strip().lower()
        if not normalized.startswith("."):
            normalized = f".{normalized}"
        return normalized

    def parse(
        self, extension: str, stream: BinaryIO, context: ParseContext
    ) -> ParsedDocument:
        normalized_extension = self._normalize_extension(extension)
        if normalized_extension not in SUPPORTED_UPLOAD_EXTENSIONS:
            raise DocumentParseError(
                "UNSUPPORTED_FILE_TYPE",
                f"The file extension {normalized_extension!r} is not supported.",
            )

        parser = self._parsers.get(normalized_extension)
        if parser is None:
            raise DocumentParseError(
                "UNSUPPORTED_FILE_TYPE",
                f"No parser is registered for {normalized_extension!r}.",
            )

        stream.seek(0)
        parsed = parser.parse(stream, context)
        if not parsed.blocks:
            raise DocumentParseError(
                "NO_EXTRACTABLE_TEXT",
                "The document contains no extractable text.",
            )
        return parsed
