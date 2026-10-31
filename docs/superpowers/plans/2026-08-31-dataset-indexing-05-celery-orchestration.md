# 持久化 Celery 索引编排实施计划

> **面向智能体工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项执行本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 用 PostgreSQL 持久化状态、RabbitMQ 可靠投递和 Celery Worker 把单文档索引原语编排成可恢复、可取消、可重试、可安全切换版本的完整任务。

**架构：** API 事务创建 job 后投递只含 ID 的消息；Worker 通过条件更新和租约领取。单文档完成后用 PostgreSQL 锁汇总，完整构建只有全部成功才激活，追加文档按文档激活。Celery Beat 补投 pending、恢复过期租约并执行幂等清理。

**技术栈：** Celery 5、RabbitMQ、FastAPI、SQLAlchemy async、PostgreSQL、MinIO、Milvus、pytest。

**规格：** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## 全局约束

- Celery 消息体只能包含 `job_id`、`job_document_id`、`index_id`、`document_id` 等标识符。
- PostgreSQL 是任务状态、进度、错误和完成结果的唯一事实来源。
- Celery 使用 `acks_late`，因此所有任务必须支持重复执行。
- API 必须先提交 PostgreSQL，再尝试 RabbitMQ publish。
- 完整重建任一文档最终失败时不得激活新索引。
- 追加文档允许部分成功，成功文档可独立激活。
- Worker 不共享 FastAPI event loop；Celery task 使用 task-scoped async engine/session 并在结束时 dispose。
- 自动重试只用于已批准的短暂故障；配置/格式/认证错误不得无限重试。

---

## 文件结构

- 创建：`rag_modules/api/dto/indexing_job.py` — 创建、详情、列表、重试和取消 DTO。
- 创建: `rag_modules/repositories/indexing_repository.py` — job/index/job-document 锁和状态更新。
- 创建: `rag_modules/services/indexing_job_service.py` — 配置 hash、任务分类和 API 事务。
- 创建: `rag_modules/api/indexing_job_api.py` — job routes。
- 修改：`main.py` — 注册 job router。
- 创建: `rag_modules/tasks/publisher.py` — RabbitMQ publish boundary。
- 创建: `rag_modules/tasks/runtime.py` — Celery task-scoped async runtime。
- 创建: `rag_modules/tasks/indexing_tasks.py` — dispatch/document/finalize tasks。
- 创建: `rag_modules/tasks/maintenance_tasks.py` — pending/stale/retry/cleanup tasks。
- 修改: `rag_modules/tasks/celery_app.py` — routes、Beat schedule、task discovery。
- 创建: `rag_modules/indexing/target_coordinator.py` — dimension/collection 并发协调。
- 创建: `rag_modules/indexing/progress.py` — PostgreSQL progress reporter/cancel check。
- 修改: `rag_modules/services/document_service.py` — 删除/重建协作。
- 修改: `rag_modules/services/knowledge_base_service.py` — 异步资源清理语义。
- 修改: `rag_modules/api/file_api.py` — 文档删除。
- 修改: `rag_modules/api/knowledge_base_api.py` — dataset 删除发布清理。
- 修改：`docker-compose.yml` — API/Worker/Beat profile 和环境变量。
- 测试: `tests/unit/services/test_indexing_job_service.py`。
- 测试: `tests/api/test_indexing_job_api.py`。
- 测试: `tests/unit/tasks/test_indexing_tasks.py`。
- 测试: `tests/unit/tasks/test_recovery_tasks.py`。
- 测试: `tests/unit/tasks/test_cleanup_tasks.py`。
- 测试: `tests/integration/test_celery_rabbitmq.py`。
- 测试: `tests/integration/test_index_version_switch.py`。

### 任务 1：持久化任务创建与分类

**接口：**
- 产出: `IndexingJobService.create_job(dataset_id, request, actor_id) -> IndexingJobResponse`。
- 产出: `canonical_config_hash(indexing_technique, model, segmentation, retrieval) -> str`。
- 依赖: active `dataset_indexes`, selected documents, safe embedding catalog.

- [ ] **步骤 1：编写失败的分类测试**

