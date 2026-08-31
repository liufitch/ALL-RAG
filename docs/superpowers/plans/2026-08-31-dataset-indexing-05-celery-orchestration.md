# Persistent Celery Indexing Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 PostgreSQL 持久化状态、RabbitMQ 可靠投递和 Celery Worker 把单文档索引原语编排成可恢复、可取消、可重试、可安全切换版本的完整任务。

**Architecture:** API 事务创建 job 后投递只含 ID 的消息；Worker 通过条件更新和租约领取。单文档完成后用 PostgreSQL 锁汇总，完整构建只有全部成功才激活，追加文档按文档激活。Celery Beat 补投 pending、恢复过期租约并执行幂等清理。

**Tech Stack:** Celery 5、RabbitMQ、FastAPI、SQLAlchemy async、PostgreSQL、MinIO、Milvus、pytest。

**Spec:** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## Global Constraints

- Celery 消息体只能包含 `job_id`、`job_document_id`、`index_id`、`document_id` 等标识符。
- PostgreSQL 是任务状态、进度、错误和完成结果的唯一事实来源。
- Celery 使用 `acks_late`，因此所有任务必须支持重复执行。
- API 必须先提交 PostgreSQL，再尝试 RabbitMQ publish。
- 完整重建任一文档最终失败时不得激活新索引。
- 追加文档允许部分成功，成功文档可独立激活。
- Worker 不共享 FastAPI event loop；Celery task 使用 task-scoped async engine/session 并在结束时 dispose。
- 自动重试只用于已批准的短暂故障；配置/格式/认证错误不得无限重试。

---

## File Structure

- Create: `rag_modules/api/dto/indexing_job.py` — 创建、详情、列表、重试和取消 DTO。
- Create: `rag_modules/repositories/indexing_repository.py` — job/index/job-document 锁和状态更新。
- Create: `rag_modules/services/indexing_job_service.py` — 配置 hash、任务分类和 API 事务。
- Create: `rag_modules/api/indexing_job_api.py` — job routes。
- Modify: `main.py` — 注册 job router。
- Create: `rag_modules/tasks/publisher.py` — RabbitMQ publish boundary。
- Create: `rag_modules/tasks/runtime.py` — Celery task-scoped async runtime。
- Create: `rag_modules/tasks/indexing_tasks.py` — dispatch/document/finalize tasks。
- Create: `rag_modules/tasks/maintenance_tasks.py` — pending/stale/retry/cleanup tasks。
- Modify: `rag_modules/tasks/celery_app.py` — routes、Beat schedule、task discovery。
- Create: `rag_modules/indexing/target_coordinator.py` — dimension/collection 并发协调。
- Create: `rag_modules/indexing/progress.py` — PostgreSQL progress reporter/cancel check。
- Modify: `rag_modules/services/document_service.py` — 删除/重建协作。
- Modify: `rag_modules/services/knowledge_base_service.py` — 异步资源清理语义。
- Modify: `rag_modules/api/file_api.py` — 文档删除。
- Modify: `rag_modules/api/knowledge_base_api.py` — dataset 删除发布清理。
- Modify: `docker-compose.yml` — API/Worker/Beat profile 和环境变量。
- Test: `tests/unit/services/test_indexing_job_service.py`。
- Test: `tests/api/test_indexing_job_api.py`。
- Test: `tests/unit/tasks/test_indexing_tasks.py`。
- Test: `tests/unit/tasks/test_recovery_tasks.py`。
- Test: `tests/unit/tasks/test_cleanup_tasks.py`。
- Test: `tests/integration/test_celery_rabbitmq.py`。
- Test: `tests/integration/test_index_version_switch.py`。

### Task 1: Persistent Job Creation and Classification

**Interfaces:**
- Produces: `IndexingJobService.create_job(dataset_id, request, actor_id) -> IndexingJobResponse`。
- Produces: `canonical_config_hash(indexing_technique, model, segmentation, retrieval) -> str`。
- Consumes: active `dataset_indexes`, selected documents, safe embedding catalog.

- [ ] **Step 1: Write failing classification tests**

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

- [ ] **Step 2: Run and observe missing service**

Run: `python -m pytest tests/unit/services/test_indexing_job_service.py -v`

Expected: FAIL on missing repository/service.

- [ ] **Step 3: Implement canonical hash and one-transaction creation**

Canonicalize config with sorted-key compact JSON and SHA-256. Job type rules:

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

If classification selects `reindex_dataset` and `confirm_full_reindex` is false, raise `FULL_REINDEX_CONFIRMATION_REQUIRED` with the full document count before creating any records. After an explicitly confirmed request, create a `building` `DatasetIndexRecord` and deterministic collection name for high quality. For add/reindex documents, target the active index. Create all job-document rows, save immutable process/retrieval snapshots, update dataset's selected config, and commit once. Do not publish from inside this transaction.

