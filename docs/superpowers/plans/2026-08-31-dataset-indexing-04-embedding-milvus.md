# Embedding, Keywords, and Milvus Indexing Primitives Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现可独立测试的 OpenAI-compatible Embedding、经济关键词、Milvus collection/upsert 和单文档索引执行引擎。

**Architecture:** `DocumentIndexingEngine` 组合已有 parser/segmenter、segment repository、Embedding 或关键词策略以及 Milvus provider。引擎不依赖 Celery，后续 Worker 只负责领取任务、提供命令和上报进度。

**Tech Stack:** HTTPX、jieba、PyMilvus 2.5、SQLAlchemy async、pytest、respx。

**Spec:** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## Global Constraints

- Embedding 请求使用 `{base_url}/embeddings` 和 Bearer API Key。
- 第一批响应确定维度；同一索引版本后续维度必须一致。
- API Key、完整正文和完整向量不得进入日志或数据库错误字段。
- Milvus entity 主键必须等于 `document_segments.id`。
- Milvus 不保存正文，只保存设计中批准的过滤字段。
- 父块写 PostgreSQL 但不生成或写入向量。
- 经济索引完全绕过 Embedding 和 Milvus。
- segment ID 必须稳定且重试幂等。

---

## File Structure

- Create: `rag_modules/embeddings/models.py` — embedding batch/result/errors。
- Create: `rag_modules/embeddings/openai_compatible.py` — HTTP client。
- Create: `rag_modules/indexing/keywords.py` — 经济关键词提取。
- Modify: `rag_modules/vector_stores/base.py` — 完整向量协议。
- Modify: `rag_modules/vector_stores/milvus.py` — schema、index、upsert、count、delete。
- Modify: `rag_modules/vector_stores/factory.py` — Milvus-only implemented provider behavior。
- Create: `rag_modules/indexing/models.py` — `IndexDocumentCommand` 和结果。
- Create: `rag_modules/indexing/ids.py` — UUIDv5 segment ID。
- Create: `rag_modules/repositories/segment_repository.py` — staging/activate/soft-delete。
- Create: `rag_modules/indexing/engine.py` — 单文档执行引擎。
- Test: `tests/unit/embeddings/test_openai_compatible.py`。
- Test: `tests/unit/indexing/test_keywords.py`。
- Test: `tests/unit/vector_stores/test_milvus_store.py`。
- Test: `tests/unit/indexing/test_document_engine.py`。
- Test: `tests/integration/test_milvus_index.py`。

### Task 1: OpenAI-Compatible Embedding Client

**Interfaces:**
- Produces: `OpenAICompatibleEmbeddingClient.embed(model_id: str, texts: Sequence[str]) -> EmbeddingBatch`。
- `EmbeddingBatch.vectors: tuple[tuple[float, ...], ...]` and `dimension: int`。
- Raises typed `EmbeddingError(code, retryable, safe_message)`。

- [ ] **Step 1: Write failing protocol tests with `respx`**

```python
# tests/unit/embeddings/test_openai_compatible.py
@pytest.mark.asyncio
async def test_embed_restores_response_index_order(respx_mock):
    route = respx_mock.post("http://embed/v1/embeddings").mock(
        return_value=httpx.Response(200, json={
            "data": [
                {"index": 1, "embedding": [0.3, 0.4]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]
        })
    )
    client = make_client(base_url="http://embed/v1", api_key="secret")

    result = await client.embed("bge-m3", ["first", "second"])

    assert result.vectors == ((0.1, 0.2), (0.3, 0.4))
    assert result.dimension == 2
    assert route.calls[0].request.headers["authorization"] == "Bearer secret"


@pytest.mark.asyncio
async def test_unauthorized_is_safe_and_not_retryable(respx_mock):
    respx_mock.post("http://embed/v1/embeddings").mock(return_value=httpx.Response(401, text="secret backend detail"))
    with pytest.raises(EmbeddingError) as error:
        await make_client().embed("bge-m3", ["private input"])
    assert error.value.code == "EMBEDDING_AUTH_FAILED"
    assert error.value.retryable is False
    assert "private input" not in str(error.value)
    assert "secret backend detail" not in str(error.value)
```

