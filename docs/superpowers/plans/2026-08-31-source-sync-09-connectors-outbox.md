# Source Sync Connectors and Outbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 connector/source/event/outbox 持久模型、统一 adapter 协议、管理 API 和可靠 RabbitMQ 事件投递。

**Architecture:** Adapter 只读取源并生成 UPSERT/DELETE；服务在同一 PostgreSQL 事务写 source event 和 outbox；dispatcher 至少一次投递 `source-events` 队列，consumer 使用事件唯一键和 revision 服务幂等处理。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy async、Alembic、PostgreSQL、Celery、RabbitMQ、pytest。

**Spec:** `docs/superpowers/specs/2026-08-31-source-change-sync-design.md`

## Global Constraints

- 依赖 Revision Safety 计划；不绕过 `DocumentRevisionService` 写 revision。
- API 和消息不含 connector secret、正文或向量。
- Connector 只产生 UPSERT/DELETE；provider event ID 优先作为幂等键。
- PostgreSQL 提交与消息投递不得使用无补偿双写。

---

## File Structure

- Modify: `rag_modules/db/models.py`。
- Create: `migrations/versions/20260831_03_source_connectors.py`。
- Create: `rag_modules/connectors/base.py` — `SourceConnector` 协议和事件类型。
- Create: `rag_modules/repositories/source_repository.py`。
- Create: `rag_modules/services/source_event_service.py`。
- Create: `rag_modules/tasks/source_event_tasks.py`。
- Create: `rag_modules/api/source_connector_api.py`、`rag_modules/api/dto/source_connector.py`。
- Modify: `main.py`、`rag_modules/tasks/celery_app.py`。
- Test: `tests/unit/connectors/test_contract.py`、`tests/unit/services/test_source_event_service.py`、`tests/api/test_source_connectors.py`、`tests/integration/source_sync/test_outbox_dispatch.py`。

### Task 1: Connector, Source Event and Outbox Schema

**Interfaces:** Produces `SourceConnectorRecord`, `SourceDocumentRecord`, `SourceEventRecord`, `OutboxRecord`; source uniqueness `(connector_id, source_document_key)` and event uniqueness `(connector_id, source_event_id)` when non-null.

- [ ] Write failing model tests asserting tables, named unique constraints, cursor/checkpoint, `missing_since`, event status and outbox `available_at/attempts`.
- [ ] Run `python -m pytest tests/unit/db/test_source_models.py -v`; expect import/column failures.
- [ ] Implement ORM and incremental migration. Store only `secret_ref`; use JSONB for non-secret connector config. Create indexes for pending outbox and connector event history.

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

- [ ] Run model tests and a real Alembic upgrade/downgrade/upgrade; expect PASS.
- [ ] Commit exact schema/model/test paths with `git commit -m "feat: add source synchronization schema"`.

### Task 2: Adapter Contract, Event Normalization and Connector API

**Interfaces:**
- Produces `SourceChange(operation, source_document_key, source_version, source_event_id, source_etag, occurred_at)`.
- Produces `SourceConnector.poll_changes(cursor)`, `list_snapshot(checkpoint)`, `fetch_metadata(key)`, `open_content(key, version)`.
- Produces connector CRUD plus `POST .../{connector_id}/sync` and event list API.

- [ ] Write contract and API tests:

```python
def test_idempotency_fallback_is_stable():
    first = source_event_key("c1", "doc1", "v2", SourceEventOperation.UPSERT)
    assert first == source_event_key("c1", "doc1", "v2", SourceEventOperation.UPSERT)

def test_connector_response_never_contains_secret(client):
    body = client.get("/api/knowledge_base/ds/connectors/c1").json()
    assert "secret" not in json.dumps(body).lower()
```

- [ ] Run `python -m pytest tests/unit/connectors/test_contract.py tests/api/test_source_connectors.py -v`; expect missing protocol/routes.
- [ ] Implement frozen dataclasses/protocol, a registry `get_connector(connector_type, config, secret)`, DTOs and CRUD/service dependencies. `sync` persists a job and returns HTTP 202; it never performs source I/O in the request.
- [ ] Run the two test files plus `tests/test_api_routes.py`; expect PASS and OpenAPI paths present.
- [ ] Commit exact connector/API paths with `git commit -m "feat: add source connector control plane"`.

### Task 3: Transactional Event Creation and Outbox Dispatch

**Interfaces:**
- Produces `SourceEventService.record(change, connector_id, dataset_id) -> RecordEventResult` with `created/duplicate`.
- Produces `dispatch_source_outbox(batch_size: int = 100) -> int`.
- Produces `consume_source_event(event_id: str) -> None` Celery task.

- [ ] Write failing transaction tests: duplicate provider event produces one row/outbox; simulated publish failure leaves pending; next dispatch publishes; duplicate consumer invocation creates at most one desired revision.
- [ ] Run `python -m pytest tests/unit/services/test_source_event_service.py tests/integration/source_sync/test_outbox_dispatch.py -v`; expect missing service/tasks.
- [ ] Implement `record()` with one session transaction and fallback SHA-256 key. Dispatcher claims rows using `FOR UPDATE SKIP LOCKED`, publishes only event ID, then marks published; failures increment attempts and set `available_at` without losing the row.

```python
@celery_app.task(name="source.consume", acks_late=True)
def consume_source_event(event_id: str) -> None:
    run_async(source_event_consumer.consume(event_id))
```

Consumer conditionally claims pending/published events, resolves current connector/source state, calls Revision Safety interfaces, and writes completed/ignored/failed. Register `source-events` routing and Beat outbox recovery.
- [ ] Run source event/outbox tests plus `tests/unit/tasks/test_celery_config.py`; expect PASS under duplicate delivery.
- [ ] Commit exact service/task/test paths with `git commit -m "feat: dispatch source events through outbox"`.
