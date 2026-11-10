from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from rag_modules.db.models import DatasetIndexRecord, IndexingJobDocumentRecord, IndexingJobRecord, ProcessRuleRecord
from rag_modules.services.document_service import DatasetNotFoundError


class ProcessRuleValidationError(ValueError):
    code = "INVALID_PROCESS_RULE"


class ProcessRuleConflictError(ProcessRuleValidationError):
    code = "INDEX_CONFIGURATION_CONFLICT"


@dataclass(frozen=True)
class ProcessRuleConfirmation:
    rule: ProcessRuleRecord
    job: IndexingJobRecord
    job_documents: list[IndexingJobDocumentRecord]


class ProcessRuleService:
    def __init__(self, repository):
        self.repository = repository

    async def confirm(self, dataset_id: str, request, actor_id: str) -> ProcessRuleConfirmation:
        if await self.repository.get_active_dataset(dataset_id) is None:
            raise DatasetNotFoundError(dataset_id)
        if await self.repository.get_running_job(dataset_id) is not None:
            raise ProcessRuleConflictError("An indexing job is already in progress; wait for it to finish before configuring another job.")
        documents = await self.repository.get_active_documents(dataset_id, list(request.document_ids))
        if len(documents) != len(request.document_ids):
            raise ProcessRuleValidationError("One or more documents do not belong to this dataset.")
        if request.indexing_technique == "economy" and request.segmentation.mode == "parent_child":
            raise ProcessRuleValidationError("Parent-child segmentation requires high quality indexing.")
        if request.indexing_technique == "high_quality" and not request.embedding_model:
            raise ProcessRuleValidationError("An embedding model is required for high quality indexing.")
        embedding_model = request.embedding_model if request.indexing_technique == "high_quality" else None

        segmentation = request.segmentation.model_dump() if hasattr(request.segmentation, "model_dump") else vars(request.segmentation).copy()
        segmentation.pop("mode", None)
        process_rule = {"segmentation": {"mode": request.segmentation.mode, **segmentation}}
        snapshot = {
            "indexing_technique": request.indexing_technique,
            "embedding_model": embedding_model,
            "segmentation": process_rule["segmentation"],
        }
        config_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        active_index = await self.repository.get_active_index(dataset_id)
        if active_index is not None:
            active_segmentation = dict((active_index.process_rule or {}).get("segmentation", {}))
            # Older snapshots may omit the optional separator.
            active_segmentation.setdefault("separator", None)
            if (
                active_index.index_type != request.indexing_technique
                or active_index.embedding_model != embedding_model
                or active_segmentation != snapshot["segmentation"]
            ):
                raise ProcessRuleConflictError("Index configuration differs from the active index; rebuild the entire knowledge base to change it.")
        existing = await self.repository.find_rule(dataset_id, config_hash)
        rule_is_new = existing is None
        if existing is not None:
            # A rule is immutable; a new confirmation creates a new job but
            # reuses the existing rule and still rebinds selected documents.
            rule = existing
        else:
            rule = ProcessRuleRecord(
                id=uuid4().hex,
                dataset_id=dataset_id,
                version=1,
                mode=request.segmentation.mode,
                rules=process_rule["segmentation"],
                config_hash=config_hash,
                created_by=actor_id,
            )
        job_id = uuid4().hex
        index_id = active_index.id if active_index is not None else uuid4().hex
        job = IndexingJobRecord(
            id=job_id,
            dataset_id=dataset_id,
            target_index_id=index_id,
            job_type="reindex_documents" if active_index is not None else "initial_index",
            scope="selected_documents",
            status="pending",
            indexing_technique=request.indexing_technique,
            segmentation_mode=request.segmentation.mode,
            embedding_model_provider="openai_compatible" if embedding_model else None,
            embedding_model=embedding_model,
            process_rule=snapshot,
            retrieval_config={},
            total_documents=len(documents),
            created_by=actor_id,
        )
        index = None if active_index is not None else DatasetIndexRecord(
            id=index_id,
            dataset_id=dataset_id,
            created_by_job_id=job_id,
            index_type=request.indexing_technique,
            status="building",
            embedding_model_provider="openai_compatible" if embedding_model else None,
            embedding_model=embedding_model,
            vector_store_provider="milvus" if request.indexing_technique == "high_quality" else None,
            process_rule=snapshot,
            retrieval_config={},
            config_hash=config_hash,
        )
        job_documents = [IndexingJobDocumentRecord(id=uuid4().hex, job_id=job_id, document_id=document.id, status="pending") for document in documents]
        rule, job, job_documents = await self.repository.create_rule_and_job(
            rule=rule, job=job, index=index, documents=documents, job_documents=job_documents, rule_is_new=rule_is_new
        )
        return ProcessRuleConfirmation(rule, job, job_documents)