```python
# tests/unit/services/test_indexing_job_service.py
@pytest.mark.asyncio
async def test_first_job_builds_initial_index_and_snapshots_config():
    service, repo = make_job_service(active_index=None)
    result = await service.create_job("dataset-1", high_quality_request(["doc-1"]), "user-1")
    assert result.job_type == "initial_index"
    assert result.scope == "selected_documents"
    assert repo.job.process_rule["segmentation"]["mode"] == "general"
    assert repo.index.status == "building"


@pytest.mark.asyncio
async def test_model_change_expands_scope_to_entire_dataset():
    service, repo = make_job_service(active_index=active_index(model="old-model"), all_documents=["doc-1", "doc-2"])
    result = await service.create_job("dataset-1", high_quality_request(["doc-2"], model="new-model"), "user-1")
    assert result.job_type == "reindex_dataset"
    assert result.scope == "entire_dataset"
    assert {item.document_id for item in repo.job_documents} == {"doc-1", "doc-2"}


@pytest.mark.asyncio
async def test_full_reindex_requires_explicit_confirmation_before_job_creation():
    service, repo = make_job_service(active_index=active_index(model="old-model"))
    with pytest.raises(BusinessRuleError) as error:
        await service.create_job(
            "dataset-1",
            high_quality_request(["doc-1"], model="new-model", confirm_full_reindex=False),
            "user-1",
        )
    assert error.value.code == "FULL_REINDEX_CONFIRMATION_REQUIRED"
    assert error.value.metadata["scope"] == "entire_dataset"
    assert repo.commits == 0


@pytest.mark.asyncio
async def test_parent_child_economy_is_rejected_before_transaction():
    service, repo = make_job_service()
    with pytest.raises(BusinessRuleError) as error:
        await service.create_job("dataset-1", parent_child_request(technique="economy"), "user-1")
    assert error.value.code == "PARENT_CHILD_REQUIRES_HIGH_QUALITY"
    assert repo.commits == 0
```

- [ ] **步骤 2：运行并观察服务缺失**

运行: `python -m pytest tests/unit/services/test_indexing_job_service.py -v`

预期：因 repository/service 缺失而失败。

- [ ] **步骤 3：实现规范哈希和单事务创建**

使用排序键的紧凑 JSON 和 SHA-256 规范化配置。任务类型规则：

```python
if active_index is None:
    job_type, scope = "initial_index", "selected_documents"
elif active_index.config_hash != requested_hash:
    job_type, scope = "reindex_dataset", "entire_dataset"
elif selected_documents_are_new:
    job_type, scope = "add_documents", "selected_documents"
else:
    job_type, scope = "reindex_documents", "selected_documents"
```

如果分类结果为 `reindex_dataset` 且 `confirm_full_reindex` 为 false，则在创建任何记录前，携带完整文档数抛出 `FULL_REINDEX_CONFIRMATION_REQUIRED`。请求得到明确确认后，为高质量模式创建状态为 `building` 的 `DatasetIndexRecord` 和确定性的 collection 名称。新增/重建文档时，以活动索引为目标。创建所有 job-document 行，保存不可变的处理/检索配置快照，更新 dataset 当前选中的配置，并一次性提交。不得在该事务内发布任务。

- [ ] **步骤 4：运行分类测试**

运行: `python -m pytest tests/unit/services/test_indexing_job_service.py -v`

预期：初始索引、追加、部分重索引、模型/分段/技术全量重建、无效模型、已删除文档和父子/经济模式测试均通过。

- [ ] **步骤 5：提交任务领域服务**

```bash
git add rag_modules/repositories/indexing_repository.py rag_modules/services/indexing_job_service.py tests/unit/services/test_indexing_job_service.py
git commit -m "feat: create persistent indexing jobs"
```

### 任务 2：任务 API 与提交后发布器

**接口：**
- 产出: create/list/detail/retry/cancel routes from the approved spec.
- 产出: `TaskPublisher.dispatch_job(job_id: str) -> None`.
- 发布失败时将任务保留为 `pending`，并返回已持久化且 `dispatch_pending=true` 的任务。

- [ ] **步骤 1：编写失败的 API 和发布顺序测试**

