# Dataset Indexing End-to-End Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用真实 PostgreSQL、RabbitMQ、MinIO、Milvus 和 mock OpenAI-compatible 服务验证完整链路，补齐故障恢复、资源清理、安全检查和旧 JSON 清理。

**Architecture:** E2E fixture 只通过公开 HTTP API 驱动系统，并通过只读数据库/Milvus/MinIO断言验证跨系统不变量。故障测试在唯一 test dataset/index 上运行，所有资源都用唯一前缀并在 finally/fixture teardown 中幂等清理。

**Tech Stack:** Docker Compose、FastAPI mock service、pytest、PostgreSQL、RabbitMQ、Celery、MinIO、Milvus、React/Vite。

**Spec:** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## Global Constraints

- E2E 不使用真实付费 Embedding 服务或真实 API Key。
- 测试对象、collection 和 dataset 使用唯一 ID，不触碰用户已有数据。
- PostgreSQL `document_segments.vector` 必须保持 `NULL`。
- RabbitMQ/Celery 测试必须验证重复投递和 Worker 中断恢复，而不仅是正常路径。
- 完整重建失败必须证明旧 active index 未变。
- 资源删除必须验证 PostgreSQL、MinIO 和 Milvus 三方最终一致。
- 不执行广泛目录删除；只清理 fixture 创建的明确对象/collection/记录。
- 最终文档不得包含真实密码、token 或 `.env` 内容。

---

## File Structure

- Create: `tests/support/mock_embedding_app.py` — 可配置维度/错误/延迟的 OpenAI-compatible mock。
- Create: `tests/e2e/conftest.py` — API、数据库、MinIO、Milvus 和轮询 fixture。
- Create: `tests/e2e/test_high_quality_flow.py`。
- Create: `tests/e2e/test_parent_child_flow.py`。
- Create: `tests/e2e/test_economy_flow.py`。
- Create: `tests/e2e/test_recovery_and_versioning.py`。
- Create: `tests/e2e/test_deletion_cleanup.py`。
- Modify: `docker-compose.yml` — test-only mock embedding profile/healthcheck。
- Create: `rag_modules/config/.env.example` — 无密钥模板。
- Modify: `.gitignore` — verify secrets、cache、volumes、node_modules 和 runtime data exclusions created in phase 1。
- Modify: `main.py` — 删除旧 JSON、旧内联 Milvus/DTO 注释块和无用 import。
- Delete if present: `data/knowledge_bases.json` and `data/knowledge_base.json` only。
- Remove obsolete: `rag_modules/api/dto/other/vectorStoreConfig.py` when no imports remain。
- Modify: `README.md` — setup、migration、API、Worker、Beat、测试和故障排查。
- Test: `tests/unit/test_no_legacy_json_store.py`。
- Test: `tests/unit/test_secret_safety.py`。

### Task 1: Deterministic Mock OpenAI-Compatible Service

**Interfaces:**
- Produces: `POST /v1/embeddings` with OpenAI-compatible `data[index, embedding]`。
- Control headers available only in tests: dimension, forced status, delay and reversed response order。

- [ ] **Step 1: Write failing mock-service contract tests**

```python
# tests/support/test_mock_embedding_app.py
def test_mock_embedding_is_deterministic_and_indexed(mock_embedding_client):
    response = mock_embedding_client.post("/v1/embeddings", json={"model": "mock-3", "input": ["A", "B"]})
    assert response.status_code == 200
    payload = response.json()
    assert [item["index"] for item in payload["data"]] == [0, 1]
    assert len(payload["data"][0]["embedding"]) == 3
    assert payload == mock_embedding_client.post(
        "/v1/embeddings", json={"model": "mock-3", "input": ["A", "B"]}
    ).json()
```

- [ ] **Step 2: Run and observe missing mock app**

Run: `python -m pytest tests/support/test_mock_embedding_app.py -v`

Expected: FAIL on missing module.

- [ ] **Step 3: Implement deterministic finite vectors and fault controls**