- [ ] **Step 4: Run classification tests**

Run: `python -m pytest tests/unit/services/test_indexing_job_service.py -v`

Expected: PASS for initial, add, partial reindex, model/segmentation/technique full rebuild, invalid model, deleted document and parent-child/economy.

- [ ] **Step 5: Commit job domain service**

```bash
git add rag_modules/repositories/indexing_repository.py rag_modules/services/indexing_job_service.py tests/unit/services/test_indexing_job_service.py
git commit -m "feat: create persistent indexing jobs"
```

### Task 2: Job API and Post-Commit Publisher

**Interfaces:**
- Produces: create/list/detail/retry/cancel routes from the approved spec.
- Produces: `TaskPublisher.dispatch_job(job_id: str) -> None`.
- Publish failure leaves job `pending` and returns the persisted job with `dispatch_pending=true`.

- [ ] **Step 1: Write failing API and publish-order tests**

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

- [ ] **Step 2: Run and verify route 404**

Run: `python -m pytest tests/api/test_indexing_job_api.py -v`

Expected: FAIL with missing route/publisher.

- [ ] **Step 3: Implement API, safe errors, and publisher boundary**

`TaskPublisher` calls Celery `send_task("rag_modules.tasks.indexing_tasks.dispatch_indexing_job", kwargs={"job_id": job_id}, queue="indexing")`. Route catches broker exceptions only after job creation, logs request/job IDs without credentials, and leaves pending state for Beat. Detail/list responses are built from PostgreSQL records, including document progress and warnings, never from `AsyncResult`.

- [ ] **Step 4: Run API tests**

Run: `python -m pytest tests/api/test_indexing_job_api.py tests/api/test_dataset_api.py -v`

Expected: PASS for 202, list/detail ownership, missing job 404, publish failure and response secret scan.

- [ ] **Step 5: Commit job API**

```bash
git add rag_modules/api/dto/indexing_job.py rag_modules/api/indexing_job_api.py rag_modules/tasks/publisher.py main.py tests/api/test_indexing_job_api.py
git commit -m "feat: expose persistent indexing job api"
```

### Task 3: Claiming, Dispatch, and Task Runtime

**Interfaces:**
- Produces: `claim_job(job_id, worker_id, lease_seconds) -> bool`。
- Produces: `claim_job_document(job_document_id, worker_id, lease_seconds) -> ClaimResult`。
- Produces Celery tasks `dispatch_indexing_job(job_id)` and `index_document(job_document_id)`。

- [ ] **Step 1: Write failing duplicate-delivery tests**

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

- [ ] **Step 2: Run and observe missing tasks**

Run: `python -m pytest tests/unit/tasks/test_indexing_tasks.py -v`

Expected: FAIL on missing task modules.

- [ ] **Step 3: Implement atomic claims and task-scoped async runtime**

Use conditional SQL updates equivalent to:

```sql
UPDATE indexing_job_documents
SET status='running', attempt=attempt+1, heartbeat_at=now(), lease_expires_at=:lease
WHERE id=:id
  AND status IN ('pending','queued','retry_wait')
  AND available_at <= now()
RETURNING *;
```

Also allow reclaim when status is running and lease is expired. Use a deterministic Celery task ID derived from `job_document_id`. Publish, then persist `celery_task_id`; a crash between those actions may produce a duplicate message, which is intentionally safe because the document claim is authoritative. Dispatcher and Beat may republish queued rows whose task ID/heartbeat proves no active delivery. Never mark a row permanently undispatchable before RabbitMQ confirms publish.

In `tasks/runtime.py`, create a task-scoped async engine with `NullPool`, run the coroutine with `asyncio.run`, and dispose the engine in `finally`; never reuse the FastAPI global pooled async engine across Celery-created event loops.

- [ ] **Step 4: Run task claim tests**

Run: `python -m pytest tests/unit/tasks/test_indexing_tasks.py -v`

Expected: PASS for duplicate dispatch, valid lease, expired lease, completed no-op and deleted/cancelled no-op.

- [ ] **Step 5: Commit task runtime and claims**

```bash
git add rag_modules/tasks/runtime.py rag_modules/tasks/indexing_tasks.py rag_modules/repositories/indexing_repository.py tests/unit/tasks/test_indexing_tasks.py
git commit -m "feat: claim and dispatch indexing tasks safely"
```

### Task 4: Dimension Coordination and Document Execution

**Interfaces:**
- Produces: `IndexTargetCoordinator.resolve(index_id, discovered_dimension) -> VectorTarget` implementing the phase 4 `VectorTargetResolver` protocol。
- Produces: `DatabaseProgressReporter` implementing phase 4 reporter protocol.
- Consumes: `DocumentIndexingEngine.run`.

- [ ] **Step 1: Write failing concurrent dimension/final state tests**

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

