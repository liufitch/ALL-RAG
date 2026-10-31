# Source Sync 监听与端到端实施计划

> **面向代理式执行者：** 必须使用子技能：建议使用 superpowers:subagent-driven-development 或 superpowers:executing-plans，按任务逐项实施本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 交付签名 Webhook 监听、秒级任务启动指标、Connector 运维可观测性、安全加固和真实基础设施端到端验收。

**架构：** Webhook 入口只校验、归一化并持久化事件后快速返回；outbox/Celery 沿用统一管道。指标从 source occurred/received 到 indexing processing 分阶段记录；E2E 同时验证监听低延迟和轮询/对账补偿能力。

**技术栈：** Python 3.11、FastAPI、Celery/RabbitMQ、PostgreSQL、MinIO、Milvus、Prometheus-compatible metrics、pytest、Docker Compose。

**规格：** `docs/superpowers/specs/2026-08-31-source-change-sync-design.md`

## 全局约束

- 依赖 source sync 阶段 8–10 和完整索引端到端阶段 7。
- Webhook 正文大小有限，必须校验签名、时间窗口和重放 ID。
- HTTP 响应不得等待解析、Embedding 或 Milvus。
- 健康监听链路 SLO 是“有效变更到 indexing processing 的 P95 < 5 秒”，不是完整索引耗时。
- 停止监听后，完整对账必须能补偿变化。

---

## 文件结构

- 创建：`rag_modules/api/source_webhook_api.py`。
- 创建：`rag_modules/connectors/webhook_security.py`。
- 创建：`rag_modules/observability/source_sync_metrics.py`。
- 修改：`rag_modules/services/source_event_service.py`、`main.py`、`docker-compose.yml`。
- 创建：`tests/api/test_source_webhooks.py`。
- 创建：`tests/unit/observability/test_source_sync_metrics.py`。
- 创建：`tests/integration/source_sync/test_webhook_pipeline.py`。
- 创建：`tests/e2e/test_source_sync.py`。
- 修改：`README.md`、`docs/architecture.md`。

### 任务 1：签名 Webhook 与快速 Event 持久化

**接口：** 产出 `WebhookVerifier.verify(body: bytes, signature: str, timestamp: str, delivery_id: str) -> None`；`POST /api/source-webhooks/{connector_id}` 返回 202，重复投递时安全返回 200。

- [ ] 编写 API 测试，覆盖有效签名、无效签名 401、时间戳过期 401、负载过大 413、重复投递、未知 connector 404，以及响应不回显 secret/body。
- [ ] 运行 `python -m pytest tests/api/test_source_webhooks.py -v`；预期因路由/verifier 缺失而失败。
- [ ] 对 `timestamp + "." + body` 实现 HMAC-SHA256，使用恒定时间比较、可配置的 300 秒时间窗口，并持久化 delivery ID。通过 secret provider 获取 secret，使用 connector adapter 规范化 provider 负载，调用 `SourceEventService.record()`，并在 outbox 投递前返回。

```python
expected = hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, supplied):
    raise InvalidWebhookSignature()
```

- [ ] 运行 API 和 outbox 集成测试；预期重复投递只产生一个 event/outbox，且不会同步调用索引。
- [ ] 使用 `git commit -m "feat: receive signed source webhooks"` 提交精确的 webhook 路径。

### 任务 2：指标、结构化状态与告警

**接口：** 产出指标 `source_events_total`、`source_event_duplicates_total`、`source_change_detection_seconds`、`source_queue_seconds`、`source_activation_seconds`、`source_cleanup_pending`、`source_revisions_superseded_total`；connector 详情暴露时间戳/计数，但不暴露凭据。

- [ ] 使用内存 registry 编写指标测试，并编写 API 测试断言存在 `last_event_at`、`last_polled_at`、`last_reconciled_at`、`last_error_code`，同时不包含 secret 字段。
- [ ] 运行 `python -m pytest tests/unit/observability/test_source_sync_metrics.py tests/api/test_source_connectors.py -v`；预期缺少指标/状态字段。
- [ ] 实现一个精简的 metrics adapter，在 event 接收、outbox 发布、consumer claim、revision 激活/失败以及清理完成时调用。使用有限标签：connector type、operation、outcome 和 stage；绝不使用 dataset/document/event ID 作为标签。
- [ ] 运行指标/API 测试，并模拟 100 个 event 以断言标签序列数量有界；预期 PASS。
- [ ] 使用 `git commit -m "feat: observe source synchronization"` 提交精确的 metrics/API 路径。

### 任务 3：真实基础设施 E2E 与运维文档

**接口：** 使用所有 source sync 接口；产出可重复的 E2E 证据和运维人员恢复说明。

- [ ] 编写 E2E 测试，覆盖创建、内容未变、更新成功、更新时 Embedding 失败、权威删除、单次快照缺失、监听中断/对账恢复、重复投递、v2/v3 排序以及清理幂等性。

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

- [ ] 运行 `docker compose --profile app up -d --build` 和 `python -m pytest tests/e2e/test_source_sync.py -v`；预期初次运行会暴露缺失的 wiring。
- [ ] 完成 Compose health check、mock source/webhook 和 mock Embedding fixture。在 README/architecture 中记录 connector 创建、secret 轮换、手动 sync/reconcile、卡住的 outbox 恢复、失败 revision 重试、清理积压以及 P95 的解释。
- [ ] 重新运行 E2E，然后运行 `python -m pytest tests/unit tests/api tests/integration/source_sync -q`；预期零失败。收集 event-to-processing 时长，并断言本地健康监听场景计算出的 P95 低于 5 秒。
- [ ] 运行 `git diff --check`，检查精确状态，并仅使用 `git commit -m "test: verify source synchronization end to end"` 提交列出的 runtime/test/docs 路径。

## 最终验收门禁

在宣布功能完成前，调用 `superpowers:verification-before-completion`，并为以下命令收集最新执行证据：

```bash
python -m alembic downgrade -1
python -m alembic upgrade head
python -m pytest tests/unit tests/api tests/integration/source_sync tests/e2e/test_source_sync.py -v
git diff --check
```

还需检查 PostgreSQL，确认每个 document 只有一个 active revision；检查 Milvus，确认退役向量最终会被删除；并检查日志/API 响应，确认没有凭据泄漏。
