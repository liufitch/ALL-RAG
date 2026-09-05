"""将解析后的文档转换为预览分段的公共数据契约，不依赖存储。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from rag_modules.parsing.models import ParserWarning


@dataclass(frozen=True)
class GeneralSegmentationConfig:
    """可独立检索的普通分段所使用的显式配置。"""

    max_chunk_length: int
    overlap: int = 0
    separator: str | None = None
    mode: Literal["general"] = "general"


@dataclass(frozen=True)
class ParentChildSegmentationConfig:
    """父级上下文记录与子级检索记录所使用的显式配置。"""

    parent_mode: Literal["paragraph", "full_document"]
    parent_max_length: int
    child_max_length: int
    child_overlap: int = 0
    separator: str | None = None
    mode: Literal["parent_child"] = "parent_child"


SegmentationConfig: TypeAlias = GeneralSegmentationConfig | ParentChildSegmentationConfig


@dataclass(frozen=True)
class PreviewSegment:
    """结果确定的预览记录，不包含持久化和向量嵌入操作。"""

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
    """配置无法使处理继续推进时，向调用方返回的稳定异常。"""

    def __init__(self, message: str, code: str = "INVALID_SEGMENTATION_CONFIG"):
        self.code = code
        self.message = message
        super().__init__(message)
