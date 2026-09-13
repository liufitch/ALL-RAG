# 2026-09-05 代码注释统一为中文

## 用户要求与处理范围

用户要求“代码中的注释都使用中文”，并要求记录修改以便后续复盘。本轮将项目自有代码中的英文自然语言注释、模块/类/函数文档字符串统一翻译为中文，保留原有技术约束和原因说明。

共修改 60 个代码及配置文件：58 个 Python 文件、`.gitignore`、`.env.example`。合计翻译 255 处文档字符串、48 处注释（Python 注释 40 处，配置注释 8 处）。本复盘文档单独计算。

检查范围包含全部 117 个自有 Python 文件，以及前端源码和测试、构建配置、数据库迁移模板、Docker 配置和 Cypher 脚本。前端等文件中的现有说明已是中文，因此无需为了本轮要求再次修改。

## 翻译约定

- 说明文字使用中文；API、HTTP、MinIO、PostgreSQL、Unicode 等技术名称，以及变量、字段和错误码保留原名。
- 保留 7 处 `type: ignore[...]` 和 1 处 `noqa: F401` 工具指令；`pragma: no cover` 保留指令本身，其后英文解释已翻译。
- 保留 `main.py` 中 9 行被注释掉的代码，确保仍可辨认并恢复为合法代码。
- 文档字符串涉及事务归属、重试稳定性、并发取消、资源预算和编码校验等约束时，保留完整原因与限制。
- 迁移脚本仅翻译文档字符串和注释，版本标识、前置版本及建表语句均保持原样。
- `.env.example` 仅翻译说明；本轮未修改真实 `.env` 凭据。第三方依赖和生成产物不属于翻译范围。
- 后续新增或修改的自然语言注释继续使用中文。

## 修改文件清单

以下数量仅统计本轮翻译，不包含此前任务或用户已有的改动。

| 文件 | 文档字符串 | 注释 |
| --- | ---: | ---: |
| `migrations/versions/20260831_01_indexing_schema.py` | 1 | 1 |
| `rag_modules/api/dto/document.py` | 2 | 2 |
| `rag_modules/api/file_api.py` | 2 | 0 |
| `rag_modules/api/indexing_options_api.py` | 1 | 0 |
| `rag_modules/api/indexing_preview_api.py` | 1 | 0 |
| `rag_modules/db/models.py` | 0 | 2 |
| `rag_modules/documents/__init__.py` | 1 | 0 |
| `rag_modules/documents/types.py` | 1 | 0 |
| `rag_modules/documents/validation.py` | 1 | 0 |
| `rag_modules/embeddings/models.py` | 2 | 0 |
| `rag_modules/embeddings/openai_compatible.py` | 1 | 1 |
| `rag_modules/indexing/__init__.py` | 1 | 0 |
| `rag_modules/indexing/constants.py` | 1 | 0 |
| `rag_modules/indexing/engine.py` | 5 | 0 |
| `rag_modules/indexing/ids.py` | 6 | 1 |
| `rag_modules/indexing/keywords.py` | 5 | 13 |
| `rag_modules/indexing/models.py` | 5 | 0 |
| `rag_modules/object_storage/__init__.py` | 1 | 0 |
| `rag_modules/object_storage/base.py` | 1 | 0 |
| `rag_modules/object_storage/factory.py` | 1 | 0 |
| `rag_modules/object_storage/minio_store.py` | 2 | 0 |
| `rag_modules/parsing/__init__.py` | 1 | 0 |
| `rag_modules/parsing/base.py` | 2 | 0 |
| `rag_modules/parsing/csv_parser.py` | 1 | 0 |
| `rag_modules/parsing/docx_parser.py` | 1 | 0 |
| `rag_modules/parsing/factory.py` | 1 | 0 |
| `rag_modules/parsing/markdown_parser.py` | 3 | 0 |
| `rag_modules/parsing/models.py` | 4 | 0 |
| `rag_modules/parsing/pdf_parser.py` | 1 | 0 |
| `rag_modules/parsing/registry.py` | 2 | 0 |
| `rag_modules/parsing/tabular.py` | 8 | 0 |
| `rag_modules/parsing/text_parser.py` | 3 | 2 |
| `rag_modules/parsing/warnings.py` | 4 | 0 |
| `rag_modules/parsing/xls_parser.py` | 1 | 0 |
| `rag_modules/parsing/xlsx_parser.py` | 5 | 0 |
| `rag_modules/repositories/document_repository.py` | 5 | 0 |
| `rag_modules/repositories/segment_repository.py` | 9 | 2 |
| `rag_modules/segmentation/__init__.py` | 1 | 0 |
| `rag_modules/segmentation/models.py` | 5 | 0 |
| `rag_modules/segmentation/segmenter.py` | 8 | 2 |
| `rag_modules/services/document_service.py` | 2 | 4 |
| `rag_modules/services/preview_service.py` | 2 | 0 |
| `rag_modules/tasks/__init__.py` | 1 | 0 |
| `rag_modules/vector_stores/base.py` | 2 | 0 |
| `rag_modules/vector_stores/factory.py` | 0 | 1 |
| `tests/api/test_document_api.py` | 0 | 2 |
| `tests/api/test_indexing_preview_api.py` | 2 | 0 |
| `tests/conftest.py` | 3 | 2 |
| `tests/integration/test_milvus_index.py` | 3 | 2 |
| `tests/unit/config/test_settings.py` | 7 | 0 |
| `tests/unit/indexing/test_keywords.py` | 14 | 0 |
| `tests/unit/indexing/test_segment_persistence.py` | 1 | 3 |
| `tests/unit/parsing/test_pdf_docx.py` | 11 | 0 |
| `tests/unit/parsing/test_tabular.py` | 37 | 0 |
| `tests/unit/parsing/test_text_markdown.py` | 31 | 0 |
| `tests/unit/segmentation/test_segmenters.py` | 30 | 0 |
| `tests/unit/services/test_preview_service.py` | 2 | 0 |
| `tests/unit/vector_stores/test_milvus_store.py` | 1 | 0 |
| `.gitignore` | 0 | 6 |
| `.env.example` | 0 | 2 |