```python
# tests/api/test_indexing_job_api.py
def test_create_job_returns_202_and_only_publishes_identifier(client, task_publisher):
    response = client.post(
        "/api/knowledge_base/dataset-1/indexing/jobs",
        json=high_quality_job_json(["doc-1"]),
    )
    assert response.status_code == 202
    assert task_publisher.calls == [("dispatch_indexing_job", {"job_id": response.json()["job_id"]})]


def test_publish_failure_keeps_queryable_pending_job(client, failing_publisher):
    response = client.post("/api/knowledge_base/dataset-1/indexing/jobs", json=economy_job_json(["doc-1"]))
    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.json()["dispatch_pending"] is True
    detail = client.get(f"/api/knowledge_base/dataset-1/indexing/jobs/{response.json()['job_id']}")
    assert detail.status_code == 200
```

- [ ] **步骤 2：运行并验证路由 404**

运行: `python -m pytest tests/api/test_indexing_job_api.py -v`

预期：因缺少路由/发布器而失败。

- [ ] **步骤 3：实现 API、安全错误和发布器边界**

`TaskPublisher` 调用 Celery `send_task("rag_modules.tasks.indexing_tasks.dispatch_indexing_job", kwargs={"job_id": job_id}, queue="indexing")`。路由仅在任务创建后捕获 broker 异常；日志记录 request/job ID，但不记录凭据，并保留 pending 状态供 Beat 后续处理。详情/列表响应由 PostgreSQL 记录构建，包含文档进度和 warning，绝不读取 `AsyncResult`。

- [ ] **步骤 4：运行 API 测试**

运行: `python -m pytest tests/api/test_indexing_job_api.py tests/api/test_dataset_api.py -v`

预期：202、列表/详情归属、任务不存在 404、发布失败和响应密钥扫描测试均通过。

- [ ] **步骤 5：提交任务 API**

```bash
git add rag_modules/api/dto/indexing_job.py rag_modules/api/indexing_job_api.py rag_modules/tasks/publisher.py main.py tests/api/test_indexing_job_api.py
git commit -m "feat: expose persistent indexing job api"
```

### 任务 3：领取、分发与任务运行时

**接口：**
- 产出: `claim_job(job_id, worker_id, lease_seconds) -> bool`。
- 产出: `claim_job_document(job_document_id, worker_id, lease_seconds) -> ClaimResult`。
- 产出 Celery 任务 `dispatch_indexing_job(job_id)` 和 `index_document(job_document_id)`。

- [ ] **步骤 1：编写失败的重复投递测试**

```python
# tests/unit/tasks/test_indexing_tasks.py
def test_duplicate_dispatch_keeps_one_persistent_job_document_per_source_document(celery_tasks, repo, publisher):
    repo.seed_pending_job("job-1", ["jd-1", "jd-2"])
    celery_tasks.dispatch_indexing_job.run(job_id="job-1")
    celery_tasks.dispatch_indexing_job.run(job_id="job-1")
    assert repo.job_document_ids("job-1") == ["jd-1", "jd-2"]
    assert set(publisher.document_ids) == {"jd-1", "jd-2"}


def test_duplicate_document_message_respects_active_lease(celery_tasks, repo, engine):
    repo.seed_running_job_document("jd-1", lease_expires_in=300)
    celery_tasks.index_document.run(job_document_id="jd-1")
    assert engine.calls == []
```

- [ ] **步骤 2：运行并观察任务缺失**

运行: `python -m pytest tests/unit/tasks/test_indexing_tasks.py -v`

预期：因任务模块缺失而失败。

- [ ] **步骤 3：实现原子领取和任务范围的异步运行时**

使用等价于以下语句的条件 SQL 更新：

```sql
UPDATE indexing_job_documents
SET status='running', attempt=attempt+1, heartbeat_at=now(), lease_expires_at=:lease
WHERE id=:id
  AND status IN ('pending','queued','retry_wait')
  AND available_at <= now()
RETURNING *;
```

当状态为 running 且租约过期时，也允许重新领取。根据 `job_document_id` 生成确定性的 Celery task ID。先发布，再持久化 `celery_task_id`；这两步之间崩溃可能产生重复消息，但这是安全的，因为文档领取状态才是权威。若 task ID/heartbeat 表明没有活动投递，Dispatcher 和 Beat 可以重新发布 queued 行。RabbitMQ 确认发布前，绝不要将行永久标记为不可分发。

