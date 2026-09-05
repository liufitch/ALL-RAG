"""解析结果的分段数据契约与实现。"""

from .models import (
    GeneralSegmentationConfig,
    ParentChildSegmentationConfig,
    PreviewSegment,
    SegmentationConfig,
    SegmentationConfigError,
    SegmentationResult,
)
from .segmenter import Segmenter

__all__ = [
    "GeneralSegmentationConfig",
    "ParentChildSegmentationConfig",
    "PreviewSegment",
    "SegmentationConfig",
    "SegmentationConfigError",
    "SegmentationResult",
    "Segmenter",
]
