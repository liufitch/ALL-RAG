from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ParserWarning:
    """转换源文档时遇到的非致命问题。"""

    code: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedBlock:
    """解析器生成的源文档级内容单元。"""

    block_type: Literal["paragraph", "heading", "list_item", "code", "table_row"]
    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ParsedDocument:
    """与文件格式无关的解析结果，供后续索引阶段使用。"""

    document_id: str
    filename: str
    source_type: str
    blocks: tuple[ParsedBlock, ...]
    metadata: dict[str, Any]
    warnings: tuple[ParserWarning, ...] = ()


class DocumentParseError(ValueError):
    """在解析器边界抛出的稳定异常，可安全返回客户端。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)
