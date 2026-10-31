# Dataset 索引基础与 Schema 实施计划

> **面向智能体工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项执行本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 建立完整索引所需的依赖、嵌套配置、Alembic 数据模型、空知识库 API 以及 RabbitMQ/Celery 基础运行环境。

**架构：** 保留现有 SQLAlchemy async session 和 `datasets`、`documents`、`document_segments` 表，使用 Alembic 增量增加任务与索引版本。配置仍由 Pydantic Settings 统一装载，所有敏感值只存在服务端。

**技术栈：** Python 3.11、FastAPI、Pydantic Settings、SQLAlchemy 2、Alembic、PostgreSQL、Celery 5、RabbitMQ 4、pytest。

**规格：** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## 全局约束

- 不创建 `knowledge_bases` 表；知识库 ORM 固定使用 `datasets`。
- 迁移不得删除或重建现有三张业务表。
- `document_segments.vector` 保持现状且本期不写入。
- RabbitMQ 是唯一 Celery broker；不配置 Redis。
- 模型密钥、对象存储密钥和 broker 密码不得出现在选项 API。
- 空知识库创建不得连接 Milvus、MinIO、RabbitMQ 或 Embedding 服务。
- 仅暂存本计划所列文件，避免提交当前工作区已有的无关改动。

---

## 文件结构

- 修改：`pyproject.toml` — 后端运行和测试依赖。
- 创建：`.gitignore` — 在任何依赖安装前排除 secret、cache、node_modules、dist、runtime data 和 Compose volumes。
- 创建：`tests/conftest.py` — FastAPI client、依赖覆盖和 integration marker 基础 fixture。
- 修改：`rag_modules/config/settings.py` — Embedding、MinIO、上传、预览、Celery 配置模型。
- 修改：`rag_modules/config/config_dev.yaml` — 非敏感开发默认值。
- 创建：`alembic.ini` — Alembic CLI 配置。
- 创建：`migrations/env.py` — async SQLAlchemy 迁移环境。
- 创建：`migrations/script.py.mako` — 迁移模板。
- 创建：`migrations/versions/20260831_01_indexing_schema.py` — 增量 schema。
- 修改：`rag_modules/db/models.py` — `DatasetIndexRecord`、`IndexingJobRecord`、`IndexingJobDocumentRecord` 和 segment 扩展字段。
- 创建：`rag_modules/api/dto/indexing_options.py` — 前端公开选项 DTO。
- 创建：`rag_modules/api/indexing_options_api.py` — `GET /api/indexing/options`。
- 修改：`rag_modules/api/dto/knowledge_base/knowledgeBaseCreate.py` — 空知识库创建请求。
- 修改：`rag_modules/api/dto/knowledge_base/knowledgeBase.py` — 不暴露 vector store 的知识库响应。
- 修改：`rag_modules/services/knowledge_base_service.py` — 移除创建阶段的 Milvus provision。
- 修改：`rag_modules/repositories/knowledge_base_repository.py` — 详情查询和过滤。
- 修改：`rag_modules/api/knowledge_base_api.py` — 创建 201、详情、筛选参数。
- 修改：`main.py` — 注册选项路由并移除运行中的旧 JSON 常量依赖。
- 创建：`rag_modules/tasks/celery_app.py` — RabbitMQ Celery app 和队列声明。
- 修改：`docker-compose.yml` — RabbitMQ 服务与持久卷。
- 测试：`tests/unit/config/test_settings.py`。
- 测试：`tests/unit/db/test_indexing_models.py`。
- 测试：`tests/api/test_indexing_options.py`。
- 测试：`tests/api/test_dataset_api.py`。
- 测试：`tests/unit/tasks/test_celery_config.py`。

### 任务 1：运行时依赖与嵌套设置

**接口：**
- 产出: `settings.embedding`, `settings.object_storage`, `settings.broker`, `settings.upload`, `settings.parser`, `settings.preview`, `settings.indexing`。
- 产出: `EmbeddingSettings.get_model(model_id: str) -> EmbeddingModelDefinition`。
- 依赖: 当前 `SettingsConfigDict(env_nested_delimiter="__")`。

- [ ] **步骤 1：编写失败的设置测试**

