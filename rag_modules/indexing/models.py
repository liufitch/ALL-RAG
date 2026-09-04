"""Immutable commands shared by indexing persistence and later orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class SegmentStagingCommand:
    """The immutable identity/configuration snapshot for one document staging run."""

    dataset_id: str
    dataset_index_id: str
    document_id: str
    indexing_job_id: str
    indexing_technique: Literal["high_quality", "economy"]
    segmentation_mode: Literal["general", "parent_child"]
