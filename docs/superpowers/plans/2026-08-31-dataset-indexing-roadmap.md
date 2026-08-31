# Complete Dataset Indexing Implementation Roadmap

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 分七个可独立验收的阶段交付 Dify 风格知识库创建、MinIO 文档存储、真实解析分段、OpenAI-compatible Embedding、Milvus 向量索引、RabbitMQ/Celery 持久化任务和完整前端流程。

**Architecture:** FastAPI 负责短事务 API，PostgreSQL 保存业务事实，MinIO 保存原始文件，RabbitMQ 为 Celery broker，独立 Worker 完成解析和索引，Milvus 只保存高质量索引向量。全量重建通过 `dataset_indexes` 双版本构建和原子激活避免旧索引中断。

**Tech Stack:** Python 3.11、FastAPI、Pydantic Settings、SQLAlchemy async、Alembic、PostgreSQL、MinIO Python SDK、Celery、RabbitMQ、HTTPX、PyMilvus 2.5、React 19、React Router、Vitest、Testing Library。

**Spec:** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## Global Constraints

- 向量只写 Milvus；`document_segments.vector` 本期保持 `NULL`。
- PostgreSQL 保存知识库、文档、正文分段、父子关系、关键词、任务、进度和索引版本。
- Celery broker 必须是 RabbitMQ；不得加入 Redis 依赖或 Redis result backend。
- Celery result backend 不得成为 API 查询任务状态的数据源。
- 原始文件必须存入 `graph-rag-uploads` MinIO bucket，API 和 Worker 不依赖共享本地上传目录。
- OpenAI-compatible API Key、MinIO Secret、RabbitMQ 密码、数据库密码和 Milvus token 不得返回前端或写入日志。
- 支持 `.txt`、`.md`、`.pdf`、`.docx`、`.xls`、`.xlsx`、`.csv`；本期不做 OCR 和 `.doc`。
- 父子分段只允许高质量索引，并且只对子块生成 Embedding。
- 经济索引不调用 Embedding、不创建或写入 Milvus。
- PostgreSQL/Milvus 跨系统写入必须通过构建版本、幂等 upsert 和补偿清理保证旧索引可用。
- 所有业务变更使用 TDD：先观察目标测试失败，再写最小实现，再运行相关回归测试。
- 不覆盖或顺带提交工作区已有的无关改动；每次 `git add` 必须列出精确路径。
- 执行阶段开始前使用 `superpowers:using-git-worktrees` 检查隔离方案；当前未提交基线若无法直接进入 worktree，先停下并与用户确认如何保存基线。

---

## Plan Set and Dependency Order

| 顺序 | 计划 | 独立交付物 | 依赖 |
|---|---|---|---|
| 1 | [Foundation and Schema](./2026-08-31-dataset-indexing-01-foundation-schema.md) | 配置、依赖、Alembic、任务/索引 ORM、空知识库 API、RabbitMQ 基础设施 | 无 |
| 2 | [MinIO Documents](./2026-08-31-dataset-indexing-02-minio-documents.md) | MinIO 存储抽象、文件校验、批量上传和文档列表 | 1 |
| 3 | [Parsing, Segmentation, Preview](./2026-08-31-dataset-indexing-03-parsing-preview.md) | 七种扩展名解析、普通/父子分段、真实预览 API | 2 |
| 4 | [Embedding and Milvus](./2026-08-31-dataset-indexing-04-embedding-milvus.md) | OpenAI-compatible 客户端、关键词、高质量/经济索引执行原语、Milvus schema/upsert | 1、3 |
| 5 | [Celery Orchestration](./2026-08-31-dataset-indexing-05-celery-orchestration.md) | 持久化任务 API、RabbitMQ/Celery Worker、恢复/取消/重试、版本切换和清理 | 2、4 |
| 6 | [Frontend Workflow](./2026-08-31-dataset-indexing-06-frontend-workflow.md) | Dify 风格三步页面、模型选择、真实预览、任务进度 | 2、3、5 |
| 7 | [End-to-End Hardening](./2026-08-31-dataset-indexing-07-e2e-hardening.md) | 真实基础设施 E2E、故障恢复、资源清理、旧 JSON 清理和运维文档 | 1–6 |