- [ ] **Step 2: Run and observe missing client**

Run: `python -m pytest tests/unit/embeddings/test_openai_compatible.py -v`

Expected: FAIL on missing module.

- [ ] **Step 3: Implement batching, validation, and retry classification**

Use one reusable `httpx.AsyncClient`. Resolve the selected model from `EmbeddingSettings.get_model`, reject inputs over `max_input_characters`, batch by the model batch size, and send:

```python
payload = {"model": definition.model, "input": list(batch_texts)}
headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"}
```

Sort response entries by `index`; require indexes `0..n-1`, exact count, equal non-zero dimensions and only finite floats. Classify 429/502/503/504/timeouts as retryable; 400/401/403/404 as non-retryable. Perform bounded exponential retries inside a batch and halve batch size after a repeatable batch-size failure without logging texts.

- [ ] **Step 4: Run full embedding tests**

Run: `python -m pytest tests/unit/embeddings/test_openai_compatible.py -v`

Expected: PASS for normal, response reordering, count mismatch, inconsistent dimensions, NaN, timeout, 429 retry, 401 no retry and adaptive batch reduction.

- [ ] **Step 5: Commit embedding client**

```bash
git add rag_modules/embeddings tests/unit/embeddings/test_openai_compatible.py
git commit -m "feat: add openai compatible embeddings"
```

### Task 2: Deterministic Economy Keywords

**Interfaces:**
- Produces: `KeywordExtractor.extract(text: str, limit: int = 15) -> list[str]`。
- Output order is deterministic by score then normalized term.

- [ ] **Step 1: Write failing multilingual tests**

```python
# tests/unit/indexing/test_keywords.py
def test_keywords_keep_chinese_terms_and_business_identifiers():
    text = "订单 A001 已发货。订单 A001 属于华东客户，客户需要发票。"
    words = KeywordExtractor().extract(text, limit=5)
    assert "A001" in words
    assert "订单" in words
    assert "客户" in words
    assert len(words) <= 5


def test_keywords_are_deterministic_and_remove_stopwords():
    extractor = KeywordExtractor()
    assert extractor.extract("the graph graph and retrieval") == extractor.extract("the graph graph and retrieval")
    assert "the" not in extractor.extract("the graph graph and retrieval")
```

- [ ] **Step 2: Run and confirm extractor absence**

Run: `python -m pytest tests/unit/indexing/test_keywords.py -v`

Expected: FAIL on import.

- [ ] **Step 3: Implement token normalization and TF-based scoring**

Use jieba for CJK, a Unicode word regex for Latin text and identifiers, lowercase Latin words while preserving the original normalized identifier, filter explicit Chinese/English stopword sets, and score by term frequency plus an identifier bonus. Resolve equal scores lexicographically so retries produce identical arrays.

- [ ] **Step 4: Run keyword tests**

Run: `python -m pytest tests/unit/indexing/test_keywords.py -v`

Expected: PASS for Chinese, English, identifiers, limits, empty text and deterministic order.

- [ ] **Step 5: Commit economy keywords**

```bash
git add rag_modules/indexing/keywords.py tests/unit/indexing/test_keywords.py
git commit -m "feat: extract economy index keywords"
```

### Task 3: Full Milvus Vector Store Protocol

**Interfaces:**
- Produces: `ensure_collection(collection_name, dimension, metric_type="COSINE")`。
- Produces: `upsert(collection_name, entities) -> int`。
- Produces: `count(collection_name) -> int`。
- Produces: `delete_ids(collection_name, ids)`、`delete_document(collection_name, document_id)`、`drop_collection(collection_name)`。
- Entity fields: `id`, `embedding`, `dataset_id`, `document_id`, `dataset_index_id`, `parent_id`, `position`。

