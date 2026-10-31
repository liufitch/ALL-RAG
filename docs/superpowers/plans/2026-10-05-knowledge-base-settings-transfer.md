# 知识库设置与完整导入导出实施计划

> **面向代理工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans。各步骤使用 checkbox（`- [ ]`）语法跟踪进度。

**目标：** 为知识库提供设置修改、检索参数保存、全量重建，以及包含文档元数据和原始文件的完整 ZIP 导入导出。

**架构：** 在现有 `DatasetRecord` 和索引任务体系上增加知识库设置服务/API；新增独立 archive 服务负责 ZIP 生成、校验、对象存储搬运和导入事务；React 在文档页增加设置面板，复用现有 request、publisher 和文档刷新机制。

**技术栈：** FastAPI、Pydantic、SQLAlchemy async、Alembic、MinIO ObjectStorage、Celery publisher、React/Vite、pytest、Playwright。

**规格：** `docs/superpowers/specs/2026-10-05-knowledge-base-settings-transfer-design.md`

## 全局约束

- 导入默认只新建知识库，不覆盖或删除已有知识库。
- ZIP 不得包含向量、密钥、Celery 内部任务 ID 或对象存储地址。
- 导入包必须拒绝路径穿越、绝对路径、超限成员和 manifest 哈希不匹配。
- 导入文档统一为 `waiting`，索引必须按当前配置重新构建。
- 每个新行为先写失败测试并确认失败，再写生产代码。
- 设计和实际难点追加记录到 spec 与 `docs/superpowers/retrospectives/2026-10-05-knowledge-base-settings-transfer.md`。

---

### 任务 1：设置 DTO、repository 和 service

**文件：**
- 创建: `rag_modules/api/dto/knowledge_base/settings.py`
- 修改: `rag_modules/api/dto/knowledge_base/knowledgeBase.py`
- 修改: `rag_modules/repositories/knowledge_base_repository.py`
- 修改: `rag_modules/services/knowledge_base_service.py`
- 测试: `tests/unit/services/test_knowledge_base_settings.py`

**接口：**
- `KnowledgeBaseSettingsResponse` 包含 `dataset`、`indexing`、`retrieval`、`needs_rebuild`。
- `KnowledgeBaseSettingsUpdate` 接收基本字段和允许列表中的检索配置。
- `KnowledgeBaseService.get_settings(dataset_id)` 和 `update_settings(dataset_id, payload, actor_id)`。

- [ ] 为 NULL 配置的默认值、成功更新、权重校验，以及索引设置变化时的 `needs_rebuild` 编写测试。
- [ ] 运行 `pytest tests/unit/services/test_knowledge_base_settings.py -q`，确认因 DTO/service 方法缺失而失败。
- [ ] 实现允许列表 DTO、repository `update_settings`、默认检索值和变更检测。
- [ ] 运行针对性测试和现有 dataset API 测试。
- [ ] 提交 `feat: add knowledge base settings service`。

### 任务 2：设置、重建和检索 API

**文件：**
- 创建: `rag_modules/api/knowledge_base_settings_api.py`
- 修改: `main.py`
- 修改: `rag_modules/tasks/publisher.py`
- 修改: `rag_modules/repositories/indexing_repository.py`
- 测试: `tests/api/test_knowledge_base_settings_api.py`

**接口：**
- `GET/PATCH /api/knowledge_base/{dataset_id}/settings`.
- `POST /api/knowledge_base/{dataset_id}/rebuild` 返回 `{job_id, status}`。
- `KnowledgeBaseSettingsService` 依赖当前 DB session 和 publisher。

- [ ] 为读写、404、422 校验、重建任务创建和等待投递响应编写 API 测试。
- [ ] 运行针对性 API 测试，确认路由缺失导致失败。
- [ ] 实现路由、稳定的 404/422/503 映射、知识库全量重建任务创建和 publisher 方法。
- [ ] 运行针对性测试和现有 indexing API 测试。
- [ ] 提交 `feat: expose knowledge base settings and rebuild APIs`。

### 任务 3：归档格式和流式导出

