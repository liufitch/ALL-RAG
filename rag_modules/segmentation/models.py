"""Public, storage-free contracts for turning parsed documents into preview segments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from rag_modules.parsing.models import ParserWarning


@dataclass(frozen=True)
class GeneralSegmentationConfig:
    """Explicit options for flat, independently searchable segments."""

    max_chunk_length: int
    overlap: int = 0
    separator: str | None = None
    mode: Literal["general"] = "general"


@dataclass(frozen=True)
class ParentChildSegmentationConfig:
    """Explicit options for parent context records and child retrieval records."""

    parent_mode: Literal["paragraph", "full_document"]
    parent_max_length: int
    child_max_length: int
    child_overlap: int = 0
    separator: str | None = None
    mode: Literal["parent_child"] = "parent_child"


SegmentationConfig: TypeAlias = GeneralSegmentationConfig | ParentChildSegmentationConfig


@dataclass(frozen=True)
class PreviewSegment:
    """A deterministic preview record; persistence and embedding are intentionally absent."""

    local_id: str
    parent_local_id: str | None
    position: int
    content: str
    source_metadata: dict[str, Any]
    index_type: Literal["general", "parent", "child"]


@dataclass(frozen=True)
class SegmentationResult:
    segments: tuple[PreviewSegment, ...]
    warnings: tuple[ParserWarning, ...] = ()


class SegmentationConfigError(ValueError):
    """Stable caller-facing failure for configurations that cannot make progress."""

    def __init__(self, message: str, code: str = "INVALID_SEGMENTATION_CONFIG"):
        self.code = code
        self.message = message
        super().__init__(message)
