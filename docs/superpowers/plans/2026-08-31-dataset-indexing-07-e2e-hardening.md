# Dataset 索引端到端加固实施计划

> **面向智能体工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项执行本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 使用真实 PostgreSQL、RabbitMQ、MinIO、Milvus 和 mock OpenAI-compatible 服务验证完整链路，补齐故障恢复、资源清理、安全检查和旧 JSON 清理。

**架构：** E2E fixture 只通过公开 HTTP API 驱动系统，并通过只读数据库/Milvus/MinIO断言验证跨系统不变量。故障测试在唯一 test dataset/index 上运行，所有资源都用唯一前缀并在 finally/fixture teardown 中幂等清理。

**技术栈：** Docker Compose、FastAPI mock service、pytest、PostgreSQL、RabbitMQ、Celery、MinIO、Milvus、React/Vite。

**规格：** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## 全局约束

- E2E 不使用真实付费 Embedding 服务或真实 API Key。
- 测试对象、collection 和 dataset 使用唯一 ID，不触碰用户已有数据。
- PostgreSQL `document_segments.vector` 必须保持 `NULL`。
- RabbitMQ/Celery 测试必须验证重复投递和 Worker 中断恢复，而不仅是正常路径。
- 完整重建失败必须证明旧 active index 未变。
- 资源删除必须验证 PostgreSQL、MinIO 和 Milvus 三方最终一致。
- 不执行广泛目录删除；只清理 fixture 创建的明确对象/collection/记录。
- 最终文档不得包含真实密码、token 或 `.env` 内容。

---

## 文件结构

- 创建：`tests/support/mock_embedding_app.py` — 可配置维度/错误/延迟的 OpenAI-compatible mock。
- 创建: `tests/e2e/conftest.py` — API、数据库、MinIO、Milvus 和轮询 fixture。
- 创建: `tests/e2e/test_high_quality_flow.py`。
- 创建: `tests/e2e/test_parent_child_flow.py`。
- 创建: `tests/e2e/test_economy_flow.py`。
- 创建: `tests/e2e/test_recovery_and_versioning.py`。
- 创建: `tests/e2e/test_deletion_cleanup.py`。
- 修改：`docker-compose.yml` — 仅测试使用的 mock embedding profile/healthcheck。
- 创建: `rag_modules/config/.env.example` — 无密钥模板。
- 修改：`.gitignore` — 验证第 1 阶段创建的 secret、cache、volumes、node_modules 和 runtime data 排除项。
- 修改: `main.py` — 删除旧 JSON、旧内联 Milvus/DTO 注释块和无用 import。
- 如存在则仅删除：`data/knowledge_bases.json` 和 `data/knowledge_base.json`。
- 移除过时文件：没有导入后移除 `rag_modules/api/dto/other/vectorStoreConfig.py`。
- 修改：`README.md` — setup、migration、API、Worker、Beat、测试和故障排查。
- 测试：`tests/unit/test_no_legacy_json_store.py`。
- 测试：`tests/unit/test_secret_safety.py`。

### 任务 1：确定性 Mock OpenAI-compatible 服务

**接口：**
- 提供：带有 OpenAI-compatible `data[index, embedding]` 的 `POST /v1/embeddings`。
- 控制 header 仅在测试中可用：维度、强制状态、延迟和反向响应顺序。

- [ ] **步骤 1：编写失败的 mock 服务契约测试**

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

- [ ] **步骤 2：运行并观察 mock app 缺失**

运行: `python -m pytest tests/support/test_mock_embedding_app.py -v`

预期：因模块缺失而失败。

- [ ] **步骤 3：实现确定性有限向量和故障控制**

根据归一化到 `[-1, 1]` 的 SHA-256 字节生成每个向量；绝不使用 Python 随机化的 `hash()`。模型名 `mock-N` 选择维度 N，默认维度为 8。要求任意 Bearer token，但绝不记录 token。测试专用环境变量启用强制 429/401/延迟/反向顺序场景；生产 Compose profile 不暴露此服务。

- [ ] **步骤 4：运行 mock 测试和健康检查**

运行:

```bash
python -m pytest tests/support/test_mock_embedding_app.py -v
docker compose --profile test config
```

预期：通过；mock 服务只存在于测试 profile，并具有 `/health` 检查。

- [ ] **步骤 5：提交 mock 服务**

```bash
git add tests/support/mock_embedding_app.py tests/support/test_mock_embedding_app.py docker-compose.yml
git commit -m "test: add deterministic embedding service"
```

### 任务 2：高质量普通模式与父子模式 E2E

**接口：**
- 仅驱动公开的 Dataset/Document/Preview/Job API。
- 随后读取 PostgreSQL/Milvus 以验证 ID 和计数。

- [ ] **步骤 1：编写失败的高质量 E2E 测试**

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

- [ ] **步骤 2：启动服务栈并运行测试以暴露集成缺口**

运行:

```bash
docker compose --profile app --profile test up -d --build
RUN_E2E=1 python -m pytest tests/e2e/test_high_quality_flow.py tests/e2e/test_parent_child_flow.py -v
```

