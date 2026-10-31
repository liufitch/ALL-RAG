# 索引工作流与 E2E 实施计划

> **面向代理工作者：** 必需子技能：使用 superpowers:executing-plans 按任务逐步实施本计划。

**目标：** 完成可取消的索引任务 API，将摄取与持久化索引统一，增加 broker-outbox 恢复机制，并验证完整的 PostgreSQL/MinIO/RabbitMQ/Celery/Milvus 链路。

**架构：** PostgreSQL 继续作为任务和投递状态的事实来源。API 请求创建短事务，只发布持久化标识符。Celery worker 通过租约领取任务文档；周期性 dispatcher 重试待发布消息。统一摄取复用现有上传校验、ProcessRule、索引 repository 和 task runner 组件。

**技术栈：** FastAPI、SQLAlchemy async、Alembic、Celery/RabbitMQ、MinIO、Milvus、pytest、Docker Compose。

**设计：** 前序对话中经用户确认的设计。

## 全局约束

- RabbitMQ 消息只包含标识符，不包含文件字节、凭据或完整文档内容。
- PostgreSQL 是任务状态的权威来源；Celery result backend 保持禁用。
- 每项新行为都先编写失败测试，再实现生产代码。
- 现有 `/documents` 和 `/documents/upload` 兼容路由继续可用。
- API 对外返回并持久化到任务中的错误信息必须安全且不含凭据。

### 任务 1：修复取消状态转换

**文件：**
- 修改: `rag_modules/repositories/indexing_repository.py`
- 测试: `tests/unit/repositories/test_indexing_repository.py` (create if absent)

- [ ] 添加测试：取消带有修订的运行中任务文档，断言不抛异常、状态为 `cancelled` 且修订错误为空。
- [ ] 运行针对性测试，确认当前未定义的 `error` 引用会导致失败。
- [ ] 用确定性的取消处理替换未定义引用。
- [ ] 运行针对性测试和 repository 测试套件。

### 任务 2：添加持久化任务 DTO 和 repository 操作

**文件：**
- 修改: `rag_modules/api/dto/` and `rag_modules/repositories/indexing_repository.py`
- 测试: `tests/unit/repositories/test_indexing_repository.py`

- [ ] 添加任务摘要、任务文档进度、重试和取消操作的请求/响应模式。
- [ ] 添加按 dataset 列出任务、加载带子行任务、请求取消以及使失败文档可重试的 repository 方法。
- [ ] 添加 dataset 隔离、重试资格、取消幂等性和终态任务保护测试。
- [ ] 运行针对性测试，然后运行全部单元测试。

### 任务 3：实现索引任务 API 路由

**文件：**
- 创建: `rag_modules/api/indexing_jobs_api.py`
- 修改: `main.py`
- 测试: `tests/api/test_indexing_jobs_api.py`

- [ ] 为创建/列表/详情/重试/取消编写失败的 API 测试，包括跨 dataset 访问 404 和终态冲突 409。
- [ ] 使用现有 ProcessRule/index repository 事务路径实现 `POST /api/knowledge_base/{dataset_id}/indexing/jobs`。
- [ ] 实现分页列表和详细进度响应。
- [ ] 实现重试投递和取消请求接口。
- [ ] 注册路由，运行 API 测试和现有 API 回归套件。

### 任务 4：实现统一摄取事务

**文件：**
- 创建或修改: `rag_modules/api/ingest_api.py`, `rag_modules/services/document_service.py`, `rag_modules/services/process_rule_service.py`, `rag_modules/repositories/process_rule_repository.py`
- 修改: `rag_modules/db/models.py` only if transaction metadata is missing
- 测试: `tests/api/test_ingest_api.py`, `tests/unit/services/test_ingest_service.py`

- [ ] 为混合批次的接受/拒绝、全部无效批次、重复幂等提交、数据库失败时的对象清理和 broker 失败时的待投递编写失败测试。
- [ ] 实现一个 service 边界，在持久化前校验所有文件和配置。
- [ ] 使用预生成的文档 ID 上传接受的文件，并保留精确对象键用于补偿。
- [ ] 在一个 PostgreSQL 事务中创建 ProcessRule、DatasetIndex、IndexingJob 和 IndexingJobDocument 行。
- [ ] 仅在提交后发布；返回接受的文档、被拒文件、任务 ID 和投递状态。
- [ ] 保留现有上传接口，兼容性测试通过后再将前端流程迁移到统一接口。

### 任务 5：添加投递 outbox 和待处理恢复

**文件：**
- 修改: `rag_modules/db/models.py`
- 创建: Alembic migration under `migrations/versions/`
- 修改: `rag_modules/repositories/indexing_repository.py`, `rag_modules/tasks/publisher.py`, `rag_modules/tasks/maintenance_tasks.py`, `rag_modules/tasks/celery_app.py`
- 测试: `tests/unit/tasks/test_dispatch_recovery.py`, `tests/api/test_ingest_api.py`

- [ ] 在任务文档行或独立 outbox 表中添加投递元数据：状态、尝试次数、下次尝试时间、最近错误代码和最近尝试时间戳。
- [ ] 为 broker 失败、重试退避、dispatcher 重复运行和成功转换为 queued 编写失败测试。
- [ ] 实现与任务文档一起创建投递记录的事务。
- [ ] 实现 Celery Beat 维护任务，领取到期的待处理记录并发布标识符。
- [ ] 使发布幂等，确保重复消息不会产生重复分段。
- [ ] 运行迁移测试和全部任务/API 测试。

### 任务 6：添加真实基础设施 E2E 测试框架

**文件：**
- 修改: `docker-compose.yml`
- 创建: `tests/e2e/conftest.py`, `tests/e2e/test_indexing_pipeline.py`, `tests/e2e/embedding_stub.py`
- 修改: `.env.example`, `README.md`

- [ ] 为 PostgreSQL、MinIO、RabbitMQ、Milvus、embedding stub、worker 和 beat 添加健康检查门控服务。
- [ ] 编写冒烟测试：创建 dataset、上传文档、等待完成，并验证 MinIO、PostgreSQL 分段、active DatasetIndex 和 Milvus 数量。
- [ ] 添加经济索引、取消、重试、重复投递和 broker 恢复场景。
- [ ] 在 compose 环境支持时添加 worker 重启和旧 active 索引保留场景。
- [ ] 使用 Docker 运行 E2E 套件，并记录准确的启动和清理命令。

### 任务 7：完整验证和文档

**文件：**
- 修改: `docs/pending-features.md`, `docs/architecture.md`, `README.md`

- [ ] 运行后端单元/API 测试、迁移测试、前端构建和基础设施 E2E。
- [ ] 修复完整套件发现的回归。
- [ ] 仅根据已验证事实更新功能状态和运维说明。
- [ ] 记录 Docker daemon 不可用或可选 Milvus 模式等已知限制。

