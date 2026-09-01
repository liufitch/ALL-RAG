from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class ParserWarning:
    """A non-fatal issue encountered while converting a source document."""

    code: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedBlock:
    """A source-level unit produced by a parser."""

    block_type: Literal["paragraph", "heading", "list_item", "code", "table_row"]
    text: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ParsedDocument:
    """The format-independent parser output consumed by later indexing stages."""

    document_id: str
    filename: str
    source_type: str
    blocks: tuple[ParsedBlock, ...]
    metadata: dict[str, Any]
    warnings: tuple[ParserWarning, ...] = ()


class DocumentParseError(ValueError):
    """A client-safe, stable error raised at the parser boundary."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)
