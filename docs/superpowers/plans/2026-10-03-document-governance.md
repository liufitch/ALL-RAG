# 文档治理与分段编辑 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (recommended). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为知识库文档实现下载、删除、重命名、启用/禁用、归档/恢复、重新索引和分段编辑，并在分段保存后自动建立新索引版本。

**Architecture:** 在现有 `DocumentService`/`DocumentRepository` 上增加文档治理和 revision 服务；用不可变 `document_revisions` 快照承载人工编辑内容；扩展现有索引任务，使普通任务读取 MinIO、revision 任务读取快照，并沿用新索引版本完成后原子切换。React 文档页面通过 API 操作菜单和分段编辑弹窗调用这些接口并轮询任务。

**Tech Stack:** FastAPI、SQLAlchemy async、Alembic、MinIO ObjectStorage、Celery publisher、现有 indexing engine、React/Vite、pytest、前端 Playwright/Vitest 配置。

**Spec:** `docs/superpowers/specs/2026-10-03-document-governance-design.md`

## Global Constraints

- 所有文档接口必须校验 `dataset_id` 和未删除文档归属。
- 删除、禁用、归档状态必须立即从检索范围排除。
- 分段编辑必须创建不可变 revision，并自动重新索引。
- 新索引失败时旧 active 索引继续可用。
- API 不得暴露 MinIO 对象地址或内部异常堆栈。
- 每个行为先写测试并确认失败，再写生产代码。

---

### Task 1: Revision schema and migration — complete

**Files:**
- Create: `migrations/versions/20261003_01_document_governance.py`
- Modify: `rag_modules/db/models.py`
- Test: `tests/unit/db/test_document_revision_model.py`

**Interfaces:**
- Produces `DocumentRevisionRecord` with immutable segment snapshot fields and indexes on `(document_id, version)` and `(indexing_job_id)`.
- Adds revision relationship fields required by service and indexing jobs without changing existing document primary keys.

- [ ] **Step 1: Write the failing model test**

  Assert that a revision stores dataset/document IDs, monotonically assigned version, JSON segments, status, and optional indexing job ID; assert the migration creates the table and uniqueness constraint.

- [ ] **Step 2: Run the focused test to verify failure**

  Run `pytest tests/unit/db/test_document_revision_model.py -q`. Expected: import/table lookup failure because the model and migration do not exist.

- [ ] **Step 3: Implement the model and Alembic upgrade/downgrade**

  Add the SQLAlchemy model using the existing JSON type helper, foreign keys to datasets/documents/indexing jobs, UTC timestamps, and a unique constraint on `(document_id, version)`. Add migration creation/drop statements following existing migration style.

- [ ] **Step 4: Run the focused test**

  Run `pytest tests/unit/db/test_document_revision_model.py -q`. Expected: PASS.

- [ ] **Step 5: Commit**

  `git add rag_modules/db/models.py migrations/versions/20261003_01_document_governance.py tests/unit/db/test_document_revision_model.py && git commit -m "feat: add document revision persistence"`

### Task 2: Document repository and service governance operations — complete

**Files:**
- Modify: `rag_modules/repositories/document_repository.py`
- Modify: `rag_modules/services/document_service.py`
- Create: `rag_modules/services/document_revision_service.py`
- Test: `tests/unit/repositories/test_document_governance.py`
- Test: `tests/unit/services/test_document_governance_service.py`

**Interfaces:**
- `DocumentRepository.get_active_document(dataset_id, document_id) -> DocumentRecord | None`
- `DocumentRepository.update_name(...)`, `set_enabled(...)`, `set_archived(...)`, `soft_delete(...)`
- `DocumentRepository.list_segments(dataset_id, document_id) -> list[DocumentSegmentRecord]`
- `DocumentRevisionService.create_revision_and_job(dataset_id, document_id, payload, actor_id) -> RevisionJobResult`

- [ ] **Step 1: Write failing service tests**

  Cover rename preserving `object_key`, idempotent enable/disable/archive/restore, soft delete setting `deleted_at`, rejecting cross-dataset IDs, validating non-empty segment content and parent references, incrementing revision version, and rejecting stale `base_version` with a conflict error.

