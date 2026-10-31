# Embedding、关键词与 Milvus 索引原语实施计划

> **面向智能体工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项执行本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 实现可独立测试的 OpenAI-compatible Embedding、经济关键词、Milvus collection/upsert 和单文档索引执行引擎。

**架构：** `DocumentIndexingEngine` 组合已有 parser/segmenter、segment repository、Embedding 或关键词策略以及 Milvus provider。引擎不依赖 Celery，后续 Worker 只负责领取任务、提供命令和上报进度。

**技术栈：** HTTPX、jieba、PyMilvus 2.5、SQLAlchemy async、pytest、respx。

**规格：** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## 全局约束

- Embedding 请求使用 `{base_url}/embeddings` 和 Bearer API Key。
- 第一批响应确定维度；同一索引版本后续维度必须一致。
- API Key、完整正文和完整向量不得进入日志或数据库错误字段。
- Milvus entity 主键必须等于 `document_segments.id`。
- Milvus 不保存正文，只保存设计中批准的过滤字段。
- 父块写 PostgreSQL 但不生成或写入向量。
- 经济索引完全绕过 Embedding 和 Milvus。
- segment ID 必须稳定且重试幂等。

---

## 文件结构

- 创建：`rag_modules/embeddings/models.py` — embedding batch/result/errors。
- 创建：`rag_modules/embeddings/openai_compatible.py` — HTTP 客户端。
- 创建：`rag_modules/indexing/keywords.py` — 经济模式关键词提取。
- 修改：`rag_modules/vector_stores/base.py` — 完整向量协议。
- 修改：`rag_modules/vector_stores/milvus.py` — schema、index、upsert、count、delete。
- 修改：`rag_modules/vector_stores/factory.py` — 仅实现 Milvus provider 行为。
- 创建：`rag_modules/indexing/models.py` — `IndexDocumentCommand` 和结果。
- 创建：`rag_modules/indexing/ids.py` — UUIDv5 segment ID。
- 创建：`rag_modules/repositories/segment_repository.py` — staging/activate/soft-delete。
- 创建：`rag_modules/indexing/engine.py` — 单文档执行引擎。
- 测试：`tests/unit/embeddings/test_openai_compatible.py`。
- 测试：`tests/unit/indexing/test_keywords.py`。
- 测试：`tests/unit/vector_stores/test_milvus_store.py`。
- 测试：`tests/unit/indexing/test_document_engine.py`。
- 测试：`tests/integration/test_milvus_index.py`。

### 任务 1：OpenAI-compatible Embedding 客户端

**接口：**
- 提供：`OpenAICompatibleEmbeddingClient.embed(model_id: str, texts: Sequence[str]) -> EmbeddingBatch`。
- `EmbeddingBatch.vectors: tuple[tuple[float, ...], ...]` 和 `dimension: int`。
- 抛出类型化的 `EmbeddingError(code, retryable, safe_message)`。

- [ ] **步骤 1：使用 `respx` 编写失败的协议测试**

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

- [ ] **步骤 2：运行并观察客户端缺失**

运行：`python -m pytest tests/unit/embeddings/test_openai_compatible.py -v`

预期：因模块缺失而失败。

- [ ] **步骤 3：实现批处理、校验和重试分类**

使用一个可复用的 `httpx.AsyncClient`。通过 `EmbeddingSettings.get_model` 解析所选模型，拒绝超过 `max_input_characters` 的输入，按模型批大小分批，并发送：

```python
payload = {"model": definition.model, "input": list(batch_texts)}
headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"}
```

按 `index` 对响应条目排序；要求索引为 `0..n-1`、数量精确、维度相同且非零，并且只包含有限浮点数。将 429/502/503/504/超时归类为可重试，将 400/401/403/404 归类为不可重试。在批次内执行有界指数退避重试；遇到可复现的批大小错误时将批大小减半，且不记录文本。

- [ ] **步骤 4：运行完整 Embedding 测试**

运行: `python -m pytest tests/unit/embeddings/test_openai_compatible.py -v`

预期：正常响应、响应乱序、数量不匹配、维度不一致、NaN、超时、429 重试、401 不重试以及自适应减小批大小等测试均通过。

- [ ] **步骤 5：提交 Embedding 客户端**

```bash
git add rag_modules/embeddings tests/unit/embeddings/test_openai_compatible.py
git commit -m "feat: add openai compatible embeddings"
```

### 任务 2：确定性经济模式关键词

**接口：**
- 提供：`KeywordExtractor.extract(text: str, limit: int = 15) -> list[str]`。
- 输出顺序按分数排序，分数相同时按规范化词项排序，保证确定性。

- [ ] **步骤 1：编写失败的多语言测试**

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

- [ ] **步骤 2：运行并确认提取器缺失**

运行：`python -m pytest tests/unit/indexing/test_keywords.py -v`

预期：导入时失败。

- [ ] **步骤 3：实现 token 规范化和基于 TF 的评分**