在 `tasks/runtime.py` 中使用 `NullPool` 创建任务范围的异步引擎，以 `asyncio.run` 运行协程，并在 `finally` 中释放引擎；绝不要在 Celery 创建的 event loop 之间复用 FastAPI 全局池化异步引擎。

- [ ] **步骤 4：运行任务领取测试**

运行: `python -m pytest tests/unit/tasks/test_indexing_tasks.py -v`

预期：重复分发、有效租约、过期租约、已完成 no-op 和已删除/已取消 no-op 测试均通过。

- [ ] **步骤 5：提交任务运行时与领取逻辑**

```bash
git add rag_modules/tasks/runtime.py rag_modules/tasks/indexing_tasks.py rag_modules/repositories/indexing_repository.py tests/unit/tasks/test_indexing_tasks.py
git commit -m "feat: claim and dispatch indexing tasks safely"
```

### 任务 4：维度协调与文档执行

**接口：**
- 产出: `IndexTargetCoordinator.resolve(index_id, discovered_dimension) -> VectorTarget` implementing the phase 4 `VectorTargetResolver` protocol。
- 提供：实现阶段 4 reporter 协议的 `DatabaseProgressReporter`。
- 使用：`DocumentIndexingEngine.run`。

- [ ] **步骤 1：编写失败的并发维度/最终状态测试**

```python
# tests/unit/tasks/test_document_execution.py
@pytest.mark.asyncio
async def test_first_dimension_is_persisted_and_same_dimension_is_reusable(coordinator):
    first = await coordinator.resolve("index-1", 1024)
    second = await coordinator.resolve("index-1", 1024)
    assert first == second
    assert coordinator.vector_store.ensure_calls == [(first.collection_name, 1024, "COSINE")]


@pytest.mark.asyncio
async def test_dimension_mismatch_fails_without_changing_persisted_dimension(coordinator):
    await coordinator.resolve("index-1", 1024)
    with pytest.raises(IndexDimensionMismatch):
        await coordinator.resolve("index-1", 768)
    assert coordinator.index.embedding_dimension == 1024


def test_document_task_records_safe_failure_and_retry_time(task, repo, retryable_engine):
    task.run(job_document_id="jd-1")
    row = repo.get_job_document("jd-1")
    assert row.status == "retry_wait"
    assert row.available_at > utcnow()
    assert "api_key" not in (row.error or "").lower()
```

- [ ] **步骤 2：运行并验证协调器/进度功能缺失**

运行: `python -m pytest tests/unit/tasks/test_document_execution.py -v`

预期：导入失败。

- [ ] **步骤 3：实现维度锁和引擎调用**

在 PostgreSQL 行锁下，仅当 `embedding_dimension` 为 null 时设置它；否则进行比较。释放锁前保存 collection 名称。提交后调用幂等的 `ensure_collection`；并发调用方可能都会执行到这里，因此捕获 already-exists 后再校验 schema。持久化 heartbeat 和加权进度。`add_documents`/`reindex_documents` 成功后，原子激活新分段并将旧文档分段退役；全量构建则保留暂存状态供最终化处理。

将可重试错误映射为 `retry_wait`，延迟依次为 30s、120s、600s，最多达到 `max_attempts`；将终止错误映射为 `failed`，并记录安全的错误代码/消息。每个退出路径都会释放/使租约过期，并调用最终化检查。

- [ ] **步骤 4：运行文档执行测试**

运行: `python -m pytest tests/unit/tasks/test_document_execution.py tests/unit/indexing/test_document_engine.py -v`

预期：维度竞争、进度、重试延迟、取消、成功激活和终止失败测试均通过。

- [ ] **步骤 5：提交文档执行逻辑**

```bash
git add rag_modules/indexing/target_coordinator.py rag_modules/indexing/progress.py rag_modules/tasks/indexing_tasks.py tests/unit/tasks/test_document_execution.py
git commit -m "feat: execute durable document indexing tasks"
```

### 任务 5：最终化与安全索引版本切换

