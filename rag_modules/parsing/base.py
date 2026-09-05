from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Protocol

from rag_modules.parsing.models import ParsedDocument


@dataclass(frozen=True)
class ParseContext:
    """文档服务在每次调用解析器时提供的身份信息。"""

    document_id: str
    filename: str


class Parser(Protocol):
    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        """将二进制流解析为统一的文档表示。"""