## 验证结果

1. **执行逻辑对比通过**：以本轮修改前保存的工作区快照为基准，解析全部 117 个 Python 文件的 AST，将文档字符串统一为占位值后逐一比较，结果完全一致。该验证保留并比较函数、装饰器、路由、普通字符串、表达式等执行内容，只排除本轮需要翻译的文档字符串。
2. **语言残留检查通过**：使用 Python 分词器识别注释、AST 识别文档字符串，复查纯英文说明以及中文文档字符串中的独立英文行。剩余 17 项为上述 8 处工具指令与 9 行注释代码，无待翻译的英文自然语言说明。
3. **差异格式检查通过**：`git diff --check` 无错误。
4. **现有回归测试**：执行 `.venv/bin/python -m pytest -q -m 'not integration'`，结果为 **569 通过、6 失败、3 未选中、3 条依赖弃用警告**。此次未运行访问外部基础设施的集成测试。
5. **修改前基线复核**：在临时目录恢复本轮修改前的 Python 快照与所需配置，执行上述失败所属的两个测试文件，结果为 **22 通过、相同 6 项失败**。因此不能将全套测试描述为通过，但已确认这 6 项失败在注释翻译前就存在。

## 已存在的上传路由与测试不一致

本轮开始时，上传处理函数仅注册 `POST /api/knowledge_base/{dataset_id}/documents/upload`，而以下测试仍请求旧路径 `POST /api/knowledge_base/{dataset_id}/documents`，导致返回 **405 Method Not Allowed**，未进入上传处理函数。

| 测试文件 | 失败用例 |
| --- | --- |
| `tests/api/test_document_api.py` | `test_batch_upload_returns_successes_and_rejections` |
| `tests/api/test_document_api.py` | `test_document_upload_maps_storage_unavailability_to_503` |
| `tests/api/test_document_api.py` | `test_upload_current_and_compatible_paths[/documents]` |
| `tests/api/test_document_api.py` | `test_document_upload_maps_minio_server_failure_to_503` |
| `tests/api/test_document_api.py` | `test_batch_validation_rejection_continues_and_commits_valid_documents` |
| `tests/unit/repositories/test_knowledge_base_summary.py` | `test_real_create_upload_detail_and_stats_api_contract` |

这与之前排查的 MinIO 503 不同：405 是请求路径与注册的 HTTP 方法不匹配；503 是进入上传处理后依赖服务无法完成操作。上述基线复核确认本轮未引入这项路由差异。后续处理接口契约时，需要同步明确是否保留旧路径，并让测试与约定一致。本轮保留工作区原有的路由行为。
