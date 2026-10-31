# Source Sync 轮询与对账实施计划

> **面向代理式执行者：** 必须使用子技能：建议使用 superpowers:subagent-driven-development 或 superpowers:executing-plans，按任务逐项实施本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 实现 connector 增量轮询、游标安全推进、完整对账、删除确认、每文档乱序防护和高频变更归并。

**架构：** Celery Beat 只调度到期 connector；poll/reconcile Worker 使用 PostgreSQL lease，先持久化整批事件再推进 cursor/checkpoint。完整快照只有明确成功时可以驱动缺失确认，所有结果仍进入统一 source event/outbox 管道。

**技术栈：** Python 3.11、SQLAlchemy async、PostgreSQL、Celery Beat、RabbitMQ、pytest。

**规格：** `docs/superpowers/specs/2026-08-31-source-change-sync-design.md`

## 全局约束

- 依赖 Connectors and Outbox 计划。
- 批次任一页失败时不得推进 cursor。
- 普通单次缺失不得删除；非权威删除必须满足连续完整快照或宽限期。
- 对账不直接下载正文或调用索引，必须记录统一事件。
- 每个 connector 同时只有一个有效 poll/reconcile lease。

---

## 文件结构

- 修改：`rag_modules/db/models.py` — connector lease、next poll 和 snapshot run 字段。
- 创建：`migrations/versions/20260831_04_connector_polling.py` — 轮询/对账增量字段和索引。
- 创建：`rag_modules/services/connector_poll_service.py`。
- 创建：`rag_modules/services/reconciliation_service.py`。
- 创建：`rag_modules/repositories/connector_lease_repository.py`。
- 创建：`rag_modules/tasks/connector_tasks.py`。
- 修改：`rag_modules/tasks/celery_app.py`、`rag_modules/config/settings.py`。
- 测试：`tests/unit/services/test_connector_poll_service.py`、`tests/unit/services/test_reconciliation_service.py`、`tests/integration/source_sync/test_connector_leases.py`、`tests/integration/source_sync/test_event_ordering.py`。

### 任务 1：轮询 Lease 与 Cursor 安全增量同步

**接口：** 产出 `ConnectorLeaseRepository.claim(connector_id, owner, ttl) -> bool`、`ConnectorPollService.poll(connector_id) -> PollResult(events, next_cursor)` 和 `poll_connector(connector_id)` 任务。

- [ ] 编写失败测试：adapter 在返回 batch 前抛出异常时已存 cursor 保持旧值；成功 batch 创建 event 并仅推进一次 cursor；`has_more=True` 会调度下一批；并发 claim 只有一个获胜者。
- [ ] 运行 `python -m pytest tests/unit/services/test_connector_poll_service.py tests/integration/source_sync/test_connector_leases.py -v`；预期失败。
- [ ] 通过 `20260831_04_connector_polling.py` 迁移添加 `lease_owner`、`lease_expires_at`、`heartbeat_at`、`next_poll_at`，并实现条件 claim/heartbeat。设置 `connector_poll_batch_size=1000`；一次 `poll_changes(cursor)` 调用最多返回一个原子 `ChangeBatch(changes, next_cursor, has_more)`。在一个事务中持久化其规范化 event 和 cursor。当 `has_more=True` 时，仅在提交后再次将同一 connector 入队，确保每个已提交 cursor 都包含其之前的所有 event。

```python
@celery_app.task(name="source.poll_connector", acks_late=True)
def poll_connector(connector_id: str) -> None:
    run_async(poll_service.poll(connector_id))
```

- [ ] 运行 poll/lease 测试和 Celery 路由测试；预期 PASS，且失败后 cursor 不推进。
- [ ] 使用 `git commit -m "feat: poll source connectors safely"` 提交 `rag_modules/db/models.py migrations/versions/20260831_04_connector_polling.py rag_modules/services/connector_poll_service.py rag_modules/repositories/connector_lease_repository.py rag_modules/tasks/connector_tasks.py rag_modules/tasks/celery_app.py rag_modules/config/settings.py tests/unit/services/test_connector_poll_service.py tests/integration/source_sync/test_connector_leases.py`。

### 任务 2：权威快照对账

**接口：** 产出 `ReconciliationService.reconcile(connector_id) -> ReconcileResult(seen, upserts, missing, deletes)`，以及配置 `missing_snapshot_threshold=2`、`missing_grace_seconds=86400`。

- [ ] 编写失败测试：

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

- [ ] 运行 `python -m pytest tests/unit/services/test_reconciliation_service.py -v`；预期因 service 缺失而失败。
- [ ] 实现临时 snapshot/run 标识、分批 seen-key upsert，并仅在 `snapshot.complete is True` 时 finalize。文档重新出现时清除 `missing_since`；仅在达到阈值或宽限期后通过 `SourceEventService` 生成 DELETE。仅在 finalize 后持久化 checkpoint。
- [ ] 运行包含 provider 超时、分页失败、重新出现和权威删除的对账测试；预期 PASS。
- [ ] 使用 `git commit -m "feat: reconcile source snapshots safely"` 提交精确的 reconciliation 路径。

### 任务 3：到期调度、排序与 Debounce

**接口：** 产出 `dispatch_due_connectors(limit: int = 100) -> int`；source 排序字段 `received_sequence` 和 desired revision 条件激活；配置 `debounce_seconds` 默认值为 2。

- [ ] 编写覆盖 v2/v3 乱序完成、DELETE 后跟随更新的 UPSERT、重复 v3 以及 debounce 窗口内三个 UPSERT event 的失败集成测试。
- [ ] 运行 `python -m pytest tests/integration/source_sync/test_event_ordering.py -v`；预期出现过期版本激活或额外任务。
- [ ] 使用 `FOR UPDATE SKIP LOCKED` 实现到期查询，将 poll/reconcile 调度到各自队列，并更新下一次运行时间戳。Consumer 按 source key 将未处理中的 UPSERT event 合并为最新版本；较旧版本变为 ignored/superseded。激活仍以 desired revision 作为最终保护条件。
- [ ] 运行排序测试、revision 激活回归测试和重复 outbox 测试；预期只有 v3 处于 active，且只有一个有效索引任务。
- [ ] 使用 `git commit -m "feat: order and reconcile source changes"` 提交精确的 task/service/test 路径。
