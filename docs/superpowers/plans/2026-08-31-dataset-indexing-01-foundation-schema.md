# Dataset Indexing Foundation and Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立完整索引所需的依赖、嵌套配置、Alembic 数据模型、空知识库 API 以及 RabbitMQ/Celery 基础运行环境。

**Architecture:** 保留现有 SQLAlchemy async session 和 `datasets`、`documents`、`document_segments` 表，使用 Alembic 增量增加任务与索引版本。配置仍由 Pydantic Settings 统一装载，所有敏感值只存在服务端。

**Tech Stack:** Python 3.11、FastAPI、Pydantic Settings、SQLAlchemy 2、Alembic、PostgreSQL、Celery 5、RabbitMQ 4、pytest。

**Spec:** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## Global Constraints

- 不创建 `knowledge_bases` 表；知识库 ORM 固定使用 `datasets`。
- 迁移不得删除或重建现有三张业务表。
- `document_segments.vector` 保持现状且本期不写入。
- RabbitMQ 是唯一 Celery broker；不配置 Redis。
- 模型密钥、对象存储密钥和 broker 密码不得出现在选项 API。
- 空知识库创建不得连接 Milvus、MinIO、RabbitMQ 或 Embedding 服务。
- 精确暂存本计划文件，避免提交当前工作区的既有无关改动。

---

## File Structure

- Modify: `pyproject.toml` — 后端运行和测试依赖。
- Create: `.gitignore` — 在任何依赖安装前排除 secret、cache、node_modules、dist、runtime data 和 Compose volumes。
- Create: `tests/conftest.py` — FastAPI client、依赖覆盖和 integration marker 基础 fixture。
- Modify: `rag_modules/config/settings.py` — Embedding、MinIO、上传、预览、Celery 配置模型。
- Modify: `rag_modules/config/config_dev.yaml` — 非敏感开发默认值。
- Create: `alembic.ini` — Alembic CLI 配置。
- Create: `migrations/env.py` — async SQLAlchemy 迁移环境。
- Create: `migrations/script.py.mako` — 迁移模板。
- Create: `migrations/versions/20260831_01_indexing_schema.py` — 增量 schema。
- Modify: `rag_modules/db/models.py` — `DatasetIndexRecord`、`IndexingJobRecord`、`IndexingJobDocumentRecord` 和 segment 扩展字段。
- Create: `rag_modules/api/dto/indexing_options.py` — 前端公开选项 DTO。
- Create: `rag_modules/api/indexing_options_api.py` — `GET /api/indexing/options`。
- Modify: `rag_modules/api/dto/knowledge_base/knowledgeBaseCreate.py` — 空知识库创建请求。
- Modify: `rag_modules/api/dto/knowledge_base/knowledgeBase.py` — 不暴露 vector store 的知识库响应。
- Modify: `rag_modules/services/knowledge_base_service.py` — 移除创建阶段的 Milvus provision。
- Modify: `rag_modules/repositories/knowledge_base_repository.py` — 详情查询和过滤。
- Modify: `rag_modules/api/knowledge_base_api.py` — 创建 201、详情、筛选参数。
- Modify: `main.py` — 注册选项路由并移除运行中的旧 JSON 常量依赖。
- Create: `rag_modules/tasks/celery_app.py` — RabbitMQ Celery app 和队列声明。
- Modify: `docker-compose.yml` — RabbitMQ 服务与持久卷。
- Test: `tests/unit/config/test_settings.py`。
- Test: `tests/unit/db/test_indexing_models.py`。
- Test: `tests/api/test_indexing_options.py`。
- Test: `tests/api/test_dataset_api.py`。
- Test: `tests/unit/tasks/test_celery_config.py`。

### Task 1: Runtime Dependencies and Nested Settings

**Interfaces:**
- Produces: `settings.embedding`, `settings.object_storage`, `settings.broker`, `settings.upload`, `settings.parser`, `settings.preview`, `settings.indexing`。
- Produces: `EmbeddingSettings.get_model(model_id: str) -> EmbeddingModelDefinition`。
- Consumes: 当前 `SettingsConfigDict(env_nested_delimiter="__")`。

- [ ] **Step 1: Write failing settings tests**

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

- [ ] **Step 2: Run tests and confirm missing configuration models**

Run: `python -m pytest tests/unit/config/test_settings.py -v`

Expected: FAIL because `Settings` has no `embedding` field or `EmbeddingSettings.get_model`.

- [ ] **Step 3: Add dependencies and focused settings models**

Add runtime dependencies to `pyproject.toml`:

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

Add test extras:

