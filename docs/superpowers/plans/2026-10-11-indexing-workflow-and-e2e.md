# Indexing Workflow and E2E Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Complete cancellable indexing job APIs, unify ingestion with durable indexing, add broker-outbox recovery, and verify the full PostgreSQL/MinIO/RabbitMQ/Celery/Milvus path.

**Architecture:** PostgreSQL remains the source of truth for jobs and dispatch state. API requests create short-lived database transactions and publish only durable identifiers. Celery workers claim job documents with leases; a periodic dispatcher retries pending publications. Unified ingestion reuses existing upload validation, ProcessRule, indexing repository, and task runner components.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Celery/RabbitMQ, MinIO, Milvus, pytest, Docker Compose.

**Spec:** User-approved design in the preceding conversation.

## Global Constraints

- RabbitMQ messages contain identifiers only, never file bytes, credentials, or full document content.
- PostgreSQL is authoritative for task state; Celery result backend remains disabled.
- Every new behavior gets a failing test before production code.
- Existing `/documents` and `/documents/upload` compatibility routes remain functional.
- Errors exposed through APIs and persisted in jobs are safe and credential-free.

### Task 1: Fix cancellation state transition

**Files:**
- Modify: `rag_modules/repositories/indexing_repository.py`
- Test: `tests/unit/repositories/test_indexing_repository.py` (create if absent)

- [ ] Add a test that cancels a running job document with an attached revision and asserts no exception, `cancelled` states, and an empty revision error.
- [ ] Run the focused test and verify it fails with the current undefined `error` reference.
- [ ] Replace the undefined reference with deterministic cancellation handling.
- [ ] Run the focused test and the repository test suite.

### Task 2: Add durable job DTOs and repository operations

**Files:**
- Modify: `rag_modules/api/dto/` and `rag_modules/repositories/indexing_repository.py`
- Test: `tests/unit/repositories/test_indexing_repository.py`

- [ ] Add response/request schemas for job summaries, job-document progress, retry, and cancel operations.
- [ ] Add repository methods to list jobs by dataset, load a job with child rows, request cancellation, and make failed documents retryable.
- [ ] Add tests for dataset scoping, retry eligibility, cancellation idempotency, and terminal job protection.
- [ ] Run focused tests and then all unit tests.

### Task 3: Implement indexing job API routes

**Files:**
- Create: `rag_modules/api/indexing_jobs_api.py`
- Modify: `main.py`
- Test: `tests/api/test_indexing_jobs_api.py`

- [ ] Write failing API tests for create/list/detail/retry/cancel, including 404 cross-dataset access and 409 terminal-state conflicts.
- [ ] Implement `POST /api/knowledge_base/{dataset_id}/indexing/jobs` using the existing ProcessRule/index repository transaction path.
- [ ] Implement paginated list and detailed progress responses.
- [ ] Implement retry publication and cancellation request endpoints.
- [ ] Register the router and run API tests plus the existing API regression suite.

### Task 4: Implement unified ingestion transaction

**Files:**
- Create or modify: `rag_modules/api/ingest_api.py`, `rag_modules/services/document_service.py`, `rag_modules/services/process_rule_service.py`, `rag_modules/repositories/process_rule_repository.py`
- Modify: `rag_modules/db/models.py` only if transaction metadata is missing
- Test: `tests/api/test_ingest_api.py`, `tests/unit/services/test_ingest_service.py`

- [ ] Write failing tests for accepted/rejected mixed batches, all-invalid batches, duplicate idempotent submission, object cleanup on DB failure, and pending dispatch on broker failure.
- [ ] Implement one service boundary that validates all files and configuration before persistence.
- [ ] Upload accepted files with pre-generated document IDs and retain exact object keys for compensation.
- [ ] Create ProcessRule, DatasetIndex, IndexingJob, and IndexingJobDocument rows in one PostgreSQL transaction.
- [ ] Publish only after commit; return accepted documents, rejected files, job ID, and dispatch status.
- [ ] Preserve existing upload endpoints and move the frontend flow to the unified endpoint only after compatibility tests pass.

### Task 5: Add dispatch outbox and pending recovery

**Files:**
- Modify: `rag_modules/db/models.py`
- Create: Alembic migration under `migrations/versions/`
- Modify: `rag_modules/repositories/indexing_repository.py`, `rag_modules/tasks/publisher.py`, `rag_modules/tasks/maintenance_tasks.py`, `rag_modules/tasks/celery_app.py`
- Test: `tests/unit/tasks/test_dispatch_recovery.py`, `tests/api/test_ingest_api.py`

- [ ] Add dispatch metadata to job-document rows or a dedicated outbox table: state, attempts, next attempt, last error code, and last attempt timestamp.
- [ ] Write failing tests for broker failure, retry backoff, duplicate dispatcher runs, and successful transition to queued.
- [ ] Implement transactional creation of dispatch records with job documents.
- [ ] Implement a Celery Beat maintenance task that claims due pending records and publishes identifiers.
- [ ] Make publication idempotent and ensure a duplicate message cannot duplicate segments.
- [ ] Run migration tests and all task/API tests.

### Task 6: Add real infrastructure E2E harness

**Files:**
- Modify: `docker-compose.yml`
- Create: `tests/e2e/conftest.py`, `tests/e2e/test_indexing_pipeline.py`, `tests/e2e/embedding_stub.py`
- Modify: `.env.example`, `README.md`

- [ ] Add health-gated services for PostgreSQL, MinIO, RabbitMQ, Milvus, embedding stub, worker, and beat.
- [ ] Write a smoke test that creates a dataset, uploads a document, waits for completion, and verifies MinIO, PostgreSQL segments, active DatasetIndex, and Milvus count.
- [ ] Add economy-index, cancellation, retry, duplicate-delivery, and broker-recovery scenarios.
- [ ] Add worker-restart and old-active-index preservation scenarios where the compose environment supports them.
- [ ] Run the E2E suite with Docker and document exact startup and teardown commands.

### Task 7: Full verification and documentation

**Files:**
- Modify: `docs/pending-features.md`, `docs/architecture.md`, `README.md`

- [ ] Run backend unit/API tests, migration tests, frontend build, and infrastructure E2E.
- [ ] Fix regressions found by the complete suite.
- [ ] Update feature status and operational instructions with only verified claims.
- [ ] Record known limitations such as unavailable Docker daemon or optional Milvus mode.