- [ ] **Step 2: Run and verify missing coordinator/progress**

Run: `python -m pytest tests/unit/tasks/test_document_execution.py -v`

Expected: FAIL on imports.

- [ ] **Step 3: Implement dimension lock and engine invocation**

Under a PostgreSQL row lock, set `embedding_dimension` only when null; otherwise compare. Store collection name before releasing the lock. Call idempotent `ensure_collection` after commit; concurrent callers may both reach it, so catch already-exists, then validate schema. Persist heartbeats and weighted progress. On successful `add_documents`/`reindex_documents`, atomically activate new segments and retire old document segments; on full build leave staging for finalization.

Map retryable errors to `retry_wait` with delays 30s, 120s, 600s up to `max_attempts`; map terminal errors to `failed` with safe error code/message. Every exit releases/expires the lease and invokes the finalization check.

- [ ] **Step 4: Run document execution tests**

Run: `python -m pytest tests/unit/tasks/test_document_execution.py tests/unit/indexing/test_document_engine.py -v`

Expected: PASS for dimension race, progress, retry delays, cancellation, success activation and terminal failure.

- [ ] **Step 5: Commit document execution**

```bash
git add rag_modules/indexing/target_coordinator.py rag_modules/indexing/progress.py rag_modules/tasks/indexing_tasks.py tests/unit/tasks/test_document_execution.py
git commit -m "feat: execute durable document indexing tasks"
```

### Task 5: Finalization and Safe Index Version Switching

**Interfaces:**
- Produces: `finalize_indexing_job(job_id)` exactly once per terminal set.
- Full builds switch `building -> active` only when all job documents completed.
- Add/reindex document jobs summarize independent outcomes.

- [ ] **Step 1: Write failing version-switch tests**

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

- [ ] **Step 2: Run and observe missing finalizer**

Run: `RUN_INTEGRATION=1 python -m pytest tests/integration/test_index_version_switch.py -v`

Expected: FAIL on missing finalization implementation.

- [ ] **Step 3: Implement row-locked finalization**

Lock the job and all job-document statuses. Return while any document is non-terminal. For initial/reindex dataset, any failed/cancelled document marks new index failed and job failed/partial without touching old active. When all completed, validate expected segment/vector counts, retire old active, activate new, activate staging segments and complete job in one PostgreSQL transaction. Catch the partial unique active-index constraint as an idempotent re-read, never by dropping the constraint.

For add/reindex document jobs, derive `completed`, `partial_success`, `failed` or `cancelled` from document rows and do not change dataset index version.

- [ ] **Step 4: Run version tests**

Run: `RUN_INTEGRATION=1 python -m pytest tests/integration/test_index_version_switch.py -v`

Expected: PASS under concurrent finalize calls and failed/successful rebuilds.

- [ ] **Step 5: Commit finalization**

```bash
git add rag_modules/tasks/indexing_tasks.py rag_modules/repositories/indexing_repository.py tests/integration/test_index_version_switch.py
git commit -m "feat: switch complete index versions atomically"
```

### Task 6: Retry, Cancel, Recovery, and Beat

**Interfaces:**
- Produces retry/cancel service methods and routes.
- Produces Beat tasks `dispatch_pending_jobs()` and `recover_stale_jobs()`.
- Retry creates a new job with `retry_of_job_id` and only failed documents by default.

- [ ] **Step 1: Write failing recovery tests**

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

- [ ] **Step 2: Run and observe failures**

Run: `python -m pytest tests/unit/tasks/test_recovery_tasks.py tests/api/test_indexing_job_api.py -v`

Expected: FAIL for missing recovery/cancel/retry behavior.

- [ ] **Step 3: Implement Beat schedules and cooperative control**

Schedule pending dispatch every 30 seconds and stale recovery every 60 seconds. Select bounded batches with `FOR UPDATE SKIP LOCKED`. Pending dispatch does not mark queued until publish succeeds or dispatcher claims. Recovery checks attempts and either schedules retry or marks terminal failed. Cancel pending/queued immediately; running sets `cancel_requested_at`, and reporters check it between all approved boundaries.

Retry copies the immutable snapshot into a new job and sets `retry_of_job_id`. For failed `initial_index`/`reindex_dataset`, the retry reuses the original `target_index_id`, changes that failed target back to `building`, and includes only failed documents; finalization validates complete target coverage using successful staging segments from the original job plus the retry job before activation. For add/reindex-document jobs, retry targets the current active index and includes only failed documents.

- [ ] **Step 4: Run recovery/API tests**

Run: `python -m pytest tests/unit/tasks/test_recovery_tasks.py tests/api/test_indexing_job_api.py -v`

Expected: PASS for pending republish, lease expiry, max attempts, cancellation and failed-only retry.

- [ ] **Step 5: Commit recovery controls**