- [ ] **Step 2: Run tests and confirm failure**

  Run `pytest tests/unit/repositories/test_document_governance.py tests/unit/services/test_document_governance_service.py -q`. Expected: missing methods/classes or failing assertions.

- [ ] **Step 3: Implement repository mutations and revision transaction**

  Use async SQLAlchemy queries scoped by dataset and `deleted_at IS NULL`. Commit status changes atomically. For revision creation, lock the document/revision version row, validate the complete snapshot, create `DocumentRevisionRecord`, `IndexingJobRecord`, `DatasetIndexRecord`, and `IndexingJobDocumentRecord` in one transaction, and set document status to `queued`.

- [ ] **Step 4: Run focused tests and refactor only after green**

  Run the same pytest command; expected PASS with no warnings. Keep all state transitions idempotent.

- [ ] **Step 5: Commit**

  `git add rag_modules/repositories/document_repository.py rag_modules/services/document_service.py rag_modules/services/document_revision_service.py tests/unit/repositories/test_document_governance.py tests/unit/services/test_document_governance_service.py && git commit -m "feat: add document governance service operations"`

### Task 3: DTOs and document governance API — complete

**Files:**
- Modify: `rag_modules/api/dto/document.py`
- Modify: `rag_modules/api/file_api.py`
- Modify: `main.py` only if a new router is split out
- Test: `tests/api/test_document_governance_api.py`

**Interfaces:**
- Adds Pydantic request/response models for rename, document detail, segment list/update, revision job response.
- Adds the ten routes in the spec and maps domain errors to 404/409/422/503.

- [ ] **Step 1: Write failing API tests**

  Test download content disposition/type, rename payload, state actions, delete, reindex response, segment list/update, cross-dataset 404, stale revision 409, and object storage failure 503.

- [ ] **Step 2: Run the API tests to verify failure**

  Run `pytest tests/api/test_document_governance_api.py -q`. Expected: 404 for missing routes or missing response models.

- [ ] **Step 3: Implement DTOs, dependencies, routes, and streaming download**

  Add a governance service dependency that reuses the existing session/storage/publisher dependencies. Wrap object storage streams in `StreamingResponse`, sanitize `Content-Disposition` to the stored filename, and never serialize storage credentials or raw exception text.

- [ ] **Step 4: Run focused API tests and existing document API tests**

  Run `pytest tests/api/test_document_governance_api.py tests/api/test_document_api.py -q`. Expected: PASS.

- [ ] **Step 5: Commit**

  `git add rag_modules/api/dto/document.py rag_modules/api/file_api.py main.py tests/api/test_document_governance_api.py && git commit -m "feat: expose document governance APIs"`

### Task 4: Revision-aware indexing worker — complete

**Files:**
- Modify: `rag_modules/tasks/indexing_tasks.py`
- Modify: `rag_modules/indexing/engine.py`
- Modify: `rag_modules/indexing/models.py`
- Modify: `rag_modules/repositories/indexing_repository.py`
- Modify: `rag_modules/repositories/segment_repository.py`
- Test: `tests/unit/indexing/test_revision_indexing.py`

**Interfaces:**
- `IndexDocumentCommand.revision_segments: tuple[PreviewSegment, ...] | None`
- `IndexingTaskRunner` loads revision snapshot when `job.revision_id` is set.
- `DocumentIndexingEngine.run` skips object download/parse/segment and stages `revision_segments` while retaining keyword/embedding/vector behavior.

- [ ] **Step 1: Write failing worker tests**

  Assert a revision job does not call object storage/parser/segmenter, stages the exact edited segments, generates keywords or embeddings, activates the new index on success, and leaves the prior active index untouched on failure.

- [ ] **Step 2: Run the focused tests to verify failure**

  Run `pytest tests/unit/indexing/test_revision_indexing.py -q`. Expected: command lacks revision input and engine still calls parser.

- [ ] **Step 3: Implement revision command path and terminal status updates**

  Load the revision JSON in `build_task_runner`, convert each item to validated `PreviewSegment`, and pass it through the engine. Set revision status to `indexing`, `completed`, or `failed` alongside job status. Keep `finalize_indexing_job` responsible for atomic active-index switching and only mark the revision completed after successful finalization.

