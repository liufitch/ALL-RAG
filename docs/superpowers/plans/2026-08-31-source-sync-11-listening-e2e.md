# Source Sync Listening and End-to-End Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付签名 Webhook 监听、秒级任务启动指标、Connector 运维可观测性、安全加固和真实基础设施端到端验收。

**Architecture:** Webhook 入口只校验、归一化并持久化事件后快速返回；outbox/Celery 沿用统一管道。指标从 source occurred/received 到 indexing processing 分阶段记录；E2E 同时验证监听低延迟和轮询/对账补偿能力。

**Tech Stack:** Python 3.11、FastAPI、Celery/RabbitMQ、PostgreSQL、MinIO、Milvus、Prometheus-compatible metrics、pytest、Docker Compose。

**Spec:** `docs/superpowers/specs/2026-08-31-source-change-sync-design.md`

## Global Constraints

- 依赖 source sync 阶段 8–10 和完整索引端到端阶段 7。
- Webhook 正文大小有限，必须校验签名、时间窗口和重放 ID。
- HTTP 响应不得等待解析、Embedding 或 Milvus。
- 健康监听链路 SLO 是“有效变更到 indexing processing 的 P95 < 5 秒”，不是完整索引耗时。
- 停止监听后，完整对账必须能补偿变化。

---

## File Structure

- Create: `rag_modules/api/source_webhook_api.py`。
- Create: `rag_modules/connectors/webhook_security.py`。
- Create: `rag_modules/observability/source_sync_metrics.py`。
- Modify: `rag_modules/services/source_event_service.py`、`main.py`、`docker-compose.yml`。
- Create: `tests/api/test_source_webhooks.py`。
- Create: `tests/unit/observability/test_source_sync_metrics.py`。
- Create: `tests/integration/source_sync/test_webhook_pipeline.py`。
- Create: `tests/e2e/test_source_sync.py`。
- Modify: `README.md`、`docs/architecture.md`。

### Task 1: Signed Webhook and Fast Event Persistence

**Interfaces:** Produces `WebhookVerifier.verify(body: bytes, signature: str, timestamp: str, delivery_id: str) -> None`; `POST /api/source-webhooks/{connector_id}` returning 202 or duplicate-safe 200.

- [ ] Write API tests for valid signature, invalid signature 401, expired timestamp 401, oversized payload 413, duplicate delivery, unknown connector 404, and response without secret/body echo.
- [ ] Run `python -m pytest tests/api/test_source_webhooks.py -v`; expect missing route/verifier.
- [ ] Implement HMAC-SHA256 over `timestamp + "." + body`, constant-time comparison, configurable 300-second window and persisted delivery ID. Resolve secret through secret provider, normalize provider payload with connector adapter, call `SourceEventService.record()`, and return before outbox delivery.

```python
expected = hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, supplied):
    raise InvalidWebhookSignature()
```

- [ ] Run API and outbox integration tests; expect one event/outbox for duplicate delivery and no synchronous indexing calls.
- [ ] Commit exact webhook paths with `git commit -m "feat: receive signed source webhooks"`.

### Task 2: Metrics, Structured Status and Alerts

**Interfaces:** Produces metrics `source_events_total`, `source_event_duplicates_total`, `source_change_detection_seconds`, `source_queue_seconds`, `source_activation_seconds`, `source_cleanup_pending`, `source_revisions_superseded_total`; connector detail exposes timestamps/counts but no credentials.

- [ ] Write metric tests with an in-memory registry and API tests asserting `last_event_at`, `last_polled_at`, `last_reconciled_at`, `last_error_code` are present while secret fields are absent.
- [ ] Run `python -m pytest tests/unit/observability/test_source_sync_metrics.py tests/api/test_source_connectors.py -v`; expect missing metrics/status fields.
- [ ] Implement one focused metrics adapter invoked at event receive, outbox publish, consumer claim, revision activation/failure and cleanup completion. Use bounded labels: connector type, operation, outcome and stage; never label by dataset/document/event ID.
- [ ] Run metrics/API tests and simulate 100 events to assert bounded label series; expect PASS.
- [ ] Commit exact metrics/API paths with `git commit -m "feat: observe source synchronization"`.

### Task 3: Real Infrastructure E2E and Operational Documentation

**Interfaces:** Consumes all source sync interfaces; produces repeatable E2E evidence and operator recovery instructions.

- [ ] Write E2E tests covering create, unchanged, update success, update Embedding failure, authoritative delete, single missing snapshot, listener outage/reconcile recovery, duplicate delivery, v2/v3 ordering and cleanup idempotency.

```python
def test_update_failure_preserves_previous_search_result(stack, source):
    source.publish("v1 text")
    wait_active("v1")
    stack.embedding.fail_next(401)
    source.publish("v2 text")
    wait_revision_status("v2", "failed")
    assert search("v1")
    assert not search("v2")
```

- [ ] Run `docker compose --profile app up -d --build` and `python -m pytest tests/e2e/test_source_sync.py -v`; expect initial failures for any missing wiring.
- [ ] Complete Compose health checks, mock source/webhook and mock Embedding fixtures. Document connector creation, secret rotation, manual sync/reconcile, stuck outbox recovery, failed revision retry, cleanup backlog and P95 interpretation in README/architecture.
- [ ] Re-run E2E, then `python -m pytest tests/unit tests/api tests/integration/source_sync -q`; expect zero failures. Collect event-to-processing durations and assert computed P95 below 5 seconds in the local healthy-listener scenario.
- [ ] Run `git diff --check`, inspect exact status, and commit only listed runtime/test/docs paths with `git commit -m "test: verify source synchronization end to end"`.

## Final Acceptance Gate

Before claiming the feature complete, invoke `superpowers:verification-before-completion` and capture fresh evidence for:

```bash
python -m alembic downgrade -1
python -m alembic upgrade head
python -m pytest tests/unit tests/api tests/integration/source_sync tests/e2e/test_source_sync.py -v
git diff --check
```

Also inspect PostgreSQL to confirm one active revision per document, inspect Milvus to confirm retired vectors are eventually removed, and inspect logs/API responses for credential leakage.