```python
# tests/unit/config/test_settings.py
from rag_modules.config.settings import Settings


def test_nested_embedding_catalog_and_secrets_are_loaded(monkeypatch):
    monkeypatch.setenv("EMBEDDING__BASE_URL", "http://embed:8000/v1")
    monkeypatch.setenv("EMBEDDING__API_KEY", "secret-value")
    monkeypatch.setenv("EMBEDDING__DEFAULT_MODEL", "bge-m3")
    monkeypatch.setenv(
        "EMBEDDING__MODELS",
        '[{"id":"bge-m3","model":"BAAI/bge-m3","display_name":"BGE-M3"}]',
    )

    loaded = Settings(_env_file=None)

    assert loaded.embedding.get_model("bge-m3").model == "BAAI/bge-m3"
    assert loaded.embedding.api_key.get_secret_value() == "secret-value"
    assert "secret-value" not in repr(loaded.embedding)


def test_default_embedding_model_must_be_enabled(monkeypatch):
    monkeypatch.setenv("EMBEDDING__DEFAULT_MODEL", "missing")
    monkeypatch.setenv("EMBEDDING__MODELS", "[]")

    with pytest.raises(ValueError, match="default embedding model"):
        Settings(_env_file=None)
```

- [ ] **步骤 2：运行测试并确认配置模型缺失**

运行：`python -m pytest tests/unit/config/test_settings.py -v`

预期：失败，因为 `Settings` 没有 `embedding` 字段或 `EmbeddingSettings.get_model`。

- [ ] **步骤 3：添加依赖与专用设置模型**

将运行时依赖添加到 `pyproject.toml`：

```toml
"alembic>=1.16,<2",
"celery>=5.5,<6",
"httpx>=0.28,<1",
"minio>=7.2,<8",
"python-multipart>=0.0.20,<1",
"charset-normalizer>=3.4,<4",
"pypdf>=6,<7",
"python-docx>=1.2,<2",
"openpyxl>=3.1,<4",
"xlrd>=2,<3",
"markdown-it-py>=4,<5",
"jieba>=0.42,<1",
```

添加测试额外依赖：

```toml
[project.optional-dependencies]
test = [
    "pytest>=8.4,<10",
    "pytest-asyncio>=1.1,<2",
    "respx>=0.22,<1",
]
```

在 `rag_modules/config/settings.py` 中实现以下确切的设置类型：

```python
class EmbeddingModelDefinition(BaseModel):
    id: str
    model: str
    display_name: str
    enabled: bool = True
    batch_size: int = Field(default=32, ge=1, le=512)
    max_input_characters: int = Field(default=8000, ge=100)
    request_timeout: int = Field(default=60, ge=1)
    dimensions: int | None = Field(default=None, ge=1, le=32768)


class EmbeddingSettings(BaseModel):
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = "http://localhost:8001/v1"
    api_key: SecretStr = SecretStr("")
    default_model: str = "bge-m3"
    models: list[EmbeddingModelDefinition] = Field(default_factory=lambda: [
        EmbeddingModelDefinition(id="bge-m3", model="bge-m3", display_name="BGE-M3")
    ])
    max_retries: int = Field(default=3, ge=0, le=10)

    @model_validator(mode="after")
    def validate_default_model(self):
        if not any(item.id == self.default_model and item.enabled for item in self.models):
            raise ValueError("default embedding model must exist and be enabled")
        return self

    def get_model(self, model_id: str) -> EmbeddingModelDefinition:
        for item in self.models:
            if item.id == model_id and item.enabled:
                return item
        raise ValueError(f"unknown or disabled embedding model: {model_id}")
```

另外添加规格中批准的确切默认值对应的 `ObjectStorageSettings`、`BrokerSettings`、`UploadSettings`、`ParserSettings`、`PreviewSettings` 和 `IndexingSettings`。凭据使用 `SecretStr`。将它们添加为顶层 `Settings` 字段，使嵌套名称采用 `OBJECT_STORAGE__...`、`BROKER__...` 等形式。