预期：初始失败应指出第一个损坏的公开契约或跨系统不变量；不得为了通过而弱化断言。

- [ ] **步骤 3：实现 E2E fixture，并仅修复发现的集成边界**

Fixture 创建唯一 Dataset 名称和对象/collection 前缀，使用有界超时轮询，从 PostgreSQL 查询分段 ID，从目标 `dataset_indexes.collection_name` 查询 entity ID/计数，并在 `yield` teardown 中注册清理。添加全部七种不含敏感内容的小型格式 fixture。

- [ ] **步骤 4：重新运行高质量 E2E**

运行: `RUN_E2E=1 python -m pytest tests/e2e/test_high_quality_flow.py tests/e2e/test_parent_child_flow.py -v`

预期：通过；普通分段或子分段各自恰好对应一个 Milvus entity，且 PostgreSQL vector 为 null。

- [ ] **步骤 5：提交高质量 E2E**

```bash
git add tests/e2e/conftest.py tests/e2e/test_high_quality_flow.py tests/e2e/test_parent_child_flow.py tests/fixtures/documents
git commit -m "test: verify complete high quality indexing"
```

### 任务 3：经济模式索引 E2E

**接口：**
- 验证仅关键词索引，以及 Embedding/Milvus 交互次数为零。

- [ ] **步骤 1：编写失败的经济模式 E2E**

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

- [ ] **步骤 2：运行并确认行为**

运行: `RUN_E2E=1 python -m pytest tests/e2e/test_economy_flow.py -v`

预期：在仅关键词的端到端状态和 API 细节正确接通前失败。

- [ ] **步骤 3：修复经济模式专属集成边界**

确保任务快照省略 embedding model/provider，目标索引没有 collection/dimension，进度跳过 embedding/indexing 阶段，每个已完成分段都有关键词，且经济模式任务不解析 vector provider。

- [ ] **步骤 4：重新运行经济模式 E2E**

运行: `RUN_E2E=1 python -m pytest tests/e2e/test_economy_flow.py -v`

预期：通过，且没有新增 mock embedding 请求，也没有 Milvus collection。

- [ ] **步骤 5：提交经济模式 E2E**

```bash
git add tests/e2e/test_economy_flow.py
git commit -m "test: verify economy keyword indexing"
```

### 任务 4：重复投递、Worker 丢失与重建失败

**接口：**
- 验证 `acks_late`、租约恢复、稳定 ID 和活动版本保护。

- [ ] **步骤 1：编写失败的韧性场景**

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

- [ ] **步骤 2：运行韧性测试**

运行: `RUN_E2E=1 python -m pytest tests/e2e/test_recovery_and_versioning.py -v`

预期：任何重复、租约或版本切换不变量不完整时均失败。

- [ ] **步骤 3：添加受控的 Worker 丢失场景**

使用仅测试的 parser/embedding 延迟，等待 job-document 行处于带租约的 running 状态，仅停止 Compose `celery-worker` 服务，重启后等待租约恢复，并断言同一任务最终完成且没有重复分段/vector ID。即使断言失败，fixture 也必须在 `finally` 中重启 Worker。

- [ ] **步骤 4：运行全部韧性场景**

运行: `RUN_E2E=1 python -m pytest tests/e2e/test_recovery_and_versioning.py -v`

预期：重复分发、Worker 丢失/重启以及重建失败保留旧活动版本的测试均通过。

- [ ] **步骤 5：提交韧性覆盖与修复**

```bash
git add tests/e2e/test_recovery_and_versioning.py tests/e2e/conftest.py
git commit -m "test: verify indexing recovery and version safety"
```

### 任务 5：删除与补偿 E2E

**接口：**
- 验证文档和 Dataset 删除最终清除外部资源，同时 API 及时返回。

- [ ] **步骤 1：编写失败的清理 E2E**

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

- [ ] **步骤 2：运行并识别补偿缺口**

运行: `RUN_E2E=1 python -m pytest tests/e2e/test_deletion_cleanup.py -v`

预期：在维护扫描和清理标记完成前失败。

- [ ] **步骤 3：仅修复确切清理缺口并验证幂等性**

确保清理涵盖 active、retired、failed 和 building 版本；仅在成功后标记对象清理状态；将资源缺失视为成功；并可调用两次。通过创建没有消息的已删除行并等待 Beat 发现它，添加发布失败场景。

- [ ] **步骤 4：重新运行清理 E2E 两次**

运行:

```bash
RUN_E2E=1 python -m pytest tests/e2e/test_deletion_cleanup.py -v
RUN_E2E=1 python -m pytest tests/e2e/test_deletion_cleanup.py -v
```

预期：两次运行都通过，且没有泄漏测试资源。

- [ ] **步骤 5：提交删除 E2E**

```bash
git add tests/e2e/test_deletion_cleanup.py
git commit -m "test: verify dataset resource cleanup"
```

### 任务 6：移除旧 JSON/Milvus UI 契约并保护密钥