Generate each vector from SHA-256 bytes normalized into `[-1, 1]`; never use Python's randomized `hash()`. Model names `mock-N` select dimension N, defaulting to 8. Require any Bearer token but never log it. A test-only environment variable enables forced 429/401/delay/reversed-order scenarios; production Compose profile does not expose this service.

- [ ] **Step 4: Run mock tests and healthcheck**

Run:

```bash
python -m pytest tests/support/test_mock_embedding_app.py -v
docker compose --profile test config
```

Expected: PASS; mock service exists only in the test profile and has a `/health` check.

- [ ] **Step 5: Commit mock service**

```bash
git add tests/support/mock_embedding_app.py tests/support/test_mock_embedding_app.py docker-compose.yml
git commit -m "test: add deterministic embedding service"
```

### Task 2: High-Quality General and Parent-Child E2E

**Interfaces:**
- Drives only public dataset/document/preview/job APIs.
- Reads PostgreSQL/Milvus afterward to verify IDs and counts.

- [ ] **Step 1: Write failing high-quality E2E tests**

```python
# tests/e2e/test_high_quality_flow.py
def test_high_quality_general_index_end_to_end(e2e):
    dataset = e2e.create_dataset("General E2E")
    documents = e2e.upload(dataset["id"], ["sample.txt", "sample.md", "sample.pdf", "sample.docx", "orders.xls", "orders.xlsx", "orders.csv"])
    preview = e2e.preview_general(dataset["id"], documents)
    assert preview["total_chunks"] > 0

    job = e2e.create_high_quality_job(dataset["id"], documents, model="mock-8")
    completed = e2e.wait_job(dataset["id"], job["job_id"], "completed")
    assert completed["failed_documents"] == 0
    e2e.assert_all_indexable_segments_have_vectors(job["job_id"])
    e2e.assert_postgres_vector_column_is_null(job["job_id"])


# tests/e2e/test_parent_child_flow.py
def test_parent_child_only_embeds_children(e2e):
    dataset, documents = e2e.dataset_with_file("parent-child.md")
    preview = e2e.preview_parent_child(dataset["id"], documents)
    assert preview["total_parents"] > 0 and preview["total_children"] > 0
    job = e2e.create_parent_child_job(dataset["id"], documents, model="mock-8")
    e2e.wait_job(dataset["id"], job["job_id"], "completed")
    e2e.assert_vector_ids_equal_child_segment_ids(job["job_id"])
```

- [ ] **Step 2: Start stack and run tests to expose integration gaps**

Run:

```bash
docker compose --profile app --profile test up -d --build
RUN_E2E=1 python -m pytest tests/e2e/test_high_quality_flow.py tests/e2e/test_parent_child_flow.py -v
```

Expected: initial FAIL identifies the first broken public contract or cross-system invariant; do not weaken assertions to obtain PASS.

- [ ] **Step 3: Implement E2E fixtures and fix only discovered integration seams**

Fixtures create unique dataset names and object/collection prefixes, poll with bounded timeouts, query segment IDs from PostgreSQL, query entity IDs/counts from the target `dataset_indexes.collection_name`, and register cleanup in `yield` teardown. Add all seven small format fixtures without sensitive content.

- [ ] **Step 4: Re-run high-quality E2E**

Run: `RUN_E2E=1 python -m pytest tests/e2e/test_high_quality_flow.py tests/e2e/test_parent_child_flow.py -v`

Expected: PASS; general segments or child segments have exactly one Milvus entity each and PostgreSQL vectors are null.

- [ ] **Step 5: Commit high-quality E2E**

```bash
git add tests/e2e/conftest.py tests/e2e/test_high_quality_flow.py tests/e2e/test_parent_child_flow.py tests/fixtures/documents
git commit -m "test: verify complete high quality indexing"
```

### Task 3: Economy Index E2E

**Interfaces:**
- Verifies keyword-only indexing and zero Embedding/Milvus interaction.

- [ ] **Step 1: Write failing economy E2E**