- [ ] **Step 4: Run focused and indexing regression tests**

  Run `pytest tests/unit/indexing/test_revision_indexing.py tests/unit/indexing/test_document_engine.py tests/unit/indexing/test_segment_persistence.py -q`. Expected: PASS.

- [ ] **Step 5: Commit**

  `git add rag_modules/tasks/indexing_tasks.py rag_modules/indexing/engine.py rag_modules/indexing/models.py rag_modules/repositories/indexing_repository.py rag_modules/repositories/segment_repository.py tests/unit/indexing/test_revision_indexing.py && git commit -m "feat: index edited document revisions"`

### Task 5: Visibility predicates and cleanup dispatch — complete

**Files:**
- Modify: `rag_modules/repositories/knowledge_base_repository.py`
- Modify: `rag_modules/api/retrieval_api.py`
- Modify: `rag_modules/repositories/indexing_repository.py`
- Modify: `rag_modules/tasks/publisher.py`
- Create: `rag_modules/tasks/document_cleanup_tasks.py`
- Test: `tests/unit/repositories/test_document_visibility.py`
- Test: `tests/unit/tasks/test_document_cleanup.py`

**Interfaces:**
- Shared `document_is_searchable()` SQL predicate for enabled, unarchived, non-deleted, completed documents.
- `TaskPublisher.dispatch_document_cleanup(document_id, dataset_id) -> str | None`.

- [ ] **Step 1: Write failing visibility/cleanup tests**

  Assert disabled, archived, deleted, waiting, and failed documents are excluded from list/statistics/retrieval; assert cleanup removes source object and soft-deletes old segments without affecting other documents.

- [ ] **Step 2: Run tests to verify failure**

  Run `pytest tests/unit/repositories/test_document_visibility.py tests/unit/tasks/test_document_cleanup.py -q`. Expected: disabled/archived documents still appear or cleanup task is undefined.

- [ ] **Step 3: Implement predicate, route filters, publisher and idempotent cleanup task**

  Apply the predicate to retrieval joins and document statistics. Dispatch cleanup only after the delete transaction commits. Cleanup must tolerate missing objects and repeated execution.

- [ ] **Step 4: Run focused plus retrieval regression tests**

  Run `pytest tests/unit/repositories/test_document_visibility.py tests/unit/tasks/test_document_cleanup.py tests/api/test_dataset_api.py -q`. Expected: PASS.

- [ ] **Step 5: Commit**

  `git add rag_modules/repositories/knowledge_base_repository.py rag_modules/api/retrieval_api.py rag_modules/repositories/indexing_repository.py rag_modules/tasks/publisher.py rag_modules/tasks/document_cleanup_tasks.py tests/unit/repositories/test_document_visibility.py tests/unit/tasks/test_document_cleanup.py && git commit -m "feat: enforce document visibility and cleanup"`

### Task 6: Frontend API module and document operations UI — complete

**Files:**
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/components/Documents.jsx`
- Modify: `frontend/src/styles.css`
- Test: `frontend/tests/document-governance.spec.js`

**Interfaces:**
- API helpers: `downloadDocument`, `renameDocument`, `setDocumentEnabled`, `setDocumentArchived`, `deleteDocument`, `reindexDocument`, `listDocumentSegments`, `saveDocumentSegments`, `getIndexingJob`.
- Documents table renders operation labels and action states from DTO fields.

- [ ] **Step 1: Write failing Playwright tests**

  Cover download button, rename dialog, enable/disable, archive/restore, delete confirmation, reindex pending state, and error notice. Mock API responses through the existing test server pattern.

- [ ] **Step 2: Run the focused frontend tests to verify failure**

  Run `npm --prefix frontend test -- --run` if Vitest is configured; otherwise run `npm --prefix frontend exec playwright test frontend/tests/document-governance.spec.js`. Expected: selectors and operations are missing.

- [ ] **Step 3: Implement operation menu, dialogs, filters and download handling**

  Add accessible buttons with Chinese labels, use `window.confirm` only for destructive operations, fetch the blob for downloads, refresh the list after mutations, disable conflicting actions while pending, and render server errors through `request`.

- [ ] **Step 4: Run frontend focused tests**

  Run `npm --prefix frontend exec playwright test tests/document-governance.spec.js`. Expected: PASS.

- [ ] **Step 5: Commit**

  `git add frontend/src/api.js frontend/src/components/Documents.jsx frontend/src/styles.css frontend/tests/document-governance.spec.js && git commit -m "feat: add document governance controls"`

### Task 7: Segment editor and indexing progress UI — complete

**Files:**
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/components/Documents.jsx`
- Create: `frontend/src/components/SegmentEditor.jsx`
- Test: `frontend/tests/segment-editor.spec.js`