安装依赖前，创建 `.gitignore`，排除 `.env`、`.idea/`、`__pycache__/`、`.pytest_cache/`、`frontend/node_modules/`、`frontend/dist/`、`data/` 和 `volumes/`。创建 `tests/conftest.py`，提供每次测试后重置 `app.dependency_overrides` 的 `TestClient(app)` fixture，并注册 `integration`/`e2e` 标记；单个 API 测试覆盖其服务依赖，使单元 API 测试不需要活动的 PostgreSQL 连接。

- [ ] **步骤 4：运行设置测试**

运行: `python -m pytest tests/unit/config/test_settings.py -v`

预期：通过；`repr` 输出不会泄露密钥。

- [ ] **步骤 5：提交设置边界**

```bash
git add .gitignore pyproject.toml rag_modules/config/settings.py rag_modules/config/config_dev.yaml tests/conftest.py tests/unit/config/test_settings.py
git commit -m "feat: add indexing service settings"
```

### 任务 2：Alembic 迁移与 ORM 记录

**接口：**
- 产出: `DatasetIndexRecord`, `IndexingJobRecord`, `IndexingJobDocumentRecord`.
- 产出: segment fields `dataset_index_id`, `indexing_job_id`, `source_metadata`, `content_hash`.
- 依赖: `Base.metadata` and async database URI.

- [ ] **步骤 1：编写失败的 ORM 契约测试**

```python
# tests/unit/db/test_indexing_models.py
from rag_modules.db.models import (
    DatasetIndexRecord,
    DocumentSegmentRecord,
    IndexingJobDocumentRecord,
    IndexingJobRecord,
)


def test_indexing_models_map_to_approved_tables():
    assert DatasetIndexRecord.__tablename__ == "dataset_indexes"
    assert IndexingJobRecord.__tablename__ == "indexing_jobs"
    assert IndexingJobDocumentRecord.__tablename__ == "indexing_job_documents"
    assert "dataset_index_id" in DocumentSegmentRecord.__table__.c
    assert "source_metadata" in DocumentSegmentRecord.__table__.c


def test_job_document_identity_is_unique():
    names = {constraint.name for constraint in IndexingJobDocumentRecord.__table__.constraints}
    assert "uq_indexing_job_document" in names
```

- [ ] **步骤 2：运行 ORM 测试并观察导入失败**

运行: `python -m pytest tests/unit/db/test_indexing_models.py -v`

预期：失败，因为三个记录尚不存在。

- [ ] **步骤 3：添加模型与增量迁移**

实现规格第 6、7 节中的字段和状态字符串。向 `IndexingJobDocumentRecord` 添加以下文档 Worker 字段：`available_at`、`heartbeat_at`、`lease_expires_at`、`worker_id`、`celery_task_id` 和 `warnings`。并行 Celery 文档任务需要独立的租约和分发状态，不能共享任务级租约。新 JSON 字段使用 PostgreSQL JSONB 变体，同时保留通用 JSON 回退以兼容模型层。

使用以下内容创建异步 Alembic 环境：

```python
# migrations/env.py
from rag_modules.config.settings import settings
from rag_modules.db.base import Base
from rag_modules.db import models  # noqa: F401

config.set_main_option("sqlalchemy.url", settings.sqlalchemy_database_uri)
target_metadata = Base.metadata
```

迁移必须按以下顺序执行操作，以避免 `indexing_jobs`/`dataset_indexes` 循环引用：

1. 创建不带 `target_index_id` 外键的 `indexing_jobs`。
2. 创建带 `created_by_job_id` 外键的 `dataset_indexes`。
3. 添加命名约束 `fk_indexing_jobs_target_index`。
4. 创建 `indexing_job_documents`。
5. 添加四个分段列和命名外键。
6. 添加部分唯一索引 `uq_dataset_indexes_one_active`。
7. 添加活动分段和关键词 GIN 索引。

降级只能撤销上述新增内容；不得删除现有业务表。

- [ ] **步骤 4：在 PostgreSQL 上验证 ORM 与迁移的正反向流程**

运行:

```bash
docker compose up -d postgres
python -m alembic upgrade head
python -m pytest tests/unit/db/test_indexing_models.py -v
python -m alembic downgrade base
python -m alembic upgrade head
```

预期：所有命令退出码为 0；降级后 `datasets`、`documents` 和 `document_segments` 仍然存在。

