# 文档治理与分段编辑实施计划

> **面向代理工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans。各步骤使用 checkbox（`- [ ]`）语法跟踪进度。

**目标：** 为知识库文档实现下载、删除、重命名、启用/禁用、归档/恢复、重新索引和分段编辑，并在分段保存后自动建立新索引版本。

**架构：** 在现有 `DocumentService`/`DocumentRepository` 上增加文档治理和 revision 服务；用不可变 `document_revisions` 快照承载人工编辑内容；扩展现有索引任务，使普通任务读取 MinIO、revision 任务读取快照，并沿用新索引版本完成后原子切换。React 文档页面通过 API 操作菜单和分段编辑弹窗调用这些接口并轮询任务。

**技术栈：** FastAPI、SQLAlchemy async、Alembic、MinIO ObjectStorage、Celery publisher、现有 indexing engine、React/Vite、pytest、前端 Playwright/Vitest 配置。

**规格：** `docs/superpowers/specs/2026-10-03-document-governance-design.md`

## 全局约束

- 所有文档接口必须校验 `dataset_id` 和未删除文档归属。
- 删除、禁用、归档状态必须立即从检索范围排除。
- 分段编辑必须创建不可变 revision，并自动重新索引。
- 新索引失败时旧 active 索引继续可用。
- API 不得暴露 MinIO 对象地址或内部异常堆栈。
- 每个行为先写测试并确认失败，再写生产代码。

---

### 任务 1：修订模式与迁移 — 已完成

**文件：**
- 创建: `migrations/versions/20261003_01_document_governance.py`
- 修改: `rag_modules/db/models.py`
- 测试: `tests/unit/db/test_document_revision_model.py`

**接口：**
- 生成 `DocumentRevisionRecord`，包含不可变分段快照字段，并在 `(document_id, version)` 和 `(indexing_job_id)` 上建立索引。
- 添加服务和索引任务所需的修订关系字段，不改变现有文档主键。

- [ ] **步骤 1：编写失败的模型测试**

  断言修订记录保存 dataset/document ID、单调递增的版本号、JSON 分段、状态和可选索引任务 ID；并断言迁移会创建表和唯一约束。

- [ ] **步骤 2：运行针对性测试以确认失败**

  运行 `pytest tests/unit/db/test_document_revision_model.py -q`。预期：由于模型和迁移尚不存在，导入或查表失败。

- [ ] **步骤 3：实现模型和 Alembic 升级/降级**

  使用现有 JSON 类型辅助工具添加 SQLAlchemy 模型，为 datasets/documents/indexing jobs 设置外键和 UTC 时间戳，并在 `(document_id, version)` 上添加唯一约束。按照现有迁移风格添加建表/删表语句。

- [ ] **步骤 4：运行针对性测试**

  运行 `pytest tests/unit/db/test_document_revision_model.py -q`。预期：通过。

- [ ] **步骤 5：提交**

  `git add rag_modules/db/models.py migrations/versions/20261003_01_document_governance.py tests/unit/db/test_document_revision_model.py && git commit -m "feat: add document revision persistence"`

### 任务 2：文档 repository 与 service 治理操作 — 已完成

**文件：**
- 修改: `rag_modules/repositories/document_repository.py`
- 修改: `rag_modules/services/document_service.py`
- 创建: `rag_modules/services/document_revision_service.py`
- 测试: `tests/unit/repositories/test_document_governance.py`
- 测试: `tests/unit/services/test_document_governance_service.py`

**接口：**
- `DocumentRepository.get_active_document(dataset_id, document_id) -> DocumentRecord | None`
- `DocumentRepository.update_name(...)`, `set_enabled(...)`, `set_archived(...)`, `soft_delete(...)`
- `DocumentRepository.list_segments(dataset_id, document_id) -> list[DocumentSegmentRecord]`
- `DocumentRevisionService.create_revision_and_job(dataset_id, document_id, payload, actor_id) -> RevisionJobResult`

- [ ] **步骤 1：编写失败的 service 测试**

  覆盖重命名时保留 `object_key`、启用/禁用/归档/恢复的幂等性、软删除时设置 `deleted_at`、拒绝跨知识库 ID、校验非空分段内容和父级引用、递增修订版本，以及对过期 `base_version` 返回冲突错误。