```python
# tests/e2e/test_economy_flow.py
def test_economy_index_uses_postgres_keywords_only(e2e, mock_embedding_metrics):
    dataset, documents = e2e.dataset_with_file("orders.csv")
    before_calls = mock_embedding_metrics.calls
    job = e2e.create_economy_job(dataset["id"], documents)
    e2e.wait_job(dataset["id"], job["job_id"], "completed")

    segments = e2e.load_segments(job["job_id"])
    assert segments and all(segment.keywords for segment in segments)
    assert all(segment.embedding_status == "not_required" for segment in segments)
    assert mock_embedding_metrics.calls == before_calls
    assert e2e.dataset_index(job["job_id"]).collection_name is None
```

- [ ] **Step 2: Run and confirm behavior**

Run: `RUN_E2E=1 python -m pytest tests/e2e/test_economy_flow.py -v`

Expected: FAIL until keyword-only end-to-end status and API details are wired correctly.

- [ ] **Step 3: Fix economy-only integration seams**

Ensure the job snapshot omits embedding model/provider, target index has no collection/dimension, progress skips embedding/indexing stages, every completed segment has keywords, and no vector provider is resolved for economy jobs.

- [ ] **Step 4: Re-run economy E2E**

Run: `RUN_E2E=1 python -m pytest tests/e2e/test_economy_flow.py -v`

Expected: PASS with zero new mock embedding requests and no Milvus collection.

- [ ] **Step 5: Commit economy E2E**

```bash
git add tests/e2e/test_economy_flow.py
git commit -m "test: verify economy keyword indexing"
```

### Task 4: Duplicate Delivery, Worker Loss, and Failed Rebuild

**Interfaces:**
- Verifies `acks_late`, lease recovery, stable IDs and active-version protection.

- [ ] **Step 1: Write failing resilience scenarios**

```python
# tests/e2e/test_recovery_and_versioning.py
def test_duplicate_dispatch_keeps_one_segment_per_stable_id(e2e):
    dataset, documents = e2e.dataset_with_file("sample.txt")
    job = e2e.create_high_quality_job(dataset["id"], documents, dispatch=False)
    e2e.dispatch_job_twice(job["job_id"])
    e2e.wait_job(dataset["id"], job["job_id"], "completed")
    assert e2e.segment_count(job["job_id"]) == e2e.distinct_segment_count(job["job_id"])
    assert e2e.vector_count(job["job_id"]) == e2e.distinct_vector_id_count(job["job_id"])


def test_failed_full_rebuild_preserves_old_active_index(e2e, mock_embedding_faults):
    dataset, documents = e2e.completed_dataset("sample.txt", model="mock-8")
    old_index_id = e2e.active_index(dataset["id"]).id
    mock_embedding_faults.fail_model("mock-16", status=401)
    job = e2e.create_high_quality_job(dataset["id"], documents, model="mock-16")
    e2e.wait_job(dataset["id"], job["job_id"], "failed")
    assert e2e.active_index(dataset["id"]).id == old_index_id
```

- [ ] **Step 2: Run resilience tests**

Run: `RUN_E2E=1 python -m pytest tests/e2e/test_recovery_and_versioning.py -v`

Expected: FAIL if any duplicate, lease or version-switch invariant is incomplete.

- [ ] **Step 3: Add controlled Worker-loss scenario**

Use a test-only parser/embedding delay, wait until the job-document row is running with a lease, stop only the Compose `celery-worker` service, restart it, wait past lease recovery, and assert the same job reaches completed without duplicate segment/vector IDs. The fixture must restart the worker in `finally` even when assertions fail.

- [ ] **Step 4: Run all resilience scenarios**

Run: `RUN_E2E=1 python -m pytest tests/e2e/test_recovery_and_versioning.py -v`

Expected: PASS for duplicate dispatch, Worker loss/restart and failed rebuild preserving the old active version.

- [ ] **Step 5: Commit resilience coverage and fixes**

```bash
git add tests/e2e/test_recovery_and_versioning.py tests/e2e/conftest.py
git commit -m "test: verify indexing recovery and version safety"
```

### Task 5: Deletion and Compensation E2E

**Interfaces:**
- Verifies document and dataset deletion eventually remove external resources while API returns promptly.

- [ ] **Step 1: Write failing cleanup E2E**