中文使用 jieba；拉丁文本和标识符使用 Unicode 单词正则；拉丁单词转为小写，同时保留原始规范化标识符；过滤明确列出的中英文停用词；根据词频和标识符加分计算分数。分数相同则按字典序决定顺序，使重试产生相同数组。

- [ ] **步骤 4：运行关键词测试**

运行: `python -m pytest tests/unit/indexing/test_keywords.py -v`

预期：中文、英文、标识符、数量限制、空文本和确定性顺序测试均通过。

- [ ] **步骤 5：提交经济模式关键词**

```bash
git add rag_modules/indexing/keywords.py tests/unit/indexing/test_keywords.py
git commit -m "feat: extract economy index keywords"
```

### 任务 3：完整 Milvus 向量存储协议

**接口：**
- 产出: `ensure_collection(collection_name, dimension, metric_type="COSINE")`。
- 产出: `upsert(collection_name, entities) -> int`。
- 产出: `count(collection_name) -> int`。
- 产出: `delete_ids(collection_name, ids)`、`delete_document(collection_name, document_id)`、`drop_collection(collection_name)`。
- Entity 字段：`id`、`embedding`、`dataset_id`、`document_id`、`dataset_index_id`、`parent_id`、`position`。

- [ ] **步骤 1：编写失败的 Milvus 适配器测试**

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

- [ ] **步骤 2：运行并观察协议不匹配**

运行: `python -m pytest tests/unit/vector_stores/test_milvus_store.py -v`

预期：失败，因为当前 provider 仅创建/删除临时 collection。

- [ ] **步骤 3：实现明确的 PyMilvus 2.5 schema**

使用 `MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)`，添加具有明确最大长度的 VARCHAR ID 字段、可空 parent ID、INT64 position 和 FLOAT_VECTOR embedding。使用 `prepare_index_params().add_index(field_name="embedding", index_type="HNSW", metric_type="COSINE", params={"M": 16, "efConstruction": 200})`，随后创建并加载 collection。

接受已存在的 collection 前，检查其 schema，并在维度/metric 不匹配时返回 `VectorSchemaMismatch`。按配置批大小分块执行 upsert 和删除；按计数校验前先调用 flush；采用有界 Milvus 一致性轮询，不要依赖可能过时的即时计数。按可信的 UUID document ID 执行带转义的等值筛选来删除文档。

- [ ] **步骤 4：运行 Milvus 单元测试**

运行: `python -m pytest tests/unit/vector_stores/test_milvus_store.py -v`

预期：创建、已有 collection 校验、upsert、计数、按 ID/文档删除和幂等 drop 测试均通过。

- [ ] **步骤 5：提交 Milvus 适配器**

```bash
git add rag_modules/vector_stores/base.py rag_modules/vector_stores/milvus.py rag_modules/vector_stores/factory.py tests/unit/vector_stores/test_milvus_store.py
git commit -m "feat: add milvus indexing operations"
```

### 任务 4：稳定分段持久化

**接口：**
- 产出: `stable_segment_id(dataset_index_id, document_id, parent_id, position, content_hash) -> str`。
- 产出: `SegmentRepository.stage(document, index, job, segments) -> list[DocumentSegmentRecord]`。
- 产出: `activate_document_segments(...)` and `soft_delete_previous_segments(...)`。

- [ ] **步骤 1：编写失败的 ID 和 repository 测试**

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

- [ ] **步骤 2：运行并验证持久化层缺失**

运行: `python -m pytest tests/unit/indexing/test_segment_persistence.py -v`

预期：导入失败。

- [ ] **步骤 3：实现 UUIDv5 ID 和冲突安全的暂存**

使用固定的应用 namespace UUID 和 `uuid5(namespace, canonical_string).hex`。对规范化内容与规范 JSON 源 metadata 的组合计算 SHA-256。父块使用 `embedding_status="not_required"`；高质量普通/子分段使用 `waiting`；经济模式分段使用 `not_required`。对完全相同的重试 ID 执行 upsert，不创建重复记录；若已有相同 ID 但 content hash 不同，则拒绝。

最终审查补充：在第一次数据库变更前校验每个候选记录；以最多 500 条的显式批次执行冲突安全插入和全 ID 重载，并保留调用方拥有的事务。完全重试仅对未删除、处于 `indexing` 且 embedding 状态与 technique 兼容的行有效：高质量普通/子行可以是 `waiting` 或 `completed`，父行和经济模式行保持 `not_required`。Repository 的 SQLAlchemy 失败必须经过固定、类型化且不含内容的存储错误边界；校验错误和程序员错误不应经过该边界。每个应用和迁移异步引擎都隐藏绑定参数，形成纵深防御。

- [ ] **步骤 4：运行持久化测试**

运行: `python -m pytest tests/unit/indexing/test_segment_persistence.py -v`

预期：重试、父级映射、状态、哈希和旧分段软删除测试均通过。