**接口：**
- 产出: `finalize_indexing_job(job_id)` exactly once per terminal set.
- 全量构建只有在所有 job document 均完成后，才将索引从 `building` 切换为 `active`。
- 新增/重建文档任务分别汇总各文档的独立结果。

- [ ] **步骤 1：编写失败的版本切换测试**

```python
# tests/integration/test_index_version_switch.py
@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_rebuild_failure_keeps_old_index_active(db):
    old, new, job = await seed_rebuild(db, document_statuses=["completed", "failed"])
    await finalize_job(db, job.id)
    assert (await db.get(DatasetIndexRecord, old.id)).status == "active"
    assert (await db.get(DatasetIndexRecord, new.id)).status == "failed"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_successful_full_rebuild_switches_once(db):
    old, new, job = await seed_rebuild(db, document_statuses=["completed", "completed"])
    await asyncio.gather(finalize_job(db, job.id), finalize_job(db, job.id))
    assert (await db.get(DatasetIndexRecord, old.id)).status == "retired"
    assert (await db.get(DatasetIndexRecord, new.id)).status == "active"
    assert await count_active_indexes(db, new.dataset_id) == 1
```

- [ ] **步骤 2：运行并观察最终化器缺失**

运行: `RUN_INTEGRATION=1 python -m pytest tests/integration/test_index_version_switch.py -v`

预期：因缺少最终化实现而失败。

- [ ] **步骤 3：实现行锁定最终化**

锁定任务及所有 job-document 状态。只要仍有文档处于非终态就返回。对于初始/重索引 Dataset，任何失败/取消文档都会将新索引标记为 failed，并将任务标记为 failed/partial，同时不触碰旧活动索引。全部完成后，在一个 PostgreSQL 事务中校验预期分段/vector 计数，退役旧活动索引、激活新索引、激活暂存分段并完成任务。将部分唯一活动索引约束冲突作为幂等重读处理，绝不要删除约束。

对于 add/reindex document 任务，根据文档行推导 `completed`、`partial_success`、`failed` 或 `cancelled`，不要改变 Dataset 索引版本。

- [ ] **步骤 4：运行版本测试**

运行: `RUN_INTEGRATION=1 python -m pytest tests/integration/test_index_version_switch.py -v`

预期：并发最终化调用以及失败/成功重建测试均通过。

- [ ] **步骤 5：提交最终化逻辑**

```bash
git add rag_modules/tasks/indexing_tasks.py rag_modules/repositories/indexing_repository.py tests/integration/test_index_version_switch.py
git commit -m "feat: switch complete index versions atomically"
```

### 任务 6：重试、取消、恢复与 Beat

**接口：**
- 提供重试/取消服务方法和路由。
- 提供 Beat 任务 `dispatch_pending_jobs()` 和 `recover_stale_jobs()`。
- 重试默认使用 `retry_of_job_id` 创建新任务，并仅包含失败文档。

- [ ] **步骤 1：编写失败的恢复测试**

```python
# tests/unit/tasks/test_recovery_tasks.py
def test_dispatch_pending_republishes_committed_unpublished_jobs(recovery, repo, publisher):
    repo.seed_job("job-1", status="pending", created_minutes_ago=5)
    recovery.dispatch_pending_jobs.run()
    assert publisher.job_ids == ["job-1"]


def test_recover_stale_job_requeues_expired_lease(recovery, repo, publisher):
    repo.seed_job_document("jd-1", status="running", lease_expired=True, attempt=1)
    recovery.recover_stale_jobs.run()
    assert repo.get_job_document("jd-1").status == "retry_wait"


def test_cancel_running_job_sets_cooperative_flag(client):
    response = client.post("/api/knowledge_base/dataset-1/indexing/jobs/job-1/cancel")
    assert response.status_code == 202
    assert response.json()["cancel_requested_at"] is not None


@pytest.mark.asyncio
async def test_retry_failed_full_build_reuses_target_and_successful_staging(service, repo):
    original = repo.seed_failed_full_build(successful=["doc-1"], failed=["doc-2"])
    retry = await service.retry_job(original.dataset_id, original.id, actor_id="user-1")
    assert retry.retry_of_job_id == original.id
    assert retry.target_index_id == original.target_index_id
    assert [item.document_id for item in repo.job_documents_for(retry.id)] == ["doc-2"]
```