```bash
git add rag_modules/tasks/maintenance_tasks.py rag_modules/tasks/celery_app.py rag_modules/services/indexing_job_service.py rag_modules/api/indexing_job_api.py tests/unit/tasks/test_recovery_tasks.py tests/api/test_indexing_job_api.py
git commit -m "feat: recover cancel and retry indexing jobs"
```

### Task 7: Document and Dataset Cleanup

**Interfaces:**
- Produces: `cleanup_document_vectors(document_id)` and `cleanup_dataset_resources(dataset_id)` on maintenance queue.
- Delete endpoints return immediately after soft-delete and publish.

- [ ] **Step 1: Write failing cleanup tests**

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

- [ ] **Step 2: Run and verify cleanup tasks missing**

Run: `python -m pytest tests/unit/tasks/test_cleanup_tasks.py -v`

Expected: FAIL on missing tasks.

- [ ] **Step 3: Implement soft-delete and inferable cleanup state**

Dataset/document delete transaction sets `deleted_at`, soft-deletes active segments and sets cancellation flags. After commit, publish maintenance task. Cleanup reads all index versions, not only active. On successful document object removal, assign a new JSON value with `data_source_info.cleanup_status="purged"` and remove the object key value while retaining name/hash audit fields. Dataset index rows transition `deleting -> deleted_at`. Missing object/vector/collection is success; transient errors retry.

Beat also scans deleted rows whose cleanup status is not purged, covering publish failures. A separate retention scan marks retired collections eligible only after the configured 24-hour default window, then drops them idempotently; explicit dataset deletion may bypass the rollback window and clean immediately.

- [ ] **Step 4: Run cleanup and delete API tests**

Run: `python -m pytest tests/unit/tasks/test_cleanup_tasks.py tests/api/test_document_api.py tests/api/test_dataset_api.py -v`

Expected: PASS for repeat cleanup, missing resources, active tasks, all index versions and publisher failure recovery.

- [ ] **Step 5: Commit cleanup semantics**

```bash
git add rag_modules/tasks/maintenance_tasks.py rag_modules/services/document_service.py rag_modules/services/knowledge_base_service.py rag_modules/api/file_api.py rag_modules/api/knowledge_base_api.py tests/unit/tasks/test_cleanup_tasks.py tests/api/test_document_api.py tests/api/test_dataset_api.py
git commit -m "feat: clean deleted dataset resources asynchronously"
```

### Task 8: RabbitMQ/Celery Integration and Compose App Profile

**Interfaces:**
- Verifies durable publish, Worker execution and redelivery using real RabbitMQ/PostgreSQL.
- Compose profile produces `api`, `celery-worker`, `celery-beat` with shared service configuration, not shared files.

- [ ] **Step 1: Write integration smoke and redelivery tests**

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

- [ ] **Step 2: Run with profile down and verify explicit failure/skip**

Run: `RUN_INTEGRATION=1 python -m pytest tests/integration/test_celery_rabbitmq.py -v`

Expected: FAIL clearly when RabbitMQ/Worker is unavailable; no false PASS.

- [ ] **Step 3: Add API/Worker/Beat Compose services**

Use the same application image and environment, `depends_on` health conditions for PostgreSQL/MinIO/RabbitMQ/Milvus, and commands:

```yaml
command: celery -A rag_modules.tasks.celery_app:celery_app worker -Q indexing,maintenance --loglevel=INFO
```

and:

```yaml
command: celery -A rag_modules.tasks.celery_app:celery_app beat --loglevel=INFO
```

Do not mount `data/uploads`. Add a persistent RabbitMQ volume and application credentials via environment interpolation rather than new hard-coded production secrets.

- [ ] **Step 4: Run real integration**

Run:

```bash
docker compose --profile app up -d --build postgres minio etcd standalone rabbitmq api celery-worker celery-beat
RUN_INTEGRATION=1 python -m pytest tests/integration/test_celery_rabbitmq.py tests/integration/test_index_version_switch.py -v
```

Expected: PASS for normal and duplicate delivery; RabbitMQ queue is durable and PostgreSQL reaches terminal state.

- [ ] **Step 5: Commit orchestration integration**

```bash
git add docker-compose.yml tests/integration/test_celery_rabbitmq.py tests/conftest.py
git commit -m "test: verify rabbitmq celery indexing flow"
```

## Phase Verification

Run:

```bash
python -m pytest tests/unit/tasks tests/unit/services/test_indexing_job_service.py tests/api/test_indexing_job_api.py -v
RUN_INTEGRATION=1 python -m pytest tests/integration/test_celery_rabbitmq.py tests/integration/test_index_version_switch.py -v
docker compose config
git diff --check
```

Expected: PostgreSQL tracks every state transition, RabbitMQ duplicate delivery is harmless, pending/expired tasks recover, and failed full rebuilds keep the old active index.