- [ ] **步骤 2：运行测试并确认失败**

  运行 `pytest tests/unit/repositories/test_document_governance.py tests/unit/services/test_document_governance_service.py -q`。预期：方法/类缺失或断言失败。

- [ ] **步骤 3：实现 repository 修改和修订事务**

  使用按 dataset 限定且满足 `deleted_at IS NULL` 的异步 SQLAlchemy 查询，原子提交状态变更。创建修订时锁定文档/修订版本行，校验完整快照，在同一事务中创建 `DocumentRevisionRecord`、`IndexingJobRecord`、`DatasetIndexRecord` 和 `IndexingJobDocumentRecord`，并将文档状态设为 `queued`。

- [ ] **步骤 4：运行针对性测试，仅在通过后重构**

  运行相同的 pytest 命令；预期通过且无警告。保持所有状态转换幂等。

- [ ] **步骤 5：提交**

  `git add rag_modules/repositories/document_repository.py rag_modules/services/document_service.py rag_modules/services/document_revision_service.py tests/unit/repositories/test_document_governance.py tests/unit/services/test_document_governance_service.py && git commit -m "feat: add document governance service operations"`

### 任务 3：DTO 和文档治理 API — 已完成

**文件：**
- 修改: `rag_modules/api/dto/document.py`
- 修改: `rag_modules/api/file_api.py`
- 修改: `main.py` only if a new router is split out
- 测试: `tests/api/test_document_governance_api.py`

**接口：**
- 添加用于重命名、文档详情、分段列表/更新和修订任务响应的 Pydantic 请求/响应模型。
- 添加设计文档中的十个路由，并将领域错误映射为 404/409/422/503。

- [ ] **步骤 1：编写失败的 API 测试**

  测试下载的 content disposition/type、重命名请求、状态操作、删除、重新索引响应、分段列表/更新、跨知识库 404、过期修订 409 和对象存储失败 503。

- [ ] **步骤 2：运行 API 测试以确认失败**

  运行 `pytest tests/api/test_document_governance_api.py -q`。预期：因路由或响应模型缺失而返回 404。

- [ ] **步骤 3：实现 DTO、依赖项、路由和流式下载**

  添加复用现有 session/storage/publisher 依赖的治理 service。使用 `StreamingResponse` 封装对象存储流，将 `Content-Disposition` 清理为已保存的文件名，绝不序列化存储凭据或原始异常文本。

- [ ] **步骤 4：运行针对性 API 测试和现有文档 API 测试**

  运行 `pytest tests/api/test_document_governance_api.py tests/api/test_document_api.py -q`。预期：通过。

- [ ] **步骤 5：提交**

  `git add rag_modules/api/dto/document.py rag_modules/api/file_api.py main.py tests/api/test_document_governance_api.py && git commit -m "feat: expose document governance APIs"`

### 任务 4：支持 revision 的索引 worker — 已完成

**文件：**
- 修改: `rag_modules/tasks/indexing_tasks.py`
- 修改: `rag_modules/indexing/engine.py`
- 修改: `rag_modules/indexing/models.py`
- 修改: `rag_modules/repositories/indexing_repository.py`
- 修改: `rag_modules/repositories/segment_repository.py`
- 测试: `tests/unit/indexing/test_revision_indexing.py`

**接口：**
- `IndexDocumentCommand.revision_segments: tuple[PreviewSegment, ...] | None`
- `IndexingTaskRunner` loads revision snapshot when `job.revision_id` is set.
- `DocumentIndexingEngine.run` skips object download/parse/segment and stages `revision_segments` while retaining keyword/embedding/vector behavior.

- [ ] **步骤 1：编写失败的 worker 测试**

  断言修订任务不会调用对象存储/parser/segmenter，会暂存精确编辑后的分段、生成关键词或 embeddings、成功时激活新索引，失败时保留原 active 索引。

- [ ] **步骤 2：运行针对性测试以确认失败**

  运行 `pytest tests/unit/indexing/test_revision_indexing.py -q`。预期：command 缺少修订输入，且 engine 仍会调用 parser。

- [ ] **步骤 3：实现修订 command 路径和终态更新**

  在 `build_task_runner` 中加载修订 JSON，将各项转换为经过校验的 `PreviewSegment` 并传入 engine。随 job 状态将修订状态设为 `indexing`、`completed` 或 `failed`。由 `finalize_indexing_job` 负责原子切换 active 索引，只有成功 finalize 后才将修订标记为完成。

