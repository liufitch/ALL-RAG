# Source Sync Revision 安全实施计划

> **面向代理式执行者：** 必须使用子技能：建议使用 superpowers:subagent-driven-development 或 superpowers:executing-plans，按任务逐项实施本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 为手工上传和后续 connector 建立不可变文档 revision、Hash 去重、安全单文档重建、原子激活和检索可见性过滤。

**架构：** `documents` 保存 active/desired 指针，`document_revisions` 保存不可变源版本，segment 关联 revision。新 revision 在 staging 中完成 MinIO、解析、Embedding/Milvus 和校验后，通过 PostgreSQL 条件事务激活；旧资源由 maintenance 任务清理。

**技术栈：** Python 3.11、SQLAlchemy async、Alembic、PostgreSQL、MinIO、Celery/RabbitMQ、PyMilvus、pytest。

**规格：** `docs/superpowers/specs/2026-08-31-source-change-sync-design.md`

## 全局约束

- 依赖完整索引计划阶段 1–5 的 `DatasetIndexRecord`、索引执行原语和 maintenance queue。
- 第一阶段原始内容 Hash 变化即文档级重建，不做块级复用。
- 新 revision 失败、取消或 superseded 时不得改变 active revision。
- MinIO key 必须包含 revision ID，禁止覆盖旧对象。
- 激活前验证 `target_revision_id == documents.desired_revision_id`。
- 检索必须过滤 deleted、非 completed、非 active revision segment。

---

## 文件结构

- 修改：`rag_modules/db/models.py` — revision ORM、document 指针、segment revision 字段。
- 创建：`migrations/versions/20260831_02_document_revisions.py` — 增量迁移和约束。
- 创建：`rag_modules/domain/content_hashes.py` — 规范化 Hash 与配置 Hash。
- 创建：`rag_modules/repositories/document_revision_repository.py` — revision 创建、条件激活、失败和 superseded。
- 创建：`rag_modules/services/document_revision_service.py` — 上传去重和安全重建编排入口。
- 修改：`rag_modules/storage/minio.py` — revision object key（由完整索引阶段 2 产出）。
- 修改：`rag_modules/tasks/indexing_tasks.py` — revision staging 和激活（由完整索引阶段 5 产出）。
- 修改：`rag_modules/repositories/retrieval_repository.py` — active revision 回查过滤。
- 测试：`tests/unit/domain/test_content_hashes.py`。
- 测试：`tests/unit/db/test_document_revision_models.py`。
- 测试：`tests/unit/services/test_document_revision_service.py`。
- 测试：`tests/integration/source_sync/test_revision_activation.py`。

### 任务 1：Revision Schema 与 Hash 边界

**接口：**
- 产出：`DocumentRevisionRecord` 以及类似枚举的状态 `uploaded/indexing/active/failed/superseded/retired/deleted`。
- 产出：`sha256_stream(chunks: AsyncIterable[bytes]) -> tuple[str, int]`。
- 产出：`index_config_hash(config: Mapping[str, object]) -> str`。

- [ ] **步骤 1：编写失败的 ORM 和 Hash 测试**

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

- [ ] **步骤 2：运行测试并确认记录/函数尚不存在**

运行：`python -m pytest tests/unit/db/test_document_revision_models.py tests/unit/domain/test_content_hashes.py -v`

预期：导入 `DocumentRevisionRecord` 或 `sha256_stream` 时 FAIL。

- [ ] **步骤 3：添加迁移、ORM 和确定性 Hash 函数**

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

迁移顺序：先创建不含 document 指针 FK 的 `document_revisions`，再为现有表添加可空 revision 列；功能上线前的文档无需回填；随后添加具名 FK 和索引。保持 `document_segments.vector` 不变。

- [ ] **步骤 4：运行单元测试和迁移测试**

运行：`python -m pytest tests/unit/db/test_document_revision_models.py tests/unit/domain/test_content_hashes.py -v`

预期：PASS，包括规范 JSON 顺序稳定性。

- [ ] **步骤 5：提交**

```bash
git add rag_modules/db/models.py rag_modules/domain/content_hashes.py migrations/versions/20260831_02_document_revisions.py tests/unit/db/test_document_revision_models.py tests/unit/domain/test_content_hashes.py
git commit -m "feat: add immutable document revisions"
```

### 任务 2：不可变上传与 No-op 检测

**接口：**
- 使用：任务 1 的 Hash 函数和完整索引中的 `ObjectStorage.put_stream`。
- 产出：`DocumentRevisionService.ingest_upload(document_id: str, filename: str, stream: AsyncIterable[bytes], config: IndexConfigSnapshot) -> RevisionDecision`。
- 产出：`RevisionDecision(kind: Literal["created", "unchanged"], revision_id: str | None, content_hash: str)`。

- [ ] **步骤 1：编写失败的服务测试**

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

- [ ] **步骤 2：运行测试并确认服务尚不存在**

运行：`python -m pytest tests/unit/services/test_document_revision_service.py -v`