- [ ] **步骤 2：运行并观察失败结果**

运行: `python -m pytest tests/unit/tasks/test_recovery_tasks.py tests/api/test_indexing_job_api.py -v`

预期：因缺少恢复/取消/重试行为而失败。

- [ ] **步骤 3：实现 Beat 调度和协作式控制**

每 30 秒调度 pending 分发，每 60 秒执行过期恢复。使用 `FOR UPDATE SKIP LOCKED` 选择有界批次。Pending 分发只有在发布成功或被 dispatcher 领取后才标记 queued。恢复逻辑检查尝试次数，然后安排重试或标记终止失败。pending/queued 立即取消；running 设置 `cancel_requested_at`，reporter 在所有批准的边界之间检查它。

重试时将不可变快照复制到新任务，并设置 `retry_of_job_id`。对于失败的 `initial_index`/`reindex_dataset`，重试复用原 `target_index_id`，将失败目标恢复为 `building`，且只包含失败文档；激活前，finalization 使用原任务和重试任务中的成功 staging 分段验证目标索引覆盖完整。对于新增/重建文档任务，重试以当前活动索引为目标，且只包含失败文档。

- [ ] **步骤 4：运行恢复/API 测试**

运行: `python -m pytest tests/unit/tasks/test_recovery_tasks.py tests/api/test_indexing_job_api.py -v`

预期：pending 重新发布、租约过期、最大尝试次数、取消和仅失败文档重试测试均通过。

- [ ] **步骤 5：提交恢复控制**

```bash
git add rag_modules/tasks/maintenance_tasks.py rag_modules/tasks/celery_app.py rag_modules/services/indexing_job_service.py rag_modules/api/indexing_job_api.py tests/unit/tasks/test_recovery_tasks.py tests/api/test_indexing_job_api.py
git commit -m "feat: recover cancel and retry indexing jobs"
```

### 任务 7：文档与 Dataset 清理

**接口：**
- 在 maintenance queue 上提供 `cleanup_document_vectors(document_id)` 和 `cleanup_dataset_resources(dataset_id)`。
- Delete endpoint 在软删除并发布任务后立即返回。

- [ ] **步骤 1：编写失败的清理测试**

```python
# tests/unit/tasks/test_cleanup_tasks.py
def test_document_cleanup_removes_vectors_then_object_idempotently(cleanup, deps):
    cleanup.cleanup_document_vectors.run(document_id="doc-1")
    cleanup.cleanup_document_vectors.run(document_id="doc-1")
    assert deps.vector_store.deleted_documents == [("active_collection", "doc-1")]
    assert deps.object_storage.removed == ["datasets/dataset-1/documents/doc-1/source.pdf"]


def test_dataset_cleanup_drops_all_index_collections_and_objects(cleanup, deps):
    cleanup.cleanup_dataset_resources.run(dataset_id="dataset-1")
    assert set(deps.vector_store.dropped) == {"collection-v1", "collection-v2"}
    assert set(deps.object_storage.removed) == set(deps.dataset_object_keys)
```

- [ ] **步骤 2：运行并验证清理任务缺失**

运行: `python -m pytest tests/unit/tasks/test_cleanup_tasks.py -v`

预期：因缺少任务而失败。

- [ ] **步骤 3：实现软删除与可推断的清理状态**

Dataset/document 删除事务设置 `deleted_at`、软删除活动分段并设置取消标志。提交后发布 maintenance task。清理时读取所有索引版本，而不只是活动版本。成功删除文档对象后，赋予 `data_source_info.cleanup_status="purged"` 一个新的 JSON 值，并移除 object key 值，同时保留名称/哈希审计字段。Dataset index 行状态从 `deleting` 转为 `deleted_at`。对象/vector/collection 缺失视为成功；暂时性错误则重试。

Beat 还会扫描清理状态尚未 purged 的已删除行，以覆盖发布失败。独立的保留期扫描只在默认配置的 24 小时窗口结束后将退役 collection 标记为可清理，随后幂等地删除；显式 Dataset 删除可跳过回滚窗口并立即清理。