- [ ] **步骤 4：运行针对性测试和索引回归测试**

  运行 `pytest tests/unit/indexing/test_revision_indexing.py tests/unit/indexing/test_document_engine.py tests/unit/indexing/test_segment_persistence.py -q`。预期：通过。

- [ ] **步骤 5：提交**

  `git add rag_modules/tasks/indexing_tasks.py rag_modules/indexing/engine.py rag_modules/indexing/models.py rag_modules/repositories/indexing_repository.py rag_modules/repositories/segment_repository.py tests/unit/indexing/test_revision_indexing.py && git commit -m "feat: index edited document revisions"`

### 任务 5：可见性谓词和清理任务投递 — 已完成

**文件：**
- 修改: `rag_modules/repositories/knowledge_base_repository.py`
- 修改: `rag_modules/api/retrieval_api.py`
- 修改: `rag_modules/repositories/indexing_repository.py`
- 修改: `rag_modules/tasks/publisher.py`
- 创建: `rag_modules/tasks/document_cleanup_tasks.py`
- 测试: `tests/unit/repositories/test_document_visibility.py`
- 测试: `tests/unit/tasks/test_document_cleanup.py`

**接口：**
- 共享 `document_is_searchable()` SQL 谓词，仅匹配已启用、未归档、未删除且已完成的文档。
- `TaskPublisher.dispatch_document_cleanup(document_id, dataset_id) -> str | None`.

- [ ] **步骤 1：编写失败的可见性/清理测试**

  断言禁用、归档、删除、等待中和失败的文档会从列表/统计/检索中排除；断言清理会删除源对象并软删除旧分段，且不影响其他文档。

- [ ] **步骤 2：运行测试以确认失败**

  运行 `pytest tests/unit/repositories/test_document_visibility.py tests/unit/tasks/test_document_cleanup.py -q`。预期：禁用/归档文档仍然出现，或清理任务尚未定义。

- [ ] **步骤 3：实现谓词、路由筛选、publisher 和幂等清理任务**

  将该谓词应用于检索 join 和文档统计。仅在删除事务提交后投递清理任务。清理必须容忍对象缺失和重复执行。

- [ ] **步骤 4：运行针对性测试和检索回归测试**

  运行 `pytest tests/unit/repositories/test_document_visibility.py tests/unit/tasks/test_document_cleanup.py tests/api/test_dataset_api.py -q`。预期：通过。

- [ ] **步骤 5：提交**

  `git add rag_modules/repositories/knowledge_base_repository.py rag_modules/api/retrieval_api.py rag_modules/repositories/indexing_repository.py rag_modules/tasks/publisher.py rag_modules/tasks/document_cleanup_tasks.py tests/unit/repositories/test_document_visibility.py tests/unit/tasks/test_document_cleanup.py && git commit -m "feat: enforce document visibility and cleanup"`

### 任务 6：前端 API 模块和文档操作界面 — 已完成

**文件：**
- 修改: `frontend/src/api.js`
- 修改: `frontend/src/components/Documents.jsx`
- 修改: `frontend/src/styles.css`
- 测试: `frontend/tests/document-governance.spec.js`

**接口：**
- API helpers：`downloadDocument`、`renameDocument`、`setDocumentEnabled`、`setDocumentArchived`、`deleteDocument`、`reindexDocument`、`listDocumentSegments`、`saveDocumentSegments`、`getIndexingJob`。
- Documents 表格根据 DTO 字段渲染操作标签和操作状态。

- [ ] **步骤 1：编写失败的 Playwright 测试**

  覆盖下载按钮、重命名对话框、启用/禁用、归档/恢复、删除确认、重新索引等待状态和错误提示。通过现有测试服务器模式模拟 API 响应。

- [ ] **步骤 2：运行针对性前端测试以确认失败**

  若已配置 Vitest，则运行 `npm --prefix frontend test -- --run`；否则运行 `npm --prefix frontend exec playwright test frontend/tests/document-governance.spec.js`。预期：选择器和操作尚不存在。

- [ ] **步骤 3：实现操作菜单、对话框、筛选器和下载处理**

  添加带中文标签的无障碍按钮，仅对破坏性操作使用 `window.confirm`，下载时获取 blob，变更后刷新列表，等待期间禁用冲突操作，并通过 `request` 展示服务器错误。

