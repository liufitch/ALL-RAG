# Source Sync Connector 与 Outbox 实施计划

> **面向代理式执行者：** 必须使用子技能：建议使用 superpowers:subagent-driven-development 或 superpowers:executing-plans，按任务逐项实施本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 建立 connector/source/event/outbox 持久模型、统一 adapter 协议、管理 API 和可靠 RabbitMQ 事件投递。

**架构：** Adapter 只读取源并生成 UPSERT/DELETE；服务在同一 PostgreSQL 事务写 source event 和 outbox；dispatcher 至少一次投递 `source-events` 队列，consumer 使用事件唯一键和 revision 服务幂等处理。

**技术栈：** Python 3.11、FastAPI、SQLAlchemy async、Alembic、PostgreSQL、Celery、RabbitMQ、pytest。

**规格：** `docs/superpowers/specs/2026-08-31-source-change-sync-design.md`

## 全局约束

- 依赖 Revision Safety 计划；不绕过 `DocumentRevisionService` 写 revision。
- API 和消息不含 connector secret、正文或向量。
- Connector 只产生 UPSERT/DELETE；provider event ID 优先作为幂等键。
- PostgreSQL 提交与消息投递不得使用无补偿双写。

---

## 文件结构

- 修改：`rag_modules/db/models.py`。
- 创建：`migrations/versions/20260831_03_source_connectors.py`。
- 创建：`rag_modules/connectors/base.py` — `SourceConnector` 协议和事件类型。
- 创建：`rag_modules/repositories/source_repository.py`。
- 创建：`rag_modules/services/source_event_service.py`。
- 创建：`rag_modules/tasks/source_event_tasks.py`。
- 创建：`rag_modules/api/source_connector_api.py`、`rag_modules/api/dto/source_connector.py`。
- 修改：`main.py`、`rag_modules/tasks/celery_app.py`。
- 测试：`tests/unit/connectors/test_contract.py`、`tests/unit/services/test_source_event_service.py`、`tests/api/test_source_connectors.py`、`tests/integration/source_sync/test_outbox_dispatch.py`。

### 任务 1：Connector、Source Event 与 Outbox Schema

**接口：** 产出 `SourceConnectorRecord`、`SourceDocumentRecord`、`SourceEventRecord`、`OutboxRecord`；source 唯一性为 `(connector_id, source_document_key)`，event 非空时唯一性为 `(connector_id, source_event_id)`。

- [ ] 编写失败的模型测试，断言表、具名唯一约束、cursor/checkpoint、`missing_since`、event 状态以及 outbox 的 `available_at/attempts`。
- [ ] 运行 `python -m pytest tests/unit/db/test_source_models.py -v`；预期导入/列相关断言失败。
- [ ] 实现 ORM 和增量迁移。仅存储 `secret_ref`；非机密 connector 配置使用 JSONB。为待处理 outbox 和 connector event 历史创建索引。

```python
class SourceEventOperation(StrEnum):
    UPSERT = "upsert"
    DELETE = "delete"

class SourceEventStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    PROCESSING = "processing"
    COMPLETED = "completed"
    IGNORED = "ignored"
    FAILED = "failed"
```

- [ ] 运行模型测试和真实的 Alembic upgrade/downgrade/upgrade；预期 PASS。
- [ ] 使用 `git commit -m "feat: add source synchronization schema"` 提交精确的 schema/model/test 路径。

### 任务 2：Adapter 契约、Event 规范化与 Connector API

**接口：**
- 产出 `SourceChange(operation, source_document_key, source_version, source_event_id, source_etag, occurred_at)`。
- 产出 `SourceConnector.poll_changes(cursor)`、`list_snapshot(checkpoint)`、`fetch_metadata(key)`、`open_content(key, version)`。
- 产出 connector CRUD、`POST .../{connector_id}/sync` 和 event 列表 API。

- [ ] 编写契约和 API 测试：

```python
def test_idempotency_fallback_is_stable():
    first = source_event_key("c1", "doc1", "v2", SourceEventOperation.UPSERT)
    assert first == source_event_key("c1", "doc1", "v2", SourceEventOperation.UPSERT)

def test_connector_response_never_contains_secret(client):
    body = client.get("/api/knowledge_base/ds/connectors/c1").json()
    assert "secret" not in json.dumps(body).lower()
```

- [ ] 运行 `python -m pytest tests/unit/connectors/test_contract.py tests/api/test_source_connectors.py -v`；预期因协议/路由缺失而失败。
- [ ] 实现冻结 dataclass/协议、注册表 `get_connector(connector_type, config, secret)`、DTO 和 CRUD/服务依赖。`sync` 持久化一个 job 并返回 HTTP 202；请求过程中不得执行 source I/O。
- [ ] 运行这两个测试文件以及 `tests/test_api_routes.py`；预期 PASS 且 OpenAPI 中包含相应路径。
- [ ] 使用 `git commit -m "feat: add source connector control plane"` 提交精确的 connector/API 路径。

### 任务 3：事务式 Event 创建与 Outbox 分发

**接口：**
- 产出 `SourceEventService.record(change, connector_id, dataset_id) -> RecordEventResult`，结果为 `created/duplicate`。
- 产出 `dispatch_source_outbox(batch_size: int = 100) -> int`。
- 产出 `consume_source_event(event_id: str) -> None` Celery task。

- [ ] 编写失败的事务测试：重复 provider event 只生成一条 row/outbox；模拟发布失败后仍保持 pending；下一次分发成功发布；重复调用 consumer 最多创建一个 desired revision。
- [ ] 运行 `python -m pytest tests/unit/services/test_source_event_service.py tests/integration/source_sync/test_outbox_dispatch.py -v`；预期因 service/task 缺失而失败。
- [ ] 在一个 session 事务中实现 `record()`，并提供 fallback SHA-256 key。Dispatcher 使用 `FOR UPDATE SKIP LOCKED` 认领 row，仅发布 event ID，然后标记为 published；失败时递增 attempts 并设置 `available_at`，不丢失该 row。

```python
@celery_app.task(name="source.consume", acks_late=True)
def consume_source_event(event_id: str) -> None:
    run_async(source_event_consumer.consume(event_id))
```

Consumer 以条件方式认领 pending/published event，解析当前 connector/source 状态，调用 Revision Safety 接口，并写入 completed/ignored/failed。注册 `source-events` 路由和 Beat outbox 恢复任务。
- [ ] 运行 source event/outbox 测试以及 `tests/unit/tasks/test_celery_config.py`；预期在重复投递情况下仍 PASS。
- [ ] 使用 `git commit -m "feat: dispatch source events through outbox"` 提交精确的 service/task/test 路径。