- [ ] **步骤 5：提交 Schema 基础**

```bash
git add alembic.ini migrations rag_modules/db/models.py rag_modules/db/__init__.py tests/unit/db/test_indexing_models.py
git commit -m "feat: add persistent indexing schema"
```

### 任务 3：公开索引选项

**接口：**
- 产出: `GET /api/indexing/options`.
- 依赖: `settings.embedding.models`, upload/parser/preview defaults.
- 响应绝不包含 `base_url` 或 `api_key`。

- [ ] **步骤 1：编写失败的 API 安全契约**

```python
# tests/api/test_indexing_options.py
def test_indexing_options_expose_enabled_models_without_secrets(client):
    response = client.get("/api/indexing/options")

    assert response.status_code == 200
    payload = response.json()
    assert payload["embedding_models"][0]["provider"] == "openai_compatible"
    assert payload["defaults"]["general"]["max_chunk_length"] == 1024
    serialized = response.text.lower()
    assert "api_key" not in serialized
    assert "base_url" not in serialized
    assert "milvus" not in serialized
```

- [ ] **步骤 2：运行并验证 404**

运行: `python -m pytest tests/api/test_indexing_options.py -v`

预期：以 HTTP 404 失败。

- [ ] **步骤 3：实现 DTO 和路由**

按公开模型形状创建 DTO：

```python
class PublicEmbeddingModel(BaseModel):
    id: str
    name: str
    provider: Literal["openai_compatible"]
    is_default: bool


class IndexingOptionsResponse(BaseModel):
    indexing_techniques: list[IndexingTechniqueOption]
    embedding_models: list[PublicEmbeddingModel]
    segmentation_modes: list[SegmentationModeOption]
    supported_files: list[str]
    defaults: IndexingDefaults
    limits: PublicIndexingLimits
```

响应只能由白名单字段构建，并将路由器直接注册到 `app`，不要额外添加 `/api/v1` 前缀。

- [ ] **步骤 4：运行 API 和 OpenAPI 测试**

运行: `python -m pytest tests/api/test_indexing_options.py tests/test_api_routes.py -v`

预期：通过，且 `/api/indexing/options` 在 OpenAPI 中只出现一次。

- [ ] **步骤 5：提交选项 API**

```bash
git add rag_modules/api/dto/indexing_options.py rag_modules/api/indexing_options_api.py main.py tests/api/test_indexing_options.py
git commit -m "feat: expose safe indexing options"
```

### 任务 4：空 Dataset 创建与详情 API

**接口：**
- 产出: `KnowledgeBaseCreate(name, description, permission)`.
- 产出: `POST /api/knowledge_base -> 201` and `GET /api/knowledge_base/{dataset_id}`.
- 依赖: `KnowledgeBaseRepository`; must not consume `VectorStoreProvider`.

- [ ] **步骤 1：编写失败的服务/API 测试**

```python
# tests/api/test_dataset_api.py
def test_create_empty_dataset_does_not_accept_vector_configuration(client):
    response = client.post(
        "/api/knowledge_base",
        json={"name": "产品知识库", "description": "说明", "permission": "only_me"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["name"] == "产品知识库"
    assert payload["indexing_status"] == "not_started"
    assert "vector_store" not in payload
    assert "embedding_model" not in payload or payload["embedding_model"] is None


@pytest.mark.asyncio
async def test_create_service_never_provisions_milvus(repository, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Milvus must not be called while creating an empty dataset")

    monkeypatch.setattr("rag_modules.vector_stores.factory.get_vector_store", forbidden)
    created = await KnowledgeBaseService(repository).create_knowledge_base(
        KnowledgeBaseCreate(name="产品知识库", permission="only_me")
    )
    assert created.indexing_status == "not_started"
```

- [ ] **步骤 2：运行并确认旧 DTO/collection 行为失败**

运行: `python -m pytest tests/api/test_dataset_api.py -v`

预期：失败，因为旧请求要求 vector/retrieval 字段，或服务会创建 Milvus 资源。

- [ ] **步骤 3：实现最小空 Dataset 契约**

将创建 DTO 替换为：

```python
class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    permission: Literal["only_me", "all_team_members"] = "only_me"
```

