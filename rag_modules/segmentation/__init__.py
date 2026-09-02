"""Parser-output segmentation contracts and implementation."""

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
