# 知识库设置与完整导入导出 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (recommended). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为知识库提供设置修改、检索参数保存、全量重建，以及包含文档元数据和原始文件的完整 ZIP 导入导出。

**Architecture:** 在现有 `DatasetRecord` 和索引任务体系上增加知识库设置服务/API；新增独立 archive 服务负责 ZIP 生成、校验、对象存储搬运和导入事务；React 在文档页增加设置面板，复用现有 request、publisher 和文档刷新机制。

**Tech Stack:** FastAPI、Pydantic、SQLAlchemy async、Alembic、MinIO ObjectStorage、Celery publisher、React/Vite、pytest、Playwright。

**Spec:** `docs/superpowers/specs/2026-10-05-knowledge-base-settings-transfer-design.md`

## Global Constraints

- 导入默认只新建知识库，不覆盖或删除已有知识库。
- ZIP 不得包含向量、密钥、Celery 内部任务 ID 或对象存储地址。
- 导入包必须拒绝路径穿越、绝对路径、超限成员和 manifest 哈希不匹配。
- 导入文档统一为 `waiting`，索引必须按当前配置重新构建。
- 每个新行为先写失败测试并确认失败，再写生产代码。
- 设计和实际难点追加记录到 spec 与 `docs/superpowers/retrospectives/2026-10-05-knowledge-base-settings-transfer.md`。

---

### Task 1: Settings DTO, repository and service

**Files:**
- Create: `rag_modules/api/dto/knowledge_base/settings.py`
- Modify: `rag_modules/api/dto/knowledge_base/knowledgeBase.py`
- Modify: `rag_modules/repositories/knowledge_base_repository.py`
- Modify: `rag_modules/services/knowledge_base_service.py`
- Test: `tests/unit/services/test_knowledge_base_settings.py`

**Interfaces:**
- `KnowledgeBaseSettingsResponse` contains `dataset`, `indexing`, `retrieval`, `needs_rebuild`.
- `KnowledgeBaseSettingsUpdate` accepts basic fields plus an allow-listed retrieval configuration.
- `KnowledgeBaseService.get_settings(dataset_id)` and `update_settings(dataset_id, payload, actor_id)`.

- [ ] Write tests for defaults from NULL config, successful update, weight validation, and `needs_rebuild` when indexing settings change.
- [ ] Run `pytest tests/unit/services/test_knowledge_base_settings.py -q` and verify it fails because DTO/service methods are absent.
- [ ] Implement allow-listed DTOs, repository `update_settings`, default retrieval values, and change detection.
- [ ] Run the focused tests and existing dataset API tests.
- [ ] Commit `feat: add knowledge base settings service`.

### Task 2: Settings, rebuild and retrieval API

**Files:**
- Create: `rag_modules/api/knowledge_base_settings_api.py`
- Modify: `main.py`
- Modify: `rag_modules/tasks/publisher.py`
- Modify: `rag_modules/repositories/indexing_repository.py`
- Test: `tests/api/test_knowledge_base_settings_api.py`

**Interfaces:**
- `GET/PATCH /api/knowledge_base/{dataset_id}/settings`.
- `POST /api/knowledge_base/{dataset_id}/rebuild` returns `{job_id, status}`.
- `KnowledgeBaseSettingsService` dependency uses current DB session and publisher.

- [ ] Write API tests for read/write, 404, 422 validation, rebuild task creation and dispatch-pending response.
- [ ] Run focused API tests and verify missing route failures.
- [ ] Implement routes, stable 404/422/503 mapping, full dataset rebuild job creation and publisher method.
- [ ] Run focused and existing indexing API tests.
- [ ] Commit `feat: expose knowledge base settings and rebuild APIs`.

### Task 3: Archive format and streaming export

**Files:**
- Create: `rag_modules/services/knowledge_base_archive_service.py`
- Modify: `rag_modules/object_storage/base.py`
- Modify: `rag_modules/api/knowledge_base_settings_api.py`
- Test: `tests/unit/services/test_knowledge_base_archive_service.py`
- Test: `tests/api/test_knowledge_base_transfer_api.py`

**Interfaces:**
- `KnowledgeBaseArchiveService.export_dataset(dataset_id) -> Path`.
- `KnowledgeBaseArchiveService.validate_archive(fileobj) -> ArchiveManifest`.
- `GET /api/knowledge_base/{dataset_id}/export` returns `StreamingResponse` with ZIP content.

- [ ] Write failing tests for manifest contents, safe file paths, SHA-256 entries, and export response headers.
- [ ] Run focused tests and verify missing service/route failures.
- [ ] Implement bounded ZIP creation using a temporary seekable file and chunked object storage reads; add repository helpers for dataset/documents.
- [ ] Run focused archive and API tests.
- [ ] Commit `feat: export complete knowledge base archives`.

### Task 4: Secure archive import and rollback

**Files:**
- Modify: `rag_modules/services/knowledge_base_archive_service.py`
- Modify: `rag_modules/api/knowledge_base_settings_api.py`
- Modify: `rag_modules/repositories/knowledge_base_repository.py`
- Modify: `rag_modules/repositories/document_repository.py`
- Test: `tests/unit/services/test_knowledge_base_archive_import.py`
- Test: `tests/api/test_knowledge_base_transfer_api.py`

**Interfaces:**
- `KnowledgeBaseArchiveService.import_dataset(upload, rebuild: bool) -> ImportResult`.
- New IDs are generated; original IDs stored in `data_source_info.imported_*`.

- [ ] Write failing tests for successful import, path traversal, hash mismatch, duplicate member, size limits, and transaction/object cleanup on DB failure.
- [ ] Run focused tests and verify failure before implementation.
- [ ] Implement manifest validation, safe extraction, temporary object keys, new dataset/document records, cleanup on exception, and optional rebuild dispatch.
- [ ] Run focused import tests plus upload/document regression tests.
- [ ] Commit `feat: import complete knowledge base archives safely`.

### Task 5: Frontend settings and transfer panel

**Files:**
- Create: `frontend/src/components/KnowledgeBaseSettings.jsx`
- Modify: `frontend/src/components/Documents.jsx`
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/styles.css`
- Test: `frontend/tests/knowledge-base-settings.spec.js`

**Interfaces:**
- Panel props: `{ dataset, onClose, onSaved }`.
- Calls settings GET/PATCH, rebuild POST, export download, and import upload endpoints.

- [ ] Add failing Playwright selectors for settings open, retrieval fields, save, rebuild confirmation, export download and import result.
- [ ] Run the focused frontend test to establish failure.
- [ ] Implement accessible panel state, API calls, validation messages, download blob handling, and import result handling.
- [ ] Run `npm run build` and focused Playwright test where port binding is available.
- [ ] Commit `feat: add knowledge base settings UI`.

### Task 6: Documentation, retrospective and full verification

**Files:**
- Create: `docs/superpowers/retrospectives/2026-10-05-knowledge-base-settings-transfer.md`
- Modify: `docs/superpowers/specs/2026-10-05-knowledge-base-settings-transfer-design.md`
- Modify: `docs/superpowers/plans/2026-10-05-knowledge-base-settings-transfer.md`

- [ ] Record actual implementation deviations, test/environment failures, and fixes in the retrospective.
- [ ] Run `PYTHONPATH=. UV_CACHE_DIR=/tmp/graph-rag-uv-cache uv run --extra test pytest -q`.
- [ ] Run `npm run build` in `frontend`.
- [ ] Run `git diff --check` and inspect the final diff for secrets and archive path bugs.
- [ ] Only report completion after fresh verification output confirms the results.