```python
# tests/e2e/test_deletion_cleanup.py
def test_delete_document_soft_deletes_then_purges_vector_and_object(e2e):
    dataset, document, index = e2e.completed_single_document_dataset()
    response = e2e.delete_document(dataset.id, document.id)
    assert response.status_code == 204
    assert e2e.document_row(document.id).deleted_at is not None
    e2e.wait_until(lambda: not e2e.minio_object_exists(document.object_key))
    e2e.wait_until(lambda: e2e.milvus_document_count(index.collection_name, document.id) == 0)


def test_delete_dataset_purges_all_version_collections_and_objects(e2e):
    dataset = e2e.dataset_with_active_and_retired_indexes()
    collections = e2e.all_collection_names(dataset.id)
    object_keys = e2e.all_object_keys(dataset.id)
    assert e2e.delete_dataset(dataset.id).status_code == 204
    e2e.wait_until(lambda: all(not e2e.collection_exists(name) for name in collections))
    e2e.wait_until(lambda: all(not e2e.minio_object_exists(key) for key in object_keys))
```

- [ ] **Step 2: Run and identify compensation gaps**

Run: `RUN_E2E=1 python -m pytest tests/e2e/test_deletion_cleanup.py -v`

Expected: FAIL until maintenance scan and purge markers are complete.

- [ ] **Step 3: Fix only exact cleanup gaps and verify idempotency**

Ensure cleanup considers active, retired, failed and building versions; marks object cleanup state only after success; treats missing resources as success; and can be called twice. Add a publish-failure case by creating a deleted row without a message and wait for Beat to discover it.

- [ ] **Step 4: Re-run cleanup E2E twice**

Run:

```bash
RUN_E2E=1 python -m pytest tests/e2e/test_deletion_cleanup.py -v
RUN_E2E=1 python -m pytest tests/e2e/test_deletion_cleanup.py -v
```

Expected: both runs PASS without leaked test resources.

- [ ] **Step 5: Commit deletion E2E**

```bash
git add tests/e2e/test_deletion_cleanup.py
git commit -m "test: verify dataset resource cleanup"
```

### Task 6: Remove Legacy JSON/Milvus UI Contracts and Protect Secrets

**Interfaces:**
- Produces no active `knowledge_bases.json` reference and no obsolete vector-store DTO imports.
- Produces `.env.example` with names/placeholders only.

- [ ] **Step 1: Write failing legacy/security tests**

```python
# tests/unit/test_no_legacy_json_store.py
def test_runtime_has_no_knowledge_base_json_store_reference():
    runtime = Path("main.py").read_text(encoding="utf-8")
    assert "knowledge_bases.json" not in runtime
    assert "knowledge_base.json" not in runtime
    assert "read_store" not in runtime
    assert "write_store" not in runtime


# tests/unit/test_secret_safety.py
def test_openapi_and_options_do_not_expose_infrastructure_secrets(client):
    text = client.get("/openapi.json").text + client.get("/api/indexing/options").text
    for forbidden in ["api_key", "secret_key", "rabbitmq_password", "minio_secret", "milvus_token"]:
        assert forbidden not in text.lower()
```

- [ ] **Step 2: Run and observe old `main.py` reference failure**

Run: `python -m pytest tests/unit/test_no_legacy_json_store.py tests/unit/test_secret_safety.py -v`

Expected: FAIL because current `main.py` defines `DATA_FILE` and contains legacy store functions/comments.

- [ ] **Step 3: Delete exact legacy code/files and add safe templates**

Reduce `main.py` to application construction, middleware/static frontend and router registration. Remove unused `json`, regex/threading/vector DTO imports and the entire commented JSON/Milvus implementation. Delete only `data/knowledge_bases.json` and `data/knowledge_base.json` if tracked/present; do not delete `data/graph_rag.db` or broad `data/`.

After `rg` proves zero imports, delete obsolete `vectorStoreConfig.py`. Add `.gitignore` entries for `.env`, `__pycache__`, `.pytest_cache`, `.idea`, `frontend/node_modules`, runtime `data`, and Compose volumes without deleting current user data. Add `.env.example` with placeholder values and at least one non-secret mock model definition.

- [ ] **Step 4: Run legacy/security and route regression tests**