- [ ] **Step 1: Write failing Milvus adapter tests**

```python
# tests/unit/vector_stores/test_milvus_store.py
def test_ensure_collection_builds_explicit_schema_and_hnsw_index():
    client = RecordingMilvusClient()
    store = MilvusVectorStore(client_factory=lambda: client)

    store.ensure_collection("graph_rag_d_i", 3, "COSINE")

    assert client.schema.fields["id"].is_primary is True
    assert client.schema.fields["embedding"].params["dim"] == 3
    assert client.index.field_name == "embedding"
    assert client.index.index_type == "HNSW"
    assert client.index.metric_type == "COSINE"


def test_upsert_uses_segment_id_as_primary_key():
    client = RecordingMilvusClient(existing=True)
    count = MilvusVectorStore(client_factory=lambda: client).upsert("collection", [vector_entity("segment-1")])
    assert count == 1
    assert client.upserted[0]["id"] == "segment-1"
    assert "content" not in client.upserted[0]
```

- [ ] **Step 2: Run and observe protocol mismatch**

Run: `python -m pytest tests/unit/vector_stores/test_milvus_store.py -v`

Expected: FAIL because current provider only provisions/drops a quick collection.

- [ ] **Step 3: Implement explicit PyMilvus 2.5 schema**

Use `MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)`, add VARCHAR ID fields with explicit max lengths, nullable parent ID, INT64 position and FLOAT_VECTOR embedding. Use `prepare_index_params().add_index(field_name="embedding", index_type="HNSW", metric_type="COSINE", params={"M": 16, "efConstruction": 200})`, then create/load the collection.

Before accepting an existing collection, inspect its schema and reject dimension/metric mismatch with `VectorSchemaMismatch`. Chunk upserts and deletes by configured batch size, call flush before count-based validation, and allow for bounded Milvus consistency polling rather than trusting an immediate stale count. Filter document deletion with escaped equality on the trusted UUID document ID.

- [ ] **Step 4: Run Milvus unit tests**

Run: `python -m pytest tests/unit/vector_stores/test_milvus_store.py -v`

Expected: PASS for create, existing validation, upsert, count, delete IDs/document and idempotent drop.

- [ ] **Step 5: Commit Milvus adapter**

```bash
git add rag_modules/vector_stores/base.py rag_modules/vector_stores/milvus.py rag_modules/vector_stores/factory.py tests/unit/vector_stores/test_milvus_store.py
git commit -m "feat: add milvus indexing operations"
```

### Task 4: Stable Segment Persistence

**Interfaces:**
- Produces: `stable_segment_id(dataset_index_id, document_id, parent_id, position, content_hash) -> str`。
- Produces: `SegmentRepository.stage(document, index, job, segments) -> list[DocumentSegmentRecord]`。
- Produces: `activate_document_segments(...)` and `soft_delete_previous_segments(...)`。

- [ ] **Step 1: Write failing ID and repository tests**

```python
# tests/unit/indexing/test_segment_persistence.py
def test_segment_id_is_stable_and_sensitive_to_index_version():
    first = stable_segment_id("index-1", "doc-1", None, 0, "hash")
    assert first == stable_segment_id("index-1", "doc-1", None, 0, "hash")
    assert first != stable_segment_id("index-2", "doc-1", None, 0, "hash")
    assert len(first) == 32


@pytest.mark.asyncio
async def test_stage_parent_child_resolves_parent_database_id(segment_repository):
    records = await segment_repository.stage(command(), parent_child_preview_segments())
    parent = next(record for record in records if record.index_type == "parent")
    child = next(record for record in records if record.index_type == "child")
    assert child.parent_id == parent.id
    assert child.status == "indexing"
    assert child.embedding_status == "waiting"
```

- [ ] **Step 2: Run and verify missing persistence layer**

Run: `python -m pytest tests/unit/indexing/test_segment_persistence.py -v`

Expected: FAIL on imports.

