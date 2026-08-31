# Source Change Synchronization Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在完整知识库索引能力之上，分四个可独立验收阶段交付不可变文档 revision、通用数据源连接器、可靠变更事件、轮询/对账、Webhook 监听和秒级任务启动。

**Architecture:** PostgreSQL 保存 source、event、revision、outbox 和任务事实；RabbitMQ/Celery 执行至少一次的异步处理；MinIO 保存不可变源快照；Milvus 保存高质量向量。更新先构建并校验新 revision，再切换 PostgreSQL 可见性并异步清理旧资源。

**Tech Stack:** Python 3.11、FastAPI、Pydantic Settings、SQLAlchemy async、Alembic、PostgreSQL、Celery 5、RabbitMQ、MinIO、OpenAI-compatible Embedding、PyMilvus 2.5、pytest。

**Spec:** `docs/superpowers/specs/2026-08-31-source-change-sync-design.md`

## Global Constraints

- 必须先完成 `2026-08-31-dataset-indexing-roadmap.md` 的阶段 1–5；端到端阶段依赖其阶段 7。
- PostgreSQL 是 connector cursor、source event、document revision、任务状态和检索可见性的事实来源。
- 文档更新不得先删除旧向量；新 revision 最终失败时旧 revision 继续 active。
- 文档删除先撤销 PostgreSQL 可见性，再异步清理 Milvus 和 MinIO。
- Connector 只产生 UPSERT/DELETE，不直接写 segment 或调用 Milvus。
- 监听负责低延迟；增量轮询和完整对账负责最终一致性。
- RabbitMQ/Celery 按至少一次投递设计；不得引入 Redis 或依赖 Celery result backend。
- 原始内容变化第一阶段执行文档级安全重建，不实现块级 diff 或跨 revision segment 复用。
- 普通轮询单次缺失不得生成 DELETE；只有权威删除事件或完整快照的确认规则可以删除。
- 凭据只使用服务端 `SecretStr`/secret reference，不进入消息、日志或 API 响应。
- 每项业务变更使用 TDD：先运行目标测试并观察预期失败，再实现最小行为并运行回归。
- 每次 `git add` 使用精确路径，不提交工作区已有无关改动。

---

## Plan Set and Dependency Order

| 阶段 | 计划 | 独立交付物 | 依赖 |
|---|---|---|---|
| 8 | [Revision Safety](./2026-08-31-source-sync-08-revision-safety.md) | revision schema、三类 Hash、不可变 MinIO key、单文档先构建后切换、查询过滤 | 完整索引 1–5 |
| 9 | [Connector Events and Outbox](./2026-08-31-source-sync-09-connectors-outbox.md) | connector/source/event/outbox schema、adapter 协议、管理 API、可靠 RabbitMQ 投递 | 8 |
| 10 | [Polling and Reconciliation](./2026-08-31-source-sync-10-polling-reconciliation.md) | cursor 安全推进、轮询 lease、完整对账、删除确认和乱序合并 | 9 |
| 11 | [Listening and End-to-End](./2026-08-31-source-sync-11-listening-e2e.md) | 签名 Webhook、秒级事件触发、监控、安全测试和真实基础设施验收 | 8–10、完整索引 7 |

## Delivery Gates

每阶段只有满足以下门禁才能进入下一阶段：

1. 计划列出的目标测试通过，且测试确实覆盖该阶段状态切换和失败路径。
2. `python -m pytest tests/unit tests/api -q` 继续通过。
3. 涉及 PostgreSQL 时运行 Alembic upgrade/downgrade/upgrade 和真实约束测试。
4. 涉及 RabbitMQ、MinIO 或 Milvus 时运行对应 integration 测试。
5. `git diff --check` 通过，提交仅含计划中列出的精确文件。
6. 对外 API 变更后检查 `app.openapi()` 路径、状态码和脱敏响应。
7. 阶段 11 必须在真实 Compose 基础设施上运行 E2E 和故障恢复矩阵。

## Verification Commands

快速回归：

```bash
python -m pytest tests/unit tests/api -q
```

迁移和集成测试：

```bash
docker compose up -d postgres minio etcd standalone rabbitmq
python -m alembic upgrade head
python -m pytest tests/integration/source_sync -v
```

端到端：

```bash
docker compose --profile app up -d --build
python -m pytest tests/e2e/test_source_sync.py -v
```

格式和工作区边界：

```bash
git diff --check
git status --short
```

## Execution Checkpoints

- 阶段 8 后强制让 Embedding 或 Milvus 写入失败，确认旧 revision 仍可查询。
- 阶段 9 后在 source event 事务提交后阻断 RabbitMQ，确认 outbox 能补发且不重复建 revision。
- 阶段 10 后模拟分页中断、单次快照缺失、连续缺失和 v2/v3 乱序，逐项检查状态。
- 阶段 11 后停止监听链路，确认完整对账可补偿；恢复监听后测量 P95 任务启动延迟。

## Spec Coverage

| 规格章节 | 实施计划 |
|---|---|
| 1–3：目标、原则和总体架构 | 本路线图全局约束；8–11 |
| 4–5：稳定源标识和 Hash | 8 Tasks 1–2；9 Task 2 |
| 6：数据模型和 Outbox | 8 Task 1；9 Task 1 |
| 7：标准事件与发现方式 | 9 Tasks 2–3；10 Tasks 1–2；11 Task 1 |
| 8–9：UPSERT、DELETE | 8 Tasks 2–4；10 Task 3 |
| 10：并发、乱序和合并 | 8 Task 3；10 Task 3 |
| 11：RabbitMQ/Celery | 9 Task 3；10 Task 1；11 Task 1 |
| 12：查询一致性 | 8 Task 4；11 Task 3 |
| 13：配置变化范围 | 8 Task 3；11 Task 3 |
| 14：API | 9 Task 2；11 Task 1 |
| 15–16：可观测性和安全 | 11 Tasks 1–2 |
| 17：测试和验收 | 每阶段验证；11 Task 3 |
| 18–19：分阶段实施和决策 | 本路线图；8–11 |