Run:

```bash
python -m pytest tests/unit/test_no_legacy_json_store.py tests/unit/test_secret_safety.py tests/test_api_routes.py -v
rg -n "knowledge_bases\.json|knowledge_base\.json|VectorStoreConfig|MILVUS.*(host|port|token)" main.py rag_modules frontend/src
```

Expected: tests PASS; `rg` finds no JSON store or frontend Milvus config, with backend-only Milvus settings allowed after manual review.

- [ ] **Step 5: Commit legacy cleanup**

```bash
git add main.py .gitignore rag_modules/config/.env.example tests/unit/test_no_legacy_json_store.py tests/unit/test_secret_safety.py rag_modules/api/dto/other/__init__.py
git add -u rag_modules/api/dto/other/vectorStoreConfig.py
git commit -m "refactor: remove legacy knowledge base json flow"
```

### Task 7: Operations Documentation and Final Verification

**Interfaces:**
- Produces exact developer commands for API, migrations, Worker, Beat, test stack and failure diagnosis.

- [ ] **Step 1: Add a documentation verification checklist test**

```python
# tests/unit/test_readme_commands.py
def test_readme_documents_required_services_and_commands():
    readme = Path("README.md").read_text(encoding="utf-8")
    for required in [
        "alembic upgrade head",
        "celery-worker",
        "celery-beat",
        "RabbitMQ",
        "MinIO",
        "OpenAI-compatible",
        "npm --prefix frontend run build",
    ]:
        assert required in readme
```

- [ ] **Step 2: Run and confirm documentation gaps**

Run: `python -m pytest tests/unit/test_readme_commands.py -v`

Expected: FAIL until README includes every required operational path.

- [ ] **Step 3: Document startup, configuration, status, and recovery**

Update README with:

1. Environment variable table using placeholder values.
2. `docker compose` infrastructure and app-profile commands.
3. Alembic upgrade/downgrade/current commands.
4. Standalone API, Worker and Beat commands.
5. Supported file formats and no-OCR limitation.
6. High-quality/economy and parent-child constraints.
7. How to inspect PostgreSQL job state, RabbitMQ queue, MinIO object and Milvus collection without exposing credentials.
8. Retry/cancel/stale recovery behavior.
9. Backend/frontend/integration/E2E test commands.

- [ ] **Step 4: Run the complete verification matrix**

Run:

```bash
python -m pytest tests/unit tests/api -q
RUN_INTEGRATION=1 python -m pytest tests/integration -v
RUN_E2E=1 python -m pytest tests/e2e -v
npm --prefix frontend test -- --run
npm --prefix frontend run build
python -m alembic current
docker compose --profile app --profile test config
git diff --check
```

Expected: every command exits 0; Alembic is at head; Compose contains RabbitMQ and no Redis; all 30 spec acceptance outcomes are represented by passing assertions.

- [ ] **Step 5: Commit operations documentation**

```bash
git add README.md tests/unit/test_readme_commands.py
git commit -m "docs: document complete dataset indexing operations"
```

## Final Completion Procedure

Before claiming the feature complete:

1. Invoke `superpowers:verification-before-completion`.
2. Re-run the full verification matrix without relying on old output.
3. Record exact passed test counts and build result.
4. Inspect `git status --short` and distinguish pre-existing user changes from implementation commits.
5. Use `superpowers:requesting-code-review` for a final spec/implementation review.
6. Only after review is clean, use `superpowers:finishing-a-development-branch` to offer merge/integration options.

## Official Implementation References

- Celery late acknowledgement and idempotency: <https://docs.celeryq.dev/en/stable/userguide/tasks.html>
- Celery prefetch behavior: <https://docs.celeryq.dev/en/latest/userguide/optimizing.html>
- Alembic asyncio migration pattern: <https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic>
- MinIO Python client API: <https://docs.min.io/aistor/developers/sdk/python/api/>
- PyMilvus schema API: <https://milvus.io/api-reference/pymilvus/v2.5.x/MilvusClient/Collections/create_schema.md>
- RabbitMQ reliability and durable messages: <https://www.rabbitmq.com/docs/reliability>