## Delivery Gates

每个阶段只有满足以下门禁才能进入下一阶段：

1. 该阶段列出的目标测试全部通过。
2. 既有 `tests/test_api_routes.py` 或其等价迁移测试继续通过。
3. `git diff --check` 无格式错误。
4. 提交只包含该阶段列出的精确文件。
5. 对外接口或迁移有变更时，OpenAPI/迁移检查已运行。
6. 阶段 6 以后还必须通过 `npm test -- --run` 和 `npm run build`。

## Full Verification Command Set

后端快速回归：

```bash
python -m pytest tests/unit tests/api -q
```

PostgreSQL/MinIO/RabbitMQ/Milvus 集成测试：

```bash
docker compose up -d postgres minio etcd standalone rabbitmq
python -m alembic upgrade head
python -m pytest tests/integration -v
```

前端：

```bash
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

端到端：

```bash
docker compose --profile app up -d --build
python -m pytest tests/e2e -v
```

## Execution Checkpoints

- 阶段 1 后审查迁移是否只增量修改现有三张业务表。
- 阶段 3 后用真实样例确认预览和正式分段调用同一实现。
- 阶段 4 后核对 PostgreSQL `vector` 列仍为空、Milvus entity ID 与 segment ID 一致。
- 阶段 5 后进行 Worker 强制退出和 RabbitMQ 重投测试。
- 阶段 6 后由用户进行页面流程验收。
- 阶段 7 后运行完整验收矩阵并使用 `superpowers:verification-before-completion` 做最终证据检查。

## Spec Acceptance Coverage

| 规格验收项 | 实施任务 |
|---|---|
| 1–3：先创建空知识库、跳转文档页、无 Milvus UI | 01 Task 4；06 Tasks 2、6 |
| 4–5：七种扩展名、MinIO 原始文件、PostgreSQL 对象元数据 | 02 Tasks 1–5；03 Tasks 2–4 |
| 6–9：普通/父子真实预览、Excel/CSV 结构 | 03 Tasks 4–6；06 Task 4 |
| 10–13：模型目录、密钥隔离、OpenAI-compatible 调用、自动维度 | 01 Tasks 1、3；04 Task 1；05 Task 4 |
| 14–17：后端建 collection、完整向量、父子只嵌入子块、ID 对齐 | 04 Tasks 3–6；07 Task 2 |
| 18–19：经济模式无 Embedding/Milvus、PostgreSQL 关键词 | 04 Tasks 2、5；07 Task 3 |
| 20–23：RabbitMQ/Celery、持久状态、重启恢复、重复消息幂等 | 01 Task 5；05 Tasks 1–6、8；07 Task 4 |
| 24：全量重建失败保留旧索引 | 05 Task 5；07 Task 4 |
| 25–26：文档/知识库异步清理向量、对象和 collection | 05 Task 7；07 Task 5 |
| 27–28：后端测试、前端测试和生产构建 | 每阶段 verification；06 Task 6 |
| 29：真实基础设施和 mock Embedding E2E | 07 Tasks 1–5 |
| 30：移除 `knowledge_bases.json` 和无用引用 | 07 Task 6 |

## Approved Extension: Source Change Synchronization

完整索引阶段 1–5 建立安全索引原语、RabbitMQ/Celery 和版本切换后，按[数据源变更同步路线图](./2026-08-31-source-sync-roadmap.md)继续实施：

| 后续阶段 | 交付物 | 前置依赖 |
|---|---|---|
| 8 | 文档 revision、Hash 去重、先构建后切换和查询过滤 | 本路线图 1–5 |
| 9 | Connector/source/event/outbox、管理 API 和可靠消息投递 | 8 |
| 10 | 增量轮询、完整对账、删除确认和乱序合并 | 9 |
| 11 | 签名 Webhook、秒级启动指标、安全和真实基础设施 E2E | 8–10、本路线图 7 |

详细规格：`docs/superpowers/specs/2026-08-31-source-change-sync-design.md`。
