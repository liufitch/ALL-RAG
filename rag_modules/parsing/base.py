from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Protocol

from rag_modules.parsing.models import ParsedDocument


@dataclass(frozen=True)
class ParseContext:
    """Identity supplied by the document service to every parser invocation."""

    document_id: str
    filename: str


class Parser(Protocol):
    def parse(self, stream: BinaryIO, context: ParseContext) -> ParsedDocument:
        """Parse a binary stream into the unified document representation."""
