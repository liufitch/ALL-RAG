"""Transaction-neutral persistence for deterministic staged document segments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from rag_modules.common import utcnow
from rag_modules.db.models import DocumentSegmentRecord
from rag_modules.indexing.ids import (
    normalize_segment_content,
    normalized_source_metadata,
    segment_content_hash,
    stable_segment_id,
)
from rag_modules.indexing.models import SegmentStagingCommand
from rag_modules.segmentation.models import PreviewSegment


class SegmentPersistenceError(ValueError):
    """A safe validation/conflict failure raised before unsafe segment mutation."""


@dataclass(frozen=True, slots=True)
class _Candidate:
    preview: PreviewSegment
    parent_id: str | None
    content: str
    source_metadata: dict
    content_hash: str
    id: str
    embedding_status: str


class SegmentRepository:
    """Stage, activate, and retire document segments without committing a transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def stage(
        self,
        command: SegmentStagingCommand,
        segments: tuple[PreviewSegment, ...] | list[PreviewSegment],
    ) -> list[DocumentSegmentRecord]:
        """Flush deterministic staging records, returning retries without mutation.

        All preview and parent-link validation happens before the first database
        mutation. This method deliberately does not commit: the caller owns the
        later vector-write, activation, and index-version-switch transaction.
        """
        self._validate_command(command)
        candidates = self._build_candidates(command, tuple(segments))
        if not candidates:
            return []

        existing_by_id = await self._load_by_id(candidates)
        records = self._exact_existing_records(command, candidates, existing_by_id)
        missing = [candidate for candidate in candidates if candidate.id not in existing_by_id]
        if not missing:
            return records

        await self.session.execute(self._insert_missing_statement(command, missing))
        existing_by_id = await self._load_by_id(candidates)
        records = self._exact_existing_records(command, candidates, existing_by_id)
        if len(records) != len(candidates):  # pragma: no cover - conflict writes are statement-visible
            raise SegmentPersistenceError("A deterministic segment ID could not be staged safely.")
        return records

    async def activate_document_segments(
        self, *, dataset_id: str, dataset_index_id: str, document_id: str
    ) -> list[DocumentSegmentRecord]:
        """Mark only currently staged records for one document/index version completed."""
        records = await self._records_for_scope(
            dataset_id=dataset_id,
            dataset_index_id=dataset_index_id,
            document_id=document_id,
            status="indexing",
        )
        timestamp = utcnow()
        for record in records:
            record.status = "completed"
            record.updated_at = timestamp
        await self.session.flush()
        return records

    async def update_keywords(
        self,
        *,
        dataset_index_id: str,
        document_id: str,
        keywords_by_segment_id: Mapping[str, Sequence[str]],
    ) -> None:
        """Flush economy keywords for exactly the submitted staged records."""
        if not keywords_by_segment_id:
            return
        records = await self._exact_mutation_records(
            dataset_index_id=dataset_index_id,
            document_id=document_id,
            segment_ids=tuple(keywords_by_segment_id),
        )
        validated: dict[str, list[str]] = {}
        for record in records:
            keywords = keywords_by_segment_id[record.id]
            if (
                record.index_type != "general"
                or isinstance(keywords, (str, bytes))
                or len(keywords) > 100
                or any(
                    not isinstance(keyword, str)
                    or not keyword
                    or len(keyword) > 255
                    for keyword in keywords
                )
            ):
                raise SegmentPersistenceError("Segment keyword update is invalid.")
            validated[record.id] = list(keywords)
        timestamp = utcnow()
        for record in records:
            record.keywords = validated[record.id]
            record.updated_at = timestamp
        await self.session.flush()

    async def mark_embeddings_completed(
        self,
        *,
        dataset_index_id: str,
        document_id: str,
        segment_ids: Sequence[str],
    ) -> None:
        """Flush successful embedding state for one exact vector-write batch."""
        if not segment_ids:
            return
        records = await self._exact_mutation_records(
            dataset_index_id=dataset_index_id,
            document_id=document_id,
            segment_ids=tuple(segment_ids),
        )
        if any(record.index_type not in {"general", "child"} for record in records):
            raise SegmentPersistenceError("Segment embedding update is invalid.")
        timestamp = utcnow()
        for record in records:
            record.embedding_status = "completed"
            record.updated_at = timestamp
        await self.session.flush()

    async def soft_delete_previous_segments(
        self, *, dataset_id: str, document_id: str, previous_dataset_index_id: str
    ) -> list[DocumentSegmentRecord]:
        """Soft-delete exactly one previous document/index version after activation."""
        records = await self._records_for_scope(
            dataset_id=dataset_id,
            dataset_index_id=previous_dataset_index_id,
            document_id=document_id,
            status=None,
        )
        timestamp = utcnow()
        for record in records:
            record.deleted_at = timestamp
            record.updated_at = timestamp
        await self.session.flush()
        return records

    async def _records_for_scope(
        self,
        *,
        dataset_id: str,
        dataset_index_id: str,
        document_id: str,
        status: str | None,
    ) -> list[DocumentSegmentRecord]:
        filters = [
            DocumentSegmentRecord.dataset_id == dataset_id,
            DocumentSegmentRecord.dataset_index_id == dataset_index_id,
            DocumentSegmentRecord.document_id == document_id,
            DocumentSegmentRecord.deleted_at.is_(None),
        ]
        if status is not None:
            filters.append(DocumentSegmentRecord.status == status)
        result = await self.session.execute(
            select(DocumentSegmentRecord)
            .where(*filters)
            .order_by(DocumentSegmentRecord.position.asc(), DocumentSegmentRecord.id.asc())
        )
        return list(result.scalars())

    async def _exact_mutation_records(
        self,
        *,
        dataset_index_id: str,
        document_id: str,
        segment_ids: tuple[str, ...],
    ) -> list[DocumentSegmentRecord]:
        requested = set(segment_ids)
        if (
            len(requested) != len(segment_ids)
            or any(
                not isinstance(value, str) or not value or len(value) > 36
                for value in requested
            )
        ):
            raise SegmentPersistenceError("Segment mutation scope is invalid.")
        result = await self.session.execute(
            select(DocumentSegmentRecord).where(
                DocumentSegmentRecord.dataset_index_id == dataset_index_id,
                DocumentSegmentRecord.document_id == document_id,
                DocumentSegmentRecord.id.in_(requested),
                DocumentSegmentRecord.status == "indexing",
                DocumentSegmentRecord.deleted_at.is_(None),
            )
        )
        records = list(result.scalars())
        if {record.id for record in records} != requested:
            raise SegmentPersistenceError("Segment mutation scope is invalid.")
        return records

    async def _load_by_id(self, candidates: list[_Candidate]) -> dict[str, DocumentSegmentRecord]:
        result = await self.session.execute(
            select(DocumentSegmentRecord).where(
                DocumentSegmentRecord.id.in_([candidate.id for candidate in candidates])
            )
        )
        return {record.id: record for record in result.scalars()}

    def _insert_missing_statement(
        self, command: SegmentStagingCommand, missing: list[_Candidate]
    ):
        dialect_name = self.session.get_bind().dialect.name
        if dialect_name == "postgresql":
            insert_statement = postgresql_insert(DocumentSegmentRecord)
        elif dialect_name == "sqlite":
            insert_statement = sqlite_insert(DocumentSegmentRecord)
        else:  # The configured production store is PostgreSQL; SQLite is the test dialect.
            raise RuntimeError("Segment staging requires PostgreSQL or SQLite conflict handling.")
        return insert_statement.values(
            [self._candidate_values(command, candidate) for candidate in missing]
        ).on_conflict_do_nothing(index_elements=[DocumentSegmentRecord.id])

    @staticmethod
    def _candidate_values(command: SegmentStagingCommand, candidate: _Candidate) -> dict:
        return {
            "id": candidate.id,
            "dataset_id": command.dataset_id,
            "dataset_index_id": command.dataset_index_id,
            "document_id": command.document_id,
            "indexing_job_id": command.indexing_job_id,
            "parent_id": candidate.parent_id,
            "position": candidate.preview.position,
            "content": candidate.content,
            "content_hash": candidate.content_hash,
            "source_metadata": candidate.source_metadata,
            "status": "indexing",
            "index_type": candidate.preview.index_type,
            "embedding_status": candidate.embedding_status,
            "created_at": utcnow(),
        }

    @classmethod
    def _exact_existing_records(
        cls,
        command: SegmentStagingCommand,
        candidates: list[_Candidate],
        existing_by_id: dict[str, DocumentSegmentRecord],
    ) -> list[DocumentSegmentRecord]:
        records: list[DocumentSegmentRecord] = []
        for candidate in candidates:
            existing = existing_by_id.get(candidate.id)
            if existing is None:
                continue
            if not cls._is_exact_retry(command, candidate, existing):
                raise SegmentPersistenceError("A deterministic segment ID conflicts with existing data.")
            records.append(existing)
        return records

    @staticmethod
    def _validate_command(command: SegmentStagingCommand) -> None:
        if not isinstance(command, SegmentStagingCommand):
            raise SegmentPersistenceError("Segment staging command is invalid.")
        identifiers = (
            command.dataset_id,
            command.dataset_index_id,
            command.document_id,
            command.indexing_job_id,
        )
        if any(not isinstance(value, str) or not value or len(value) > 36 for value in identifiers):
            raise SegmentPersistenceError("Segment staging command is invalid.")
        if command.indexing_technique not in {"high_quality", "economy"}:
            raise SegmentPersistenceError("Segment staging command is invalid.")
        if command.segmentation_mode not in {"general", "parent_child"}:
            raise SegmentPersistenceError("Segment staging command is invalid.")
        if (
            command.indexing_technique == "economy"
            and command.segmentation_mode != "general"
        ):
            raise SegmentPersistenceError("Segment staging command is invalid.")

    @classmethod
    def _build_candidates(
        cls, command: SegmentStagingCommand, segments: tuple[PreviewSegment, ...]
    ) -> list[_Candidate]:
        by_local_id: dict[str, PreviewSegment] = {}
        for preview in segments:
            cls._validate_preview(preview)
            if preview.local_id in by_local_id:
                raise SegmentPersistenceError("Preview segment local IDs must be unique.")
            by_local_id[preview.local_id] = preview

        parent_ids: dict[str, str] = {}
        normalized: dict[str, tuple[str, dict, str]] = {}
        for preview in segments:
            content = normalize_segment_content(preview.content)
            metadata = normalized_source_metadata(preview.source_metadata)
            content_hash = segment_content_hash(content, metadata)
            normalized[preview.local_id] = (content, metadata, content_hash)
            if preview.index_type == "parent":
                parent_ids[preview.local_id] = stable_segment_id(
                    command.dataset_index_id,
                    command.document_id,
                    None,
                    preview.position,
                    content_hash,
                )

        candidates: list[_Candidate] = []
        ids: set[str] = set()
        for preview in segments:
            parent_id = cls._resolved_parent_id(preview, by_local_id, parent_ids)
            content, metadata, content_hash = normalized[preview.local_id]
            segment_id = stable_segment_id(
                command.dataset_index_id,
                command.document_id,
                parent_id,
                preview.position,
                content_hash,
            )
            if segment_id in ids:
                raise SegmentPersistenceError("Preview segments produce duplicate deterministic IDs.")
            ids.add(segment_id)
            embedding_status = (
                "not_required"
                if preview.index_type == "parent" or command.indexing_technique == "economy"
                else "waiting"
            )
            candidates.append(
                _Candidate(
                    preview=preview,
                    parent_id=parent_id,
                    content=content,
                    source_metadata=metadata,
                    content_hash=content_hash,
                    id=segment_id,
                    embedding_status=embedding_status,
                )
            )
        return candidates

    @staticmethod
    def _validate_preview(preview: PreviewSegment) -> None:
        if not isinstance(preview, PreviewSegment):
            raise SegmentPersistenceError("Preview segment is invalid.")
        if not preview.local_id or len(preview.local_id) > 255:
            raise SegmentPersistenceError("Preview segment is invalid.")
        if isinstance(preview.position, bool) or not isinstance(preview.position, int) or preview.position < 0:
            raise SegmentPersistenceError("Preview segment is invalid.")
        if preview.index_type not in {"general", "parent", "child"}:
            raise SegmentPersistenceError("Preview segment is invalid.")
        if not isinstance(preview.source_metadata, dict):
            raise SegmentPersistenceError("Preview segment is invalid.")

    @staticmethod
    def _resolved_parent_id(
        preview: PreviewSegment,
        by_local_id: dict[str, PreviewSegment],
        parent_ids: dict[str, str],
    ) -> str | None:
        if preview.index_type == "child":
            parent_local_id = preview.parent_local_id
            parent = by_local_id.get(parent_local_id) if parent_local_id is not None else None
            if parent is None or parent.index_type != "parent":
                raise SegmentPersistenceError("Child preview segment references an invalid parent.")
            return parent_ids[parent.local_id]
        if preview.parent_local_id is not None:
            raise SegmentPersistenceError("Only child preview segments may reference a parent.")
        return None

    @staticmethod
    def _is_exact_retry(
        command: SegmentStagingCommand,
        candidate: _Candidate,
        existing: DocumentSegmentRecord,
    ) -> bool:
        return (
            existing.dataset_id == command.dataset_id
            and existing.dataset_index_id == command.dataset_index_id
            and existing.document_id == command.document_id
            and existing.parent_id == candidate.parent_id
            and existing.position == candidate.preview.position
            and existing.index_type == candidate.preview.index_type
            and existing.content_hash == candidate.content_hash
            and existing.content == candidate.content
            and existing.source_metadata == candidate.source_metadata
        )