- [ ] **Step 3: Implement UUIDv5 IDs and conflict-safe staging**

Use a fixed application namespace UUID and `uuid5(namespace, canonical_string).hex`. Hash normalized content plus canonical JSON source metadata with SHA-256. Parents get `embedding_status="not_required"`; high-quality general/child segments get `waiting`; economy segments get `not_required`. Upsert exact retry IDs without creating duplicates, but reject an existing same ID with different content hash.

Final-review clarification: validate every candidate before the first database
mutation, issue conflict-safe inserts and all-ID reloads in explicit batches of at
most 500 records, and keep the caller-owned transaction. An exact retry is valid
only for a non-deleted `indexing` row with technique-compatible embedding state:
high-quality general/child rows may be `waiting` or `completed`, while parent and
economy rows remain `not_required`. Repository SQLAlchemy failures cross a fixed,
typed, content-free storage-error boundary; validation and programmer errors do
not. Every application and migration async engine hides bound parameters as
defense in depth.

- [ ] **Step 4: Run persistence tests**

Run: `python -m pytest tests/unit/indexing/test_segment_persistence.py -v`

Expected: PASS for retry, parent mapping, status, hashes and previous-segment soft deletion.

- [ ] **Step 5: Commit segment persistence**

```bash
git add rag_modules/indexing/models.py rag_modules/indexing/ids.py rag_modules/repositories/segment_repository.py tests/unit/indexing/test_segment_persistence.py
git commit -m "feat: stage deterministic document segments"
```

### Task 5: Single-Document Indexing Engine

**Interfaces:**
- Produces: `DocumentIndexingEngine.run(command, progress) -> IndexDocumentResult`。
- `IndexDocumentCommand` contains immutable IDs/config snapshot, independently validated `embedding_batch_size` and `vector_batch_size` snapshots, and `collection_name`/expected dimension when high quality.
- `ProgressReporter.update(stage: str, progress: int, processed_segments: int)` and `check_cancelled()`.
- Consumes: `VectorTargetResolver.resolve(index_id: str, discovered_dimension: int) -> Awaitable[VectorTarget]`; phase 5 supplies the PostgreSQL/Milvus coordinator and unit tests supply a fake.

- [ ] **Step 1: Write failing high-quality and economy engine tests**

```python
# tests/unit/indexing/test_document_engine.py
@pytest.mark.asyncio
async def test_high_quality_indexes_general_segments_and_leaves_pg_vector_empty():
    engine, deps = make_engine(indexing_technique="high_quality")
    result = await engine.run(high_quality_command(), RecordingProgress())
    assert result.vector_count == result.total_indexable_segments
    assert all(record.vector is None for record in deps.segment_repository.records)
    assert [entity["id"] for entity in deps.vector_store.entities] == [
        record.id for record in deps.segment_repository.records if record.index_type != "parent"
    ]


@pytest.mark.asyncio
async def test_parent_child_embeds_only_children():
    engine, deps = make_engine(parent_child=True)
    await engine.run(parent_child_command(), RecordingProgress())
    assert {entity["id"] for entity in deps.vector_store.entities} == {
        record.id for record in deps.segment_repository.records if record.index_type == "child"
    }


@pytest.mark.asyncio
async def test_economy_never_calls_embedding_or_milvus():
    engine, deps = make_engine(indexing_technique="economy", forbidden_external=True)
    result = await engine.run(economy_command(), RecordingProgress())
    assert result.vector_count == 0
    assert all(record.keywords for record in deps.segment_repository.records)
```

- [ ] **Step 2: Run and verify engine absence**

Run: `python -m pytest tests/unit/indexing/test_document_engine.py -v`

Expected: FAIL on missing engine.

- [ ] **Step 3: Implement the exact execution sequence**

Implement:

```text
download → parse → split → stage → embed-or-keywords → vector-upsert → validate
```