**Interfaces:**
- `SegmentEditor` receives `{ datasetId, document, onSaved, onClose }` and submits `{ base_version, segments }`.
- Polls returned `job_id` until `completed`, `partial_success`, `failed`, or `cancelled`.

- [ ] **Step 1: Write failing segment editor tests**

  Assert segment content/question/answer/keywords editing, order changes, validation errors, save response, “正在重新索引” state, terminal success refresh, and failed-job retry.

- [ ] **Step 2: Run focused frontend tests to verify failure**

  Run `npm --prefix frontend exec playwright test tests/segment-editor.spec.js`. Expected: component and selectors are missing.

- [ ] **Step 3: Implement editor and polling**

  Load segments on open, maintain local immutable row state, validate non-empty content and parent IDs, submit once, and poll with abort cleanup. On 409 show a refresh action instead of overwriting newer edits.

- [ ] **Step 4: Run focused frontend tests and the existing console test**

  Run `npm --prefix frontend exec playwright test tests/segment-editor.spec.js tests/console.spec.js`. Expected: PASS.

- [ ] **Step 5: Commit**

  `git add frontend/src/api.js frontend/src/components/Documents.jsx frontend/src/components/SegmentEditor.jsx frontend/tests/segment-editor.spec.js && git commit -m "feat: edit document segments with automatic reindexing"`

### Task 8: Full verification and documentation handoff — complete

**Files:**
- Modify: `docs/superpowers/specs/2026-10-03-document-governance-design.md` only for verified limitations or final API details.
- Modify: `docs/superpowers/plans/2026-10-03-document-governance.md` to check completed steps.

- [ ] **Step 1: Run migration and backend tests**

  Run `pytest -q` from `Graph-RAG/`, then run the migration/model checks if the project exposes a dedicated command. Record the exact pass/fail counts.

- [ ] **Step 2: Run frontend tests**

  Run `npm --prefix frontend test -- --run` and `npm --prefix frontend exec playwright test`. Record any environment-dependent failures separately from product failures.

- [ ] **Step 3: Review the requirement checklist**

  Verify every operation has an API route, service behavior, UI control, and test; verify old active index behavior on revision failure; verify no object storage URL is returned.

- [ ] **Step 4: Update the spec with verified implementation notes**

  Document any intentional deviations, supported limitations, and exact task/job status values. Do not claim completion until all verification commands have fresh successful output.

- [ ] **Step 5: Commit documentation and final implementation state**

  `git add docs/superpowers/specs/2026-10-03-document-governance-design.md docs/superpowers/plans/2026-10-03-document-governance.md && git commit -m "docs: record document governance design and plan"`

## 本地合并记录

- 当前工作目录已经位于 `main` 分支，功能提交 `e2818b2` 已是 `main` 的 HEAD，因此没有可执行的 feature → main 合并差异。
- 本地 `main` 相对 `origin/main` ahead 3，分别包含设计/计划、revision 持久化和文档治理实现。
- 保留未跟踪的 `uv.lock`，它不是本次功能生成的文件，没有纳入合并提交。
- 收尾验证使用：`PYTHONPATH=. UV_CACHE_DIR=/tmp/graph-rag-uv-cache uv run --extra test pytest -q`（599 passed, 3 skipped）、`npm run build`（成功）和 `git diff --check`（无输出）。