```toml
[project.optional-dependencies]
test = [
    "pytest>=8.4,<10",
    "pytest-asyncio>=1.1,<2",
    "respx>=0.22,<1",
]
```

Implement these exact settings types in `rag_modules/config/settings.py`:

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

Also add `ObjectStorageSettings`, `BrokerSettings`, `UploadSettings`, `ParserSettings`, `PreviewSettings`, and `IndexingSettings` with the exact defaults approved in the spec. Use `SecretStr` for credentials. Add them as top-level `Settings` fields so the nested names are `OBJECT_STORAGE__...`, `BROKER__...`, and so on.

Before installing dependencies, create `.gitignore` with `.env`, `.idea/`, `__pycache__/`, `.pytest_cache/`, `frontend/node_modules/`, `frontend/dist/`, `data/`, and `volumes/`. Create `tests/conftest.py` with a `TestClient(app)` fixture that resets `app.dependency_overrides` after each test and registers `integration`/`e2e` markers; individual API tests override their service dependencies so unit API tests do not require a live PostgreSQL connection.

- [ ] **Step 4: Run settings tests**

Run: `python -m pytest tests/unit/config/test_settings.py -v`

Expected: PASS; repr output does not reveal the secret.

- [ ] **Step 5: Commit the settings boundary**

```bash
git add .gitignore pyproject.toml rag_modules/config/settings.py rag_modules/config/config_dev.yaml tests/conftest.py tests/unit/config/test_settings.py
git commit -m "feat: add indexing service settings"
```

### Task 2: Alembic Migration and ORM Records

**Interfaces:**
- Produces: `DatasetIndexRecord`, `IndexingJobRecord`, `IndexingJobDocumentRecord`.
- Produces: segment fields `dataset_index_id`, `indexing_job_id`, `source_metadata`, `content_hash`.
- Consumes: `Base.metadata` and async database URI.

- [ ] **Step 1: Write failing ORM contract tests**

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

- [ ] **Step 2: Run the ORM tests and observe import failure**

Run: `python -m pytest tests/unit/db/test_indexing_models.py -v`

Expected: FAIL because the three records do not exist.

- [ ] **Step 3: Add models and an incremental migration**

Implement the fields and status strings from spec sections 6 and 7. Add these document-worker fields to `IndexingJobDocumentRecord`: `available_at`, `heartbeat_at`, `lease_expires_at`, `worker_id`, `celery_task_id`, and `warnings`. Parallel Celery document tasks require their own lease and dispatch state rather than sharing the job-level lease. Use PostgreSQL JSONB variants for new JSON fields while retaining a generic JSON fallback for model-level compatibility.

Create an async Alembic environment with:

```python
# migrations/env.py
from rag_modules.config.settings import settings
from rag_modules.db.base import Base
from rag_modules.db import models  # noqa: F401

config.set_main_option("sqlalchemy.url", settings.sqlalchemy_database_uri)
target_metadata = Base.metadata
```

The migration must perform operations in this order to avoid the `indexing_jobs`/`dataset_indexes` circular reference:

1. Create `indexing_jobs` without the `target_index_id` foreign key.
2. Create `dataset_indexes` with `created_by_job_id` foreign key.
3. Add the named `fk_indexing_jobs_target_index` constraint.
4. Create `indexing_job_documents`.
5. Add four segment columns and named foreign keys.
6. Add partial unique index `uq_dataset_indexes_one_active`.
7. Add active segment and keyword GIN indexes.

Downgrade must reverse only these additions; it must not drop existing business tables.

- [ ] **Step 4: Verify ORM and migration both directions on PostgreSQL**

Run:

```bash
docker compose up -d postgres
python -m alembic upgrade head
python -m pytest tests/unit/db/test_indexing_models.py -v
python -m alembic downgrade base
python -m alembic upgrade head
```

Expected: all commands exit 0; `datasets`, `documents`, and `document_segments` survive downgrade.

- [ ] **Step 5: Commit schema foundation**

```bash
git add alembic.ini migrations rag_modules/db/models.py rag_modules/db/__init__.py tests/unit/db/test_indexing_models.py
git commit -m "feat: add persistent indexing schema"
```

### Task 3: Public Indexing Options

**Interfaces:**
- Produces: `GET /api/indexing/options`.
- Consumes: `settings.embedding.models`, upload/parser/preview defaults.
- Response never includes `base_url` or `api_key`.

- [ ] **Step 1: Write the failing API security contract**

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

- [ ] **Step 2: Run and verify 404**

Run: `python -m pytest tests/api/test_indexing_options.py -v`

Expected: FAIL with HTTP 404.

- [ ] **Step 3: Implement DTO and route**