**接口：**
- 不再保留活动的 `knowledge_bases.json` 引用或过时的 vector-store DTO 导入。
- 提供只包含名称/占位值的 `.env.example`。

- [ ] **步骤 1：编写失败的旧实现/安全测试**

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

- [ ] **步骤 2：运行并观察旧 `main.py` 引用导致的失败**

运行: `python -m pytest tests/unit/test_no_legacy_json_store.py tests/unit/test_secret_safety.py -v`

预期：失败，因为当前 `main.py` 定义了 `DATA_FILE`，并包含旧存储函数/注释。

- [ ] **步骤 3：删除确切的旧代码/文件并添加安全模板**

将 `main.py` 精简为应用构建、中间件/静态前端和路由注册。移除未使用的 `json`、regex/threading/vector DTO 导入以及整段注释掉的 JSON/Milvus 实现。若 `data/knowledge_bases.json` 和 `data/knowledge_base.json` 已跟踪/存在，则仅删除这两个文件；不要删除 `data/graph_rag.db` 或整个 `data/`。

在使用 `rg` 证明没有导入后，删除过时的 `vectorStoreConfig.py`。向 `.gitignore` 添加 `.env`、`__pycache__`、`.pytest_cache`、`.idea`、`frontend/node_modules`、runtime `data` 和 Compose volumes 条目，不删除当前用户数据。添加 `.env.example`，使用占位值并至少包含一个非密钥的 mock model 定义。

- [ ] **步骤 4：运行旧实现/安全与路由回归测试**

运行:

```bash
python -m pytest tests/unit/test_no_legacy_json_store.py tests/unit/test_secret_safety.py tests/test_api_routes.py -v
rg -n "knowledge_bases\.json|knowledge_base\.json|VectorStoreConfig|MILVUS.*(host|port|token)" main.py rag_modules frontend/src
```

预期：测试通过；`rg` 找不到 JSON store 或前端 Milvus 配置。经人工审查后，可保留仅供后端使用的 Milvus 设置。

- [ ] **步骤 5：提交旧实现清理**

```bash
git add main.py .gitignore rag_modules/config/.env.example tests/unit/test_no_legacy_json_store.py tests/unit/test_secret_safety.py rag_modules/api/dto/other/__init__.py
git add -u rag_modules/api/dto/other/vectorStoreConfig.py
git commit -m "refactor: remove legacy knowledge base json flow"
```

### 任务 7：运维文档与最终验证

**接口：**
- 提供 API、迁移、Worker、Beat、测试服务栈和故障诊断的准确开发者命令。

- [ ] **步骤 1：添加文档验证清单测试**

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

- [ ] **步骤 2：运行并确认文档缺口**

运行: `python -m pytest tests/unit/test_readme_commands.py -v`

预期：在 README 包含所有必需运维流程前失败。

- [ ] **步骤 3：记录启动、配置、状态和恢复流程**

更新 README，包含：

1. 使用占位值的环境变量表。
2. `docker compose` 基础设施和 app-profile 命令。
3. Alembic upgrade/downgrade/current 命令。
4. 独立运行 API、Worker 和 Beat 的命令。
5. 支持的文件格式和不支持 OCR 的限制。
6. 高质量/经济模式及父子模式约束。
7. 如何在不暴露凭据的情况下检查 PostgreSQL 任务状态、RabbitMQ 队列、MinIO 对象和 Milvus collection。
8. 重试/取消/过期任务恢复行为。
9. 后端/前端/集成/E2E 测试命令。

- [ ] **步骤 4：运行完整验证矩阵**

运行:

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

预期：每条命令的退出码均为 0；Alembic 处于 head；Compose 包含 RabbitMQ 且不含 Redis；全部 30 项规格验收结果都有通过的断言覆盖。

- [ ] **步骤 5：提交运维文档**

```bash
git add README.md tests/unit/test_readme_commands.py
git commit -m "docs: document complete dataset indexing operations"
```

## 最终完成流程

在宣告功能完成前：

1. 执行 `superpowers:verification-before-completion`。
2. 重新运行完整验证矩阵，不依赖旧的输出结果。
3. 记录准确的通过测试数量和构建结果。
4. 检查 `git status --short`，区分既有用户改动与实现提交。
5. 使用 `superpowers:requesting-code-review` 对规格和实现进行最终审查。
6. 只有审查通过后，才使用 `superpowers:finishing-a-development-branch` 提供合并/集成选项。

## 官方实现参考

- Celery 延迟确认和幂等性：<https://docs.celeryq.dev/en/stable/userguide/tasks.html>
- Celery 预取行为：<https://docs.celeryq.dev/en/latest/userguide/optimizing.html>
- Alembic asyncio 迁移模式：<https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic>
- MinIO Python 客户端 API：<https://docs.min.io/aistor/developers/sdk/python/api/>
- PyMilvus schema API：<https://milvus.io/api-reference/pymilvus/v2.5.x/MilvusClient/Collections/create_schema.md>
- RabbitMQ 可靠性和持久化消息：<https://www.rabbitmq.com/docs/reliability>
