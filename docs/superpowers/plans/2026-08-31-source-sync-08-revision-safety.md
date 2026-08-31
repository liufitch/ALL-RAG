# Source Sync Revision Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为手工上传和后续 connector 建立不可变文档 revision、Hash 去重、安全单文档重建、原子激活和检索可见性过滤。

**Architecture:** `documents` 保存 active/desired 指针，`document_revisions` 保存不可变源版本，segment 关联 revision。新 revision 在 staging 中完成 MinIO、解析、Embedding/Milvus 和校验后，通过 PostgreSQL 条件事务激活；旧资源由 maintenance 任务清理。

**Tech Stack:** Python 3.11、SQLAlchemy async、Alembic、PostgreSQL、MinIO、Celery/RabbitMQ、PyMilvus、pytest。

**Spec:** `docs/superpowers/specs/2026-08-31-source-change-sync-design.md`

## Global Constraints

- 依赖完整索引计划阶段 1–5 的 `DatasetIndexRecord`、索引执行原语和 maintenance queue。
- 第一阶段原始内容 Hash 变化即文档级重建，不做块级复用。
- 新 revision 失败、取消或 superseded 时不得改变 active revision。
- MinIO key 必须包含 revision ID，禁止覆盖旧对象。
- 激活前验证 `target_revision_id == documents.desired_revision_id`。
- 检索必须过滤 deleted、非 completed、非 active revision segment。

---

## File Structure

- Modify: `rag_modules/db/models.py` — revision ORM、document 指针、segment revision 字段。
- Create: `migrations/versions/20260831_02_document_revisions.py` — 增量迁移和约束。
- Create: `rag_modules/domain/content_hashes.py` — 规范化 Hash 与配置 Hash。
- Create: `rag_modules/repositories/document_revision_repository.py` — revision 创建、条件激活、失败和 superseded。
- Create: `rag_modules/services/document_revision_service.py` — 上传去重和安全重建编排入口。
- Modify: `rag_modules/storage/minio.py` — revision object key（由完整索引阶段 2 产出）。
- Modify: `rag_modules/tasks/indexing_tasks.py` — revision staging 和激活（由完整索引阶段 5 产出）。
- Modify: `rag_modules/repositories/retrieval_repository.py` — active revision 回查过滤。
- Test: `tests/unit/domain/test_content_hashes.py`。
- Test: `tests/unit/db/test_document_revision_models.py`。
- Test: `tests/unit/services/test_document_revision_service.py`。
- Test: `tests/integration/source_sync/test_revision_activation.py`。

### Task 1: Revision Schema and Hash Boundary

**Interfaces:**
- Produces: `DocumentRevisionRecord` and enum-like statuses `uploaded/indexing/active/failed/superseded/retired/deleted`.
- Produces: `sha256_stream(chunks: AsyncIterable[bytes]) -> tuple[str, int]`.
- Produces: `index_config_hash(config: Mapping[str, object]) -> str`.

- [ ] **Step 1: Write failing ORM and Hash tests**

```python
def test_document_revision_schema_has_active_and_desired_pointers():
    assert DocumentRevisionRecord.__tablename__ == "document_revisions"
    assert "active_revision_id" in DocumentRecord.__table__.c
    assert "desired_revision_id" in DocumentRecord.__table__.c
    assert "document_revision_id" in DocumentSegmentRecord.__table__.c

async def test_sha256_stream_is_stable():
    digest, size = await sha256_stream(aiter([b"ab", b"c"]))
    assert digest == hashlib.sha256(b"abc").hexdigest()
    assert size == 3
```

- [ ] **Step 2: Run tests and verify missing records/functions**

Run: `python -m pytest tests/unit/db/test_document_revision_models.py tests/unit/domain/test_content_hashes.py -v`

Expected: FAIL importing `DocumentRevisionRecord` or `sha256_stream`.

- [ ] **Step 3: Add the migration, ORM and deterministic Hash functions**

```python
async def sha256_stream(chunks: AsyncIterable[bytes]) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    async for chunk in chunks:
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size

def index_config_hash(config: Mapping[str, object]) -> str:
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

Migration order: create `document_revisions` without document pointer FKs, add nullable revision columns to existing tables, backfill is not required for pre-feature documents, then add named FKs and indexes. Keep `document_segments.vector` untouched.

- [ ] **Step 4: Run unit and migration tests**

Run: `python -m pytest tests/unit/db/test_document_revision_models.py tests/unit/domain/test_content_hashes.py -v`

Expected: PASS, including stable canonical JSON ordering.

- [ ] **Step 5: Commit**

```bash
git add rag_modules/db/models.py rag_modules/domain/content_hashes.py migrations/versions/20260831_02_document_revisions.py tests/unit/db/test_document_revision_models.py tests/unit/domain/test_content_hashes.py
git commit -m "feat: add immutable document revisions"
```

### Task 2: Immutable Upload and No-op Detection

**Interfaces:**
- Consumes: Task 1 Hash functions and complete-index `ObjectStorage.put_stream`.
- Produces: `DocumentRevisionService.ingest_upload(document_id: str, filename: str, stream: AsyncIterable[bytes], config: IndexConfigSnapshot) -> RevisionDecision`.
- Produces: `RevisionDecision(kind: Literal["created", "unchanged"], revision_id: str | None, content_hash: str)`.

- [ ] **Step 1: Write failing service tests**

```python
async def test_same_content_is_unchanged_and_does_not_enqueue():
    result = await service.ingest_upload("doc-1", "renamed.txt", aiter([b"same"]), config)
    assert result.kind == "unchanged"
    queue.enqueue.assert_not_awaited()

