# Source Sync Polling and Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 connector 增量轮询、游标安全推进、完整对账、删除确认、每文档乱序防护和高频变更归并。

**Architecture:** Celery Beat 只调度到期 connector；poll/reconcile Worker 使用 PostgreSQL lease，先持久化整批事件再推进 cursor/checkpoint。完整快照只有明确成功时可以驱动缺失确认，所有结果仍进入统一 source event/outbox 管道。

**Tech Stack:** Python 3.11、SQLAlchemy async、PostgreSQL、Celery Beat、RabbitMQ、pytest。

**Spec:** `docs/superpowers/specs/2026-08-31-source-change-sync-design.md`

## Global Constraints

- 依赖 Connectors and Outbox 计划。
- 批次任一页失败时不得推进 cursor。
- 普通单次缺失不得删除；非权威删除必须满足连续完整快照或宽限期。
- 对账不直接下载正文或调用索引，必须记录统一事件。
- 每个 connector 同时只有一个有效 poll/reconcile lease。

---

## File Structure

- Modify: `rag_modules/db/models.py` — connector lease、next poll 和 snapshot run 字段。
- Create: `migrations/versions/20260831_04_connector_polling.py` — 轮询/对账增量字段和索引。
- Create: `rag_modules/services/connector_poll_service.py`。
- Create: `rag_modules/services/reconciliation_service.py`。
- Create: `rag_modules/repositories/connector_lease_repository.py`。
- Create: `rag_modules/tasks/connector_tasks.py`。
- Modify: `rag_modules/tasks/celery_app.py`、`rag_modules/config/settings.py`。
- Test: `tests/unit/services/test_connector_poll_service.py`、`tests/unit/services/test_reconciliation_service.py`、`tests/integration/source_sync/test_connector_leases.py`、`tests/integration/source_sync/test_event_ordering.py`。

### Task 1: Poll Lease and Cursor-safe Incremental Sync

**Interfaces:** Produces `ConnectorLeaseRepository.claim(connector_id, owner, ttl) -> bool`; `ConnectorPollService.poll(connector_id) -> PollResult(events, next_cursor)`; `poll_connector(connector_id)` task.

- [ ] Write failing tests where adapter raises before returning a batch and stored cursor remains old, a successful batch creates events and advances cursor once, `has_more=True` schedules the next batch, and concurrent claim has one winner.
- [ ] Run `python -m pytest tests/unit/services/test_connector_poll_service.py tests/integration/source_sync/test_connector_leases.py -v`; expect failures.
- [ ] Add `lease_owner`、`lease_expires_at`、`heartbeat_at`、`next_poll_at` by the `20260831_04_connector_polling.py` migration and implement conditional claim/heartbeat. Set `connector_poll_batch_size=1000`; one `poll_changes(cursor)` call returns at most one atomic `ChangeBatch(changes, next_cursor, has_more)`. Persist its normalized events and cursor in one transaction. When `has_more=True`, enqueue the same connector again only after commit, so every committed cursor has all preceding events.

```python
@celery_app.task(name="source.poll_connector", acks_late=True)
def poll_connector(connector_id: str) -> None:
    run_async(poll_service.poll(connector_id))
```

- [ ] Run poll/lease tests and Celery routing tests; expect PASS and no cursor advance after failure.
- [ ] Commit `rag_modules/db/models.py migrations/versions/20260831_04_connector_polling.py rag_modules/services/connector_poll_service.py rag_modules/repositories/connector_lease_repository.py rag_modules/tasks/connector_tasks.py rag_modules/tasks/celery_app.py rag_modules/config/settings.py tests/unit/services/test_connector_poll_service.py tests/integration/source_sync/test_connector_leases.py` with `git commit -m "feat: poll source connectors safely"`.

### Task 2: Authoritative Snapshot Reconciliation

**Interfaces:** Produces `ReconciliationService.reconcile(connector_id) -> ReconcileResult(seen, upserts, missing, deletes)` and config `missing_snapshot_threshold=2`, `missing_grace_seconds=86400`.

- [ ] Write failing tests:

```python
async def test_one_missing_snapshot_does_not_delete(service):
    result = await service.reconcile("c1")
    assert result.deletes == 0
    assert await missing_since("source-doc-1") is not None

async def test_incomplete_snapshot_cannot_mark_missing(service):
    service.connector.snapshot.complete = False
    await service.reconcile("c1")
    assert await missing_since("source-doc-1") is None
```

- [ ] Run `python -m pytest tests/unit/services/test_reconciliation_service.py -v`; expect missing service.
- [ ] Implement temporary snapshot/run identity, batched seen-key upsert and finalize only when `snapshot.complete is True`. Clear `missing_since` on reappearance; produce DELETE through `SourceEventService` only after threshold or grace. Persist checkpoint only after finalize.
- [ ] Run reconciliation tests including provider timeout, paging failure, reappearance and authoritative delete; expect PASS.
- [ ] Commit exact reconciliation paths with `git commit -m "feat: reconcile source snapshots safely"`.

### Task 3: Due Scheduling, Ordering and Debounce

**Interfaces:** Produces `dispatch_due_connectors(limit: int = 100) -> int`; source ordering fields `received_sequence` and desired revision conditional activation; config `debounce_seconds` default 2.

- [ ] Write failing integration tests for v2/v3 out-of-order completion, DELETE then newer UPSERT, duplicate v3, and three UPSERT events inside the debounce window.
- [ ] Run `python -m pytest tests/integration/source_sync/test_event_ordering.py -v`; expect stale version activation or extra jobs.
- [ ] Implement due query with `FOR UPDATE SKIP LOCKED`, schedule poll/reconcile to their queues, and update next-run timestamps. Consumer coalesces non-processing UPSERT events per source key to the newest version; older ones become ignored/superseded. Activation still uses desired revision as the final guard.
- [ ] Run ordering tests, revision activation regression and duplicate outbox tests; expect only v3 active and one effective indexing job.
- [ ] Commit exact task/service/test paths with `git commit -m "feat: order and reconcile source changes"`.