For an existing index, the command supplies its resolved collection/dimension. For a building index, the engine embeds the first non-empty batch, obtains `discovered_dimension`, calls `VectorTargetResolver.resolve`, then writes that batch and all remaining batches to the returned collection. Call `progress.check_cancelled()` between stages and every batch. Mark segment embedding status after the corresponding vector batch succeeds; validate vector count against indexable segment count. Do not activate segments in this engine—phase 5 finalization owns activation and version switching.

Final-review clarification: one engine streaming unit is
`min(1024, embedding_batch_size, vector_batch_size)`, so normally one Embedding
request corresponds to one submitted Milvus upsert and one PostgreSQL status
acknowledgement. The Embedding client may still adaptively split a rejected HTTP
request, and the Milvus adapter retains defensive chunking, but configured engine
batches do not exceed either snapshot. Before either external dependency is
called, staged rows must be non-deleted, remain `indexing`, and have the
technique-compatible embedding state described in Task 4. A high-quality retry
skips rows already `completed` and processes only `waiting` rows. On successful
return, `IndexDocumentResult.vector_count` means total ready indexable rows for
the document (previously completed plus newly acknowledged), not writes made by
this invocation and not collection-wide cardinality; Phase 5 owns independent
collection reconciliation.

- [ ] **Step 4: Run engine regression tests**

Run: `python -m pytest tests/unit/indexing/test_document_engine.py tests/unit/embeddings tests/unit/vector_stores tests/unit/indexing/test_keywords.py -v`

Expected: PASS for high-quality, parent-child, economy, cancellation, empty parse, dimension mismatch and partial vector failure.

- [ ] **Step 5: Commit indexing engine**

```bash
git add rag_modules/indexing/engine.py tests/unit/indexing/test_document_engine.py
git commit -m "feat: index one document into postgres and milvus"
```

### Task 6: Real Milvus Integration

**Interfaces:**
- Verifies explicit schema and idempotent upsert against Compose Milvus 2.5.14.

- [ ] **Step 1: Write an isolated integration test**

```python
# tests/integration/test_milvus_index.py
@pytest.mark.integration
def test_milvus_collection_upsert_count_delete_and_drop(real_milvus_store):
    collection = f"test_{uuid4().hex}"
    try:
        real_milvus_store.ensure_collection(collection, 3, "COSINE")
        real_milvus_store.upsert(collection, [vector_entity("segment-1", [0.1, 0.2, 0.3])])
        real_milvus_store.upsert(collection, [vector_entity("segment-1", [0.2, 0.3, 0.4])])
        assert real_milvus_store.count(collection) == 1
        real_milvus_store.delete_ids(collection, ["segment-1"])
        assert real_milvus_store.count(collection) == 0
    finally:
        real_milvus_store.drop_collection(collection)
```

- [ ] **Step 2: Run with integration disabled**

Run: `python -m pytest tests/integration/test_milvus_index.py -v`

Expected: SKIP unless `RUN_INTEGRATION=1`.

- [ ] **Step 3: Add a real Milvus fixture using only unique collection names**

Build the fixture from `settings.vector_store`; cleanup only names created by the test and never list/drop arbitrary application collections.

- [ ] **Step 4: Run against Compose Milvus**

Run:

```bash
docker compose up -d etcd minio standalone
RUN_INTEGRATION=1 python -m pytest tests/integration/test_milvus_index.py -v
```

Expected: PASS with one entity after repeated upsert and zero leaked test collections.

- [ ] **Step 5: Commit Milvus integration coverage**

```bash
git add tests/conftest.py tests/integration/test_milvus_index.py
git commit -m "test: verify real milvus indexing"
```

## Phase Verification

Run:

```bash
python -m pytest tests/unit/embeddings tests/unit/indexing tests/unit/vector_stores -v
RUN_INTEGRATION=1 python -m pytest tests/integration/test_milvus_index.py -v
git diff --check
```

Expected: OpenAI-compatible and Milvus primitives are independently verified, economy mode never reaches either service, and PostgreSQL vector values remain unset.
