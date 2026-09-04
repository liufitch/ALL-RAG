"""Local primitives used by the document indexing pipeline."""

from .engine import DocumentIndexingEngine, DocumentIndexingError
from .keywords import KeywordExtractor
from .models import (
    IndexDocumentCommand,
    IndexDocumentResult,
    ProgressReporter,
    SegmentStagingCommand,
    VectorTarget,
    VectorTargetResolver,
)

__all__ = [
    "DocumentIndexingEngine",
    "DocumentIndexingError",
    "IndexDocumentCommand",
    "IndexDocumentResult",
    "KeywordExtractor",
    "ProgressReporter",
    "SegmentStagingCommand",
    "VectorTarget",
    "VectorTargetResolver",
]