- [ ] **步骤 4：运行前端针对性测试**

  运行 `npm --prefix frontend exec playwright test tests/document-governance.spec.js`。预期：通过。

- [ ] **步骤 5：提交**

  `git add frontend/src/api.js frontend/src/components/Documents.jsx frontend/src/styles.css frontend/tests/document-governance.spec.js && git commit -m "feat: add document governance controls"`

### 任务 7：分段编辑器和索引进度界面 — 已完成

**文件：**
- 修改: `frontend/src/api.js`
- 修改: `frontend/src/components/Documents.jsx`
- 创建: `frontend/src/components/SegmentEditor.jsx`
- 测试: `frontend/tests/segment-editor.spec.js`

**接口：**
- `SegmentEditor` 接收 `{ datasetId, document, onSaved, onClose }`，并提交 `{ base_version, segments }`。
- 持续轮询返回的 `job_id`，直至状态变为 `completed`、`partial_success`、`failed` 或 `cancelled`。

- [ ] **步骤 1：编写失败的分段编辑器测试**

  断言可以编辑分段 content/question/answer/keywords、调整顺序、显示校验错误和保存响应、“正在重新索引”状态、终态成功后刷新以及失败任务重试。

- [ ] **步骤 2：运行针对性前端测试以确认失败**

  运行 `npm --prefix frontend exec playwright test tests/segment-editor.spec.js`。预期：组件和选择器尚不存在。

- [ ] **步骤 3：实现编辑器和轮询**

  打开时加载分段，维护本地不可变行状态，校验非空内容和 parent ID，只提交一次并在轮询结束时中止请求。遇到 409 时显示刷新操作，不覆盖较新的编辑。

- [ ] **步骤 4：运行针对性前端测试和现有 console 测试**

  运行 `npm --prefix frontend exec playwright test tests/segment-editor.spec.js tests/console.spec.js`。预期：通过。

- [ ] **步骤 5：提交**

  `git add frontend/src/api.js frontend/src/components/Documents.jsx frontend/src/components/SegmentEditor.jsx frontend/tests/segment-editor.spec.js && git commit -m "feat: edit document segments with automatic reindexing"`

### 任务 8：完整验证与文档交接 — 已完成

**文件：**
- 修改: `docs/superpowers/specs/2026-10-03-document-governance-design.md` only for verified limitations or final API details.
- 修改: `docs/superpowers/plans/2026-10-03-document-governance.md` to check completed steps.

- [ ] **步骤 1：运行迁移和后端测试**

  在 `Graph-RAG/` 中运行 `pytest -q`，随后运行项目提供的迁移/模型专用检查命令（若有）。记录准确的通过/失败数量。

- [ ] **步骤 2：运行前端测试**

  运行 `npm --prefix frontend test -- --run` 和 `npm --prefix frontend exec playwright test`。将环境相关失败与产品问题分开记录。

- [ ] **步骤 3：审查需求清单**

  核实每项操作都有 API 路由、service 行为、UI 控件和测试；核实修订失败时旧 active 索引的行为；核实响应未返回对象存储 URL。

- [ ] **步骤 4：在 spec 中更新已验证的实现说明**

  记录有意偏差、支持范围限制和准确的 task/job 状态值。所有验证命令都取得最新成功输出后才能声明完成。

- [ ] **步骤 5：提交文档和最终实现状态**

  `git add docs/superpowers/specs/2026-10-03-document-governance-design.md docs/superpowers/plans/2026-10-03-document-governance.md && git commit -m "docs: record document governance design and plan"`

## 本地合并记录

- 当前工作目录已经位于 `main` 分支，功能提交 `e2818b2` 已是 `main` 的 HEAD，因此没有可执行的 feature → main 合并差异。
- 本地 `main` 相对 `origin/main` ahead 3，分别包含设计/计划、revision 持久化和文档治理实现。
- 保留未跟踪的 `uv.lock`，它不是本次功能生成的文件，没有纳入合并提交。
- 收尾验证使用：`PYTHONPATH=. UV_CACHE_DIR=/tmp/graph-rag-uv-cache uv run --extra test pytest -q`（599 passed, 3 skipped）、`npm run build`（成功）和 `git diff --check`（无输出）。