- [ ] **步骤 5：提交分段持久化**

```bash
git add rag_modules/indexing/models.py rag_modules/indexing/ids.py rag_modules/repositories/segment_repository.py tests/unit/indexing/test_segment_persistence.py
git commit -m "feat: stage deterministic document segments"
```

### 任务 5：单文档索引引擎

**接口：**
- 产出: `DocumentIndexingEngine.run(command, progress) -> IndexDocumentResult`。
- `IndexDocumentCommand` 包含不可变 ID/配置快照、分别校验过的 `embedding_batch_size` 和 `vector_batch_size` 快照；高质量模式下还包含 `collection_name`/预期维度。
- `ProgressReporter` 提供 `update(stage: str, progress: int, processed_segments: int)` 和 `check_cancelled()`。
- 依赖: `VectorTargetResolver.resolve(index_id: str, discovered_dimension: int) -> Awaitable[VectorTarget]`；第 5 阶段提供 PostgreSQL/Milvus coordinator，单元测试使用 fake 实现。

- [ ] **步骤 1：编写失败的高质量和经济模式引擎测试**

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

- [ ] **步骤 2：运行并验证引擎缺失**

运行: `python -m pytest tests/unit/indexing/test_document_engine.py -v`

预期：因引擎缺失而失败。

- [ ] **步骤 3：实现精确的执行顺序**

Implement:

```text
download → parse → split → stage → embed-or-keywords → vector-upsert → validate
```

对于已有索引，命令提供已解析的 collection/dimension。对于构建中的索引，引擎为第一个非空批次生成 embedding，获取 `discovered_dimension`，调用 `VectorTargetResolver.resolve`，然后将该批次和剩余批次写入返回的 collection。在各阶段之间及每个批次之间调用 `progress.check_cancelled()`。对应 vector 批次成功后标记分段 embedding 状态；根据可索引分段数校验 vector 计数。引擎不得激活分段；阶段 5 的最终化负责激活和版本切换。

最终审查补充：一个引擎流式单元为
`min(1024, embedding_batch_size, vector_batch_size)`，因此通常一次 Embedding
请求对应一次提交的 Milvus upsert 和一次 PostgreSQL 状态确认。Embedding 客户端仍可自适应拆分被拒绝的 HTTP 请求，Milvus 适配器也保留防御性分块，但引擎配置批次不得超过任一快照。调用任一外部依赖前，暂存行必须未删除、保持 `indexing`，并具有任务 4 所述的 technique 兼容 embedding 状态。高质量重试跳过已为 `completed` 的行，只处理 `waiting` 行。成功返回时，`IndexDocumentResult.vector_count` 表示该文档准备就绪的可索引行总数（之前已完成的加上新确认的），不是本次调用写入数，也不是 collection 全局基数；阶段 5 负责独立的 collection 对账。

- [ ] **步骤 4：运行引擎回归测试**

运行: `python -m pytest tests/unit/indexing/test_document_engine.py tests/unit/embeddings tests/unit/vector_stores tests/unit/indexing/test_keywords.py -v`

预期：高质量、父子、经济模式、取消、空解析、维度不匹配和部分 vector 失败测试均通过。

- [ ] **步骤 5：提交索引引擎**

```bash
git add rag_modules/indexing/engine.py tests/unit/indexing/test_document_engine.py
git commit -m "feat: index one document into postgres and milvus"
```

### 任务 6： Real Milvus Integration

**接口：**
- 针对 Compose Milvus 2.5.14 验证明确定义的 schema 和幂等 upsert。

- [ ] **步骤 1：编写隔离的集成测试**

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

- [ ] **步骤 2：在禁用集成测试时运行**

运行: `python -m pytest tests/integration/test_milvus_index.py -v`

预期：除非设置 `RUN_INTEGRATION=1`，否则跳过。

- [ ] **步骤 3：添加仅使用唯一 collection 名称的真实 Milvus fixture**

根据 `settings.vector_store` 构建 fixture；清理时仅处理测试创建的名称，绝不列出/删除任意应用 collection。

- [ ] **步骤 4：针对 Compose Milvus 运行测试**

运行:

```bash
docker compose up -d etcd minio standalone
RUN_INTEGRATION=1 python -m pytest tests/integration/test_milvus_index.py -v
```

预期：重复 upsert 后有一个 entity，且没有泄漏的测试 collection。

- [ ] **步骤 5：提交 Milvus 集成覆盖**

```bash
git add tests/conftest.py tests/integration/test_milvus_index.py
git commit -m "test: verify real milvus indexing"
```

## 阶段验证

运行：

```bash
python -m pytest tests/unit/embeddings tests/unit/indexing tests/unit/vector_stores -v
RUN_INTEGRATION=1 python -m pytest tests/integration/test_milvus_index.py -v
git diff --check
```

预期：OpenAI-compatible 和 Milvus 原语均得到独立验证，经济模式不会访问任一服务，且 PostgreSQL vector 值保持未设置。