- [ ] **步骤 4：运行清理和删除 API 测试**

运行: `python -m pytest tests/unit/tasks/test_cleanup_tasks.py tests/api/test_document_api.py tests/api/test_dataset_api.py -v`

预期：重复清理、资源缺失、活动任务、所有索引版本和发布器失败恢复测试均通过。

- [ ] **步骤 5：提交清理语义**

```bash
git add rag_modules/tasks/maintenance_tasks.py rag_modules/services/document_service.py rag_modules/services/knowledge_base_service.py rag_modules/api/file_api.py rag_modules/api/knowledge_base_api.py tests/unit/tasks/test_cleanup_tasks.py tests/api/test_document_api.py tests/api/test_dataset_api.py
git commit -m "feat: clean deleted dataset resources asynchronously"
```

### 任务 8：RabbitMQ/Celery 集成与 Compose 应用 Profile

**接口：**
- 使用真实 RabbitMQ/PostgreSQL 验证持久化发布、Worker 执行和消息重投。
- Compose profile 提供 `api`、`celery-worker`、`celery-beat`，共享服务配置而不是共享文件。

- [ ] **步骤 1：编写集成冒烟和消息重投测试**

```python
# tests/integration/test_celery_rabbitmq.py
@pytest.mark.integration
def test_job_message_reaches_worker_and_updates_postgres(real_job_factory, wait_for):
    job = real_job_factory(economy=True)
    dispatch_job(job.id)
    wait_for(lambda: load_job(job.id).status == "completed", timeout=30)
    assert load_job(job.id).completed_documents == 1


@pytest.mark.integration
def test_duplicate_message_does_not_duplicate_segments(real_job_factory, wait_for):
    job = real_job_factory(economy=True)
    dispatch_job(job.id)
    dispatch_job(job.id)
    wait_for(lambda: load_job(job.id).status == "completed", timeout=30)
    assert count_distinct_segment_ids(job.id) == count_segments(job.id)
```

- [ ] **步骤 2：关闭 profile 运行，并验证明确的失败/跳过结果**

运行: `RUN_INTEGRATION=1 python -m pytest tests/integration/test_celery_rabbitmq.py -v`

预期：RabbitMQ/Worker 不可用时明确失败；不得错误地报告通过。

- [ ] **步骤 3：添加 API/Worker/Beat Compose 服务**

使用相同的应用镜像和环境，设置 PostgreSQL/MinIO/RabbitMQ/Milvus 的 `depends_on` 健康条件，并使用以下命令：

```yaml
command: celery -A rag_modules.tasks.celery_app:celery_app worker -Q indexing,maintenance --loglevel=INFO
```

and:

```yaml
command: celery -A rag_modules.tasks.celery_app:celery_app beat --loglevel=INFO
```

不要挂载 `data/uploads`。添加持久化 RabbitMQ volume，并通过环境变量插值提供应用凭据，避免新增硬编码生产密钥。

- [ ] **步骤 4：运行真实集成测试**

运行:

```bash
docker compose --profile app up -d --build postgres minio etcd standalone rabbitmq api celery-worker celery-beat
RUN_INTEGRATION=1 python -m pytest tests/integration/test_celery_rabbitmq.py tests/integration/test_index_version_switch.py -v
```

预期：正常投递和重复投递测试均通过；RabbitMQ 队列持久化，PostgreSQL 状态达到终态。

- [ ] **步骤 5：提交编排集成**

```bash
git add docker-compose.yml tests/integration/test_celery_rabbitmq.py tests/conftest.py
git commit -m "test: verify rabbitmq celery indexing flow"
```

## 阶段验证

运行:

```bash
python -m pytest tests/unit/tasks tests/unit/services/test_indexing_job_service.py tests/api/test_indexing_job_api.py -v
RUN_INTEGRATION=1 python -m pytest tests/integration/test_celery_rabbitmq.py tests/integration/test_index_version_switch.py -v
docker compose config
git diff --check
```

预期：PostgreSQL 跟踪每次状态转换，RabbitMQ 重复投递无害，pending/过期任务能够恢复，且全量重建失败时保留旧活动索引。