创建 `DatasetRecord` 时设置 `provider="vendor"`、`indexing_technique="high_quality"`，不设置 embedding model 和检索配置，并将 `partial_user_config={"process_rule": None}`。删除 `_normalize_vector_store`、默认维度映射以及所有创建时的 provider 调用。增加 repository 方法 `get_active(dataset_id)`；数据集不存在或已软删除时返回 404。

保持列表接口对 `status`、`visibility`、`q`、`page` 和 `page_size` 的兼容；未知过滤值应返回 422，不得静默忽略。

- [ ] **步骤 4：运行 Dataset 回归测试**

运行: `python -m pytest tests/api/test_dataset_api.py tests/test_api_routes.py -v`

预期：通过；不会尝试连接 Milvus。

- [ ] **步骤 5：提交空 Dataset 行为**

```bash
git add rag_modules/api/dto/knowledge_base/knowledgeBaseCreate.py rag_modules/api/dto/knowledge_base/knowledgeBase.py rag_modules/api/knowledge_base_api.py rag_modules/services/knowledge_base_service.py rag_modules/repositories/knowledge_base_repository.py tests/api/test_dataset_api.py tests/test_api_routes.py
git commit -m "feat: create empty datasets before documents"
```

### 任务 5：RabbitMQ 与 Celery 启动

**接口：**
- 产出: `celery_app: Celery`，包含 `indexing` 和 `maintenance` 队列。
- 依赖: `settings.broker.url`.
- Compose 应确保 `rabbitmq` 在 5672 端口健康运行，管理界面使用 15672 端口。

- [ ] **步骤 1：编写失败的 Celery 配置测试**

```python
# tests/unit/tasks/test_celery_config.py
from rag_modules.tasks.celery_app import celery_app


def test_celery_uses_rabbitmq_with_late_ack_and_no_result_backend():
    assert celery_app.conf.broker_url.startswith("amqp://")
    assert celery_app.conf.result_backend is None
    assert celery_app.conf.task_ignore_result is True
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert {queue.name for queue in celery_app.conf.task_queues} == {"indexing", "maintenance"}
```

- [ ] **步骤 2：运行并验证模块导入失败**

运行: `python -m pytest tests/unit/tasks/test_celery_config.py -v`

预期：失败，因为 `rag_modules.tasks.celery_app` 不存在。

- [ ] **步骤 3：添加 RabbitMQ Compose 服务与 Celery app**

使用 `kombu.Queue` 声明和仅 JSON 序列化创建 `celery_app`：

```python
celery_app = Celery("graph_rag", broker=settings.broker.url)
celery_app.conf.update(
    result_backend=None,
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_delivery_mode=2,
    task_serializer="json",
    accept_content=["json"],
    task_queues=(Queue("indexing", durable=True), Queue("maintenance", durable=True)),
)
```

使用 `rabbitmq:4-management`、明确的应用用户/vhost、命名数据卷以及未弃用的就绪检查命令：

```yaml
healthcheck:
  test: ["CMD", "rabbitmq-diagnostics", "-q", "check_running"]
  interval: 10s
  timeout: 5s
  retries: 10
```

- [ ] **步骤 4：验证配置与 broker 就绪状态**

运行:

```bash
python -m pytest tests/unit/tasks/test_celery_config.py -v
docker compose config
docker compose up -d rabbitmq
docker compose exec rabbitmq rabbitmq-diagnostics -q check_running
```

预期：所有命令通过，且 Compose 不包含 Redis 服务。

- [ ] **步骤 5：提交 broker 基础**

```bash
git add docker-compose.yml rag_modules/tasks/__init__.py rag_modules/tasks/celery_app.py tests/unit/tasks/test_celery_config.py
git commit -m "feat: add rabbitmq celery foundation"
```

## 阶段验证

运行:

```bash
python -m pytest tests/unit/config tests/unit/db tests/unit/tasks tests/api tests/test_api_routes.py -v
python -m alembic current
docker compose config
git diff --check
```

预期：所有测试通过，Alembic 报告 head，Compose 含 RabbitMQ 且不含 Redis，并且空 Dataset 创建不会调用外部服务。