**文件：**
- 创建: `rag_modules/services/knowledge_base_archive_service.py`
- 修改: `rag_modules/object_storage/base.py`
- 修改: `rag_modules/api/knowledge_base_settings_api.py`
- 测试: `tests/unit/services/test_knowledge_base_archive_service.py`
- 测试: `tests/api/test_knowledge_base_transfer_api.py`

**接口：**
- `KnowledgeBaseArchiveService.export_dataset(dataset_id) -> Path`.
- `KnowledgeBaseArchiveService.validate_archive(fileobj) -> ArchiveManifest`.
- `GET /api/knowledge_base/{dataset_id}/export` 返回包含 ZIP 内容的 `StreamingResponse`。

- [ ] 为 manifest 内容、安全文件路径、SHA-256 条目和导出响应头编写失败测试。
- [ ] 运行针对性测试，确认 service/路由缺失导致失败。
- [ ] 使用临时可 seek 文件和分块对象存储读取实现有界 ZIP 创建；为 dataset/documents 添加 repository 辅助方法。
- [ ] 运行针对性归档和 API 测试。
- [ ] 提交 `feat: export complete knowledge base archives`。

### 任务 4：安全归档导入与回滚

**文件：**
- 修改: `rag_modules/services/knowledge_base_archive_service.py`
- 修改: `rag_modules/api/knowledge_base_settings_api.py`
- 修改: `rag_modules/repositories/knowledge_base_repository.py`
- 修改: `rag_modules/repositories/document_repository.py`
- 测试: `tests/unit/services/test_knowledge_base_archive_import.py`
- 测试: `tests/api/test_knowledge_base_transfer_api.py`

**接口：**
- `KnowledgeBaseArchiveService.import_dataset(upload, rebuild: bool) -> ImportResult`.
- 生成新的 ID；原始 ID 存入 `data_source_info.imported_*`。

- [ ] 为成功导入、路径穿越、哈希不匹配、重复成员、大小限制，以及数据库失败时事务/对象清理编写失败测试。
- [ ] 运行针对性测试，在实现前确认失败。
- [ ] 实现 manifest 校验、安全解压、临时对象键、新 dataset/document 记录、异常清理和可选的重建投递。
- [ ] 运行针对性导入测试以及上传/文档回归测试。
- [ ] 提交 `feat: import complete knowledge base archives safely`。

### 任务 5：前端设置与传输面板

**文件：**
- 创建: `frontend/src/components/KnowledgeBaseSettings.jsx`
- 修改: `frontend/src/components/Documents.jsx`
- 修改: `frontend/src/api.js`
- 修改: `frontend/src/styles.css`
- 测试: `frontend/tests/knowledge-base-settings.spec.js`

**接口：**
- 面板属性：`{ dataset, onClose, onSaved }`。
- 调用设置 GET/PATCH、重建 POST、导出下载和导入上传接口。

- [ ] 为打开设置、检索字段、保存、重建确认、导出下载和导入结果添加失败的 Playwright 选择器测试。
- [ ] 运行针对性前端测试，建立失败基线。
- [ ] 实现无障碍面板状态、API 调用、校验消息、下载 blob 处理和导入结果处理。
- [ ] 在端口可绑定时运行 `npm run build` 和针对性 Playwright 测试。
- [ ] 提交 `feat: add knowledge base settings UI`。

### 任务 6：文档、回顾记录和完整验证

**文件：**
- 创建: `docs/superpowers/retrospectives/2026-10-05-knowledge-base-settings-transfer.md`
- 修改: `docs/superpowers/specs/2026-10-05-knowledge-base-settings-transfer-design.md`
- 修改: `docs/superpowers/plans/2026-10-05-knowledge-base-settings-transfer.md`

- [ ] 在回顾记录中记录实际实现偏差、测试/环境失败和修复措施。
- [ ] 运行 `PYTHONPATH=. UV_CACHE_DIR=/tmp/graph-rag-uv-cache uv run --extra test pytest -q`。
- [ ] 在 `frontend` 中运行 `npm run build`。
- [ ] 运行 `git diff --check`，检查最终 diff 中是否存在密钥和归档路径问题。
- [ ] 只有最新验证输出确认结果后才能报告完成。