预期：导入 `DocumentRevisionService` 时 FAIL。

- [ ] **步骤 3：实现一次暂存并计算 Hash，以及不可变持久化**

```python
@dataclass(frozen=True)
class RevisionDecision:
    kind: Literal["created", "unchanged"]
    content_hash: str
    revision_id: str | None = None
```

在流式写入有界临时暂存区的同时计算 Hash，并与当前源 Hash 比较；只有内容变化时才分配 revision/object key。在一个 PostgreSQL 事务中创建 revision 并设置 `desired_revision_id`；通过现有索引任务边界仅在提交后入队。如果数据库事务最终失败，则删除新对象。

- [ ] **步骤 4：运行服务测试和现有上传测试**

运行：`python -m pytest tests/unit/services/test_document_revision_service.py tests/api/test_documents.py -v`

预期：PASS；内容相同时不会调用队列。

- [ ] **步骤 5：提交**

```bash
git add rag_modules/services/document_revision_service.py rag_modules/repositories/document_revision_repository.py rag_modules/storage/minio.py tests/unit/services/test_document_revision_service.py tests/api/test_documents.py
git commit -m "feat: create immutable upload revisions"
```

### 任务 3：条件激活与失败安全性

**接口：**
- 产出：`DocumentRevisionRepository.activate(document_id: str, target_revision_id: str) -> ActivationResult`。
- 产出：`ActivationResult` 值 `activated` 和 `superseded`。
- 使用：完整索引中的 segment 持久化、向量校验和 maintenance 入队接口。

- [ ] **步骤 1：编写覆盖成功、失败和过期完成的集成测试**

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

- [ ] **步骤 2：运行测试并观察不安全/缺失的激活行为**

运行：`python -m pytest tests/integration/source_sync/test_revision_activation.py -v`

预期：由于条件激活尚未实现而 FAIL。

- [ ] **步骤 3：实现一个简短的条件事务**

```sql
SELECT id, active_revision_id, desired_revision_id
FROM documents
WHERE id = :document_id
FOR UPDATE;
```

如果 desired 不同，则将目标标记为 `superseded` 并返回。否则退役旧 revision、激活目标、软删除旧 segment、完成目标 segment，并更新 `documents.active_revision_id`。提交后再将旧资源清理任务幂等入队。激活前发生的任何异常都只将目标标记为失败。

- [ ] **步骤 4：运行激活测试和 Celery 重试测试**

运行：`python -m pytest tests/integration/source_sync/test_revision_activation.py tests/unit/tasks/test_indexing_tasks.py -v`

预期：PASS；失败和过期完成都保留 v1。

- [ ] **步骤 5：提交**

```bash
git add rag_modules/repositories/document_revision_repository.py rag_modules/tasks/indexing_tasks.py tests/integration/source_sync/test_revision_activation.py tests/unit/tasks/test_indexing_tasks.py
git commit -m "feat: activate document revisions safely"
```

### 任务 4：检索可见性与清理

**接口：**
- 产出：`RetrievalRepository.filter_active_segment_ids(dataset_id: str, ids: Sequence[str]) -> list[str]`，保留输入排序。
- 产出：`cleanup_revision_resources(revision_id: str) -> None` 幂等 Celery 任务。

- [ ] **步骤 1：编写失败的 active 过滤和清理测试**

```python
async def test_filter_keeps_only_active_completed_segments(repository):
    result = await repository.filter_active_segment_ids("ds-1", ["old", "new", "deleted"])
    assert result == ["new"]

async def test_cleanup_missing_vector_and_object_is_success(task):
    await task.run("retired-v1")
    assert await revision_status("retired-v1") == "deleted"
```

- [ ] **步骤 2：运行测试并确认旧/staging ID 泄漏或函数尚不存在**

运行：`python -m pytest tests/unit/repositories/test_retrieval_repository.py tests/integration/source_sync/test_revision_cleanup.py -v`

预期：FAIL。

- [ ] **步骤 3：实现保持排序的 PostgreSQL 过滤和 maintenance 清理**

按 dataset、document 未删除、segment 已完成/未删除、segment revision 等于 document active revision、dataset index 为 active 进行过滤。以可配置的 over-fetch 倍数（默认 3）获取 Milvus 候选项。按 segment ID 清理向量，并在保留策略允许后清理 revision 对象；目标不存在也视为成功。

- [ ] **步骤 4：运行 revision 和检索回归测试**

运行：`python -m pytest tests/unit/repositories/test_retrieval_repository.py tests/integration/source_sync/test_revision_cleanup.py tests/integration/test_retrieval.py -v`

预期：PASS。

- [ ] **步骤 5：提交**

```bash
git add rag_modules/repositories/retrieval_repository.py rag_modules/tasks/maintenance_tasks.py rag_modules/services/retrieval_service.py tests/unit/repositories/test_retrieval_repository.py tests/integration/source_sync/test_revision_cleanup.py tests/integration/test_retrieval.py
git commit -m "feat: filter and clean retired revisions"
```