Create DTOs with public model shape:

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

Build the response only from whitelisted fields and register the router directly on `app` without an extra `/api/v1` prefix.

- [ ] **Step 4: Run API and OpenAPI tests**

Run: `python -m pytest tests/api/test_indexing_options.py tests/test_api_routes.py -v`

Expected: PASS and `/api/indexing/options` appears once in OpenAPI.

- [ ] **Step 5: Commit options API**

```bash
git add rag_modules/api/dto/indexing_options.py rag_modules/api/indexing_options_api.py main.py tests/api/test_indexing_options.py
git commit -m "feat: expose safe indexing options"
```

### Task 4: Empty Dataset Creation and Detail API

**Interfaces:**
- Produces: `KnowledgeBaseCreate(name, description, permission)`.
- Produces: `POST /api/knowledge_base -> 201` and `GET /api/knowledge_base/{dataset_id}`.
- Consumes: `KnowledgeBaseRepository`; must not consume `VectorStoreProvider`.

- [ ] **Step 1: Write failing service/API tests**

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

- [ ] **Step 2: Run and confirm old DTO/collection behavior fails**

Run: `python -m pytest tests/api/test_dataset_api.py -v`

Expected: FAIL because the old request requires vector/retrieval fields or the service provisions Milvus.

- [ ] **Step 3: Implement the minimal empty-dataset contract**

Replace the create DTO with:

```python
class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=2000)
    permission: Literal["only_me", "all_team_members"] = "only_me"
```

Create `DatasetRecord` with `provider="vendor"`, `indexing_technique="high_quality"`, no embedding model, no retrieval configuration, and `partial_user_config={"process_rule": None}`. Remove `_normalize_vector_store`, default dimension maps, and every create-time provider call. Add repository `get_active(dataset_id)` and return 404 for a missing or soft-deleted dataset.

Keep list compatibility with `status`, `visibility`, `q`, `page`, and `page_size`; unknown filter values return 422 rather than being silently ignored.

- [ ] **Step 4: Run dataset regression tests**

Run: `python -m pytest tests/api/test_dataset_api.py tests/test_api_routes.py -v`

Expected: PASS; no Milvus connection is attempted.

- [ ] **Step 5: Commit empty dataset behavior**

```bash
git add rag_modules/api/dto/knowledge_base/knowledgeBaseCreate.py rag_modules/api/dto/knowledge_base/knowledgeBase.py rag_modules/api/knowledge_base_api.py rag_modules/services/knowledge_base_service.py rag_modules/repositories/knowledge_base_repository.py tests/api/test_dataset_api.py tests/test_api_routes.py
git commit -m "feat: create empty datasets before documents"
```

### Task 5: RabbitMQ and Celery Bootstrap

**Interfaces:**
- Produces: `celery_app: Celery` with `indexing` and `maintenance` queues.
- Consumes: `settings.broker.url`.
- Compose produces healthy `rabbitmq` on 5672 and management UI on 15672.

- [ ] **Step 1: Write failing Celery configuration test**

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

- [ ] **Step 2: Run and verify module import failure**

Run: `python -m pytest tests/unit/tasks/test_celery_config.py -v`

Expected: FAIL because `rag_modules.tasks.celery_app` does not exist.

- [ ] **Step 3: Add RabbitMQ Compose service and Celery app**

Create `celery_app` with `kombu.Queue` declarations and JSON-only serialization:

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

Use `rabbitmq:4-management`, an explicit application user/vhost, a named data volume, and the non-deprecated readiness command:

```yaml
healthcheck:
  test: ["CMD", "rabbitmq-diagnostics", "-q", "check_running"]
  interval: 10s
  timeout: 5s
  retries: 10
```

- [ ] **Step 4: Verify configuration and broker readiness**

Run:

```bash
python -m pytest tests/unit/tasks/test_celery_config.py -v
docker compose config
docker compose up -d rabbitmq
docker compose exec rabbitmq rabbitmq-diagnostics -q check_running
```

Expected: all commands pass and Compose contains no Redis service.

- [ ] **Step 5: Commit broker foundation**

```bash
git add docker-compose.yml rag_modules/tasks/__init__.py rag_modules/tasks/celery_app.py tests/unit/tasks/test_celery_config.py
git commit -m "feat: add rabbitmq celery foundation"
```

## Phase Verification

Run:

```bash
python -m pytest tests/unit/config tests/unit/db tests/unit/tasks tests/api tests/test_api_routes.py -v
python -m alembic current
docker compose config
git diff --check
```

Expected: all tests pass, Alembic reports head, Compose has RabbitMQ and no Redis, and empty dataset creation performs no external service calls.