async def test_changed_content_uses_revision_object_key():
    result = await service.ingest_upload("doc-1", "a.txt", aiter([b"new"]), config)
    assert result.kind == "created"
    assert storage.last_key == f"datasets/ds-1/documents/doc-1/revisions/{result.revision_id}/a.txt"
```

- [ ] **Step 2: Run test and confirm service is absent**

Run: `python -m pytest tests/unit/services/test_document_revision_service.py -v`

Expected: FAIL importing `DocumentRevisionService`.

- [ ] **Step 3: Implement spool-once hashing and immutable persistence**

```python
@dataclass(frozen=True)
class RevisionDecision:
    kind: Literal["created", "unchanged"]
    content_hash: str
    revision_id: str | None = None
```

Stream to a bounded temporary spool while hashing, compare with the current source Hash, and only allocate a revision/object key for changed content. In one PostgreSQL transaction create the revision and set `desired_revision_id`; enqueue only after commit through the existing indexing job boundary. Delete the new object if the database transaction ultimately fails.

- [ ] **Step 4: Run service and existing upload tests**

Run: `python -m pytest tests/unit/services/test_document_revision_service.py tests/api/test_documents.py -v`

Expected: PASS; same content makes zero queue calls.

- [ ] **Step 5: Commit**

```bash
git add rag_modules/services/document_revision_service.py rag_modules/repositories/document_revision_repository.py rag_modules/storage/minio.py tests/unit/services/test_document_revision_service.py tests/api/test_documents.py
git commit -m "feat: create immutable upload revisions"
```

### Task 3: Conditional Activation and Failure Safety

**Interfaces:**
- Produces: `DocumentRevisionRepository.activate(document_id: str, target_revision_id: str) -> ActivationResult`.
- Produces: `ActivationResult` values `activated` and `superseded`.
- Consumes: complete-index segment persistence, vector validation and maintenance enqueue interfaces.

- [ ] **Step 1: Write integration tests for success, failure and stale completion**

```python
async def test_failed_v2_keeps_v1_active(db, worker):
    await seed_active_revision(db, "v1")
    worker.embedding.raise_for_document("v2")
    await worker.process_revision("v2")
    assert await active_revision_id(db, "doc-1") == "v1"

async def test_v2_cannot_activate_after_v3_is_desired(db, repository):
    await set_desired(db, "doc-1", "v3")
    assert await repository.activate("doc-1", "v2") == ActivationResult.SUPERSEDED
```

- [ ] **Step 2: Run and observe unsafe/missing activation behavior**

Run: `python -m pytest tests/integration/source_sync/test_revision_activation.py -v`

Expected: FAIL because conditional activation does not exist.

- [ ] **Step 3: Implement one short conditional transaction**

```sql
SELECT id, active_revision_id, desired_revision_id
FROM documents
WHERE id = :document_id
FOR UPDATE;
```

If desired differs, mark target `superseded` and return. Otherwise retire the old revision, activate target, soft-delete old segments, complete target segments and update `documents.active_revision_id`. Commit before enqueueing idempotent old-resource cleanup. Any pre-activation exception marks only target failed.

- [ ] **Step 4: Run activation and Celery retry tests**

Run: `python -m pytest tests/integration/source_sync/test_revision_activation.py tests/unit/tasks/test_indexing_tasks.py -v`

Expected: PASS; failure and stale completion preserve v1.

- [ ] **Step 5: Commit**

```bash
git add rag_modules/repositories/document_revision_repository.py rag_modules/tasks/indexing_tasks.py tests/integration/source_sync/test_revision_activation.py tests/unit/tasks/test_indexing_tasks.py
git commit -m "feat: activate document revisions safely"
```

### Task 4: Retrieval Visibility and Cleanup

**Interfaces:**
- Produces: `RetrievalRepository.filter_active_segment_ids(dataset_id: str, ids: Sequence[str]) -> list[str]` preserving input rank.
- Produces: `cleanup_revision_resources(revision_id: str) -> None` idempotent Celery task.

- [ ] **Step 1: Write failing active-filter and cleanup tests**

```python
async def test_filter_keeps_only_active_completed_segments(repository):
    result = await repository.filter_active_segment_ids("ds-1", ["old", "new", "deleted"])
    assert result == ["new"]

async def test_cleanup_missing_vector_and_object_is_success(task):
    await task.run("retired-v1")
    assert await revision_status("retired-v1") == "deleted"
```

- [ ] **Step 2: Run tests and confirm old/staging IDs leak or functions are absent**

Run: `python -m pytest tests/unit/repositories/test_retrieval_repository.py tests/integration/source_sync/test_revision_cleanup.py -v`

Expected: FAIL.

- [ ] **Step 3: Implement ranked PostgreSQL filtering and maintenance cleanup**

Filter on dataset, document not deleted, segment completed/not deleted, segment revision equals document active revision, and dataset index active. Fetch Milvus candidates with configurable over-fetch default 3. Cleanup vectors by segment IDs, then the revision object after retention policy permits; missing targets count as success.

- [ ] **Step 4: Run revision and retrieval regression**

Run: `python -m pytest tests/unit/repositories/test_retrieval_repository.py tests/integration/source_sync/test_revision_cleanup.py tests/integration/test_retrieval.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rag_modules/repositories/retrieval_repository.py rag_modules/tasks/maintenance_tasks.py rag_modules/services/retrieval_service.py tests/unit/repositories/test_retrieval_repository.py tests/integration/source_sync/test_revision_cleanup.py tests/integration/test_retrieval.py
git commit -m "feat: filter and clean retired revisions"
```
