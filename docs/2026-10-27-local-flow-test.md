# 2026-10-27 本地 Docker 流程测试与问题记录

本文记录本地启动、真实 PaddleOCR 流程测试中遇到的问题。每项包含**现象、原因、解决方案和验证结果**。本次经济索引/OCR 流程验收已完成，13 项问题均已修复并回归；高质量索引缺少真实 Embedding 服务，单独列为未验收项。

## 测试环境与范围

- macOS Apple Silicon，Docker Desktop；应用采用 `linux/amd64` CPU 容器。
- 前端：<http://127.0.0.1:5173>；API：<http://127.0.0.1:8000>。
- 复用 PostgreSQL、MinIO、RabbitMQ、Milvus、API、Celery Worker 和共享模型缓存。
- 真实模型：PaddleOCR 3.7.0、PaddlePaddle 3.2.2、PP-OCRv5 中文检测与识别模型。
- 样本：PNG、JPEG、扫描 PDF、文本层与扫描页混合 PDF，以及 TXT、Markdown、DOCX、XLSX、CSV，共 9 份文档。
- OCR 样本文字：`文档解析与清洗测试`、`OCR TEST 123`、`项目编号 ALPHA2026 金额 1280 元`。
- 原始响应、执行脚本、日志及截图保存在本机 `data/flow-test-2026-10-27/`（运行产物目录，不纳入 Git）。

## 问题 1：OCR 段落合并后丢失置信度和文字框

**现象**：PNG 预览返回 200，文字识别正确，但多个 OCR 段落合并后的 `source_metadata` 只有页码和 `ocr` 标记，没有 `confidence` 和 `boxes`。

**原因**：通用分段器的元数据合并仅保留所有输入值完全相同的普通字段。不同段落的置信度、文字框通常不同，因此被丢弃；父子分段的全文父块也复用该逻辑。

**解决方案**：合并 OCR 段落时，明确计算来源段落置信度的平均值，按顺序汇总文字框；每个分段最多保存 100 个框，超限或来源已截断时保留 `boxes_truncated=true`。不修改原解析块。

**验证结果**：修复前新增 4 项回归均失败；修复后普通分段、全文父子分段、框数量上限及截断标记用例通过。真实图片预览已返回置信度和文字框。

**代码**：`rag_modules/segmentation/segmenter.py`；`tests/unit/segmentation/test_segmenters.py`。

## 问题 2：确认索引返回 503，任务无法创建

**现象**：上传与预览正常，`POST /api/knowledge_base/{id}/indexing/configure` 返回 503。直接调用服务捕获 PostgreSQL 错误：`dataset_indexes` 违反 `fk_dataset_indexes_created_by_job` 外键约束。整库重建采用相同写入模式。

**原因**：`indexing_jobs.target_index_id` 引用索引版本，`dataset_indexes.created_by_job_id` 又引用任务，形成双向引用。一次性加入 ORM 会话并提交不能保证满足即时外键约束，实际先插入了引用尚不存在任务的索引版本。原服务测试使用 FakeRepository，未覆盖真实外键。

**解决方案**：在同一事务中先保存 `target_index_id=NULL` 的任务并 flush，再保存索引版本并 flush，最后绑定任务目标和文档任务后提交。确认索引、整库重建，以及尚无活动索引时的单文档重建入口均修正；兼容入口还在绑定文档前先 flush 处理规则。

**验证结果**：在 SQLite 开启 `PRAGMA foreign_keys=ON` 后，确认、整库重建及首次单文档重建回归修复前失败、修复后通过。真实 PostgreSQL 确认及整库重建接口返回 202，Worker 执行并切换索引成功。

**代码**：`rag_modules/repositories/process_rule_repository.py`、`rag_modules/repositories/knowledge_base_repository.py`；`tests/unit/repositories/test_process_rule_repository.py`。

## 问题 3：Worker 连续 OCR 间歇失败，混合 PDF 可能遗漏扫描页

**现象**：API 预览识别成功，但 Celery 连续处理图片时，部分任务报 `OCR_FAILED`，底层为 Paddle `RuntimeError: std::exception`。纯图片进入重试，混合 PDF 会保留文本页并产生 `OCR_PAGE_FAILED` 警告，扫描页正文未进入索引。

**原因**：Worker 为每个任务建立新的 asyncio 事件循环及临时线程池，同时进程缓存复用 Paddle 预测器。该环境下原生预测器跨调用线程使用会失败，现有互斥锁只避免同时执行，不能保证线程一致。真实模型复现中，新建事件循环/线程池连续调用发生失败，而同一持久线程连续调用成功。

**解决方案**：每个 OCR 引擎使用一个延迟启动的单线程执行器，模型初始化、推理及结果适配都在该专用线程中执行，调用线程变化时仍复用同一预测器线程。修复后需要重新建立曾遗漏扫描页的索引，不能仅凭旧任务“completed”判定正文完整。

**验证结果**：线程绑定回归用例修复前失败、修复后通过。真实模型在 10 个独立 asyncio 循环下连续识别全部通过。新镜像重新创建 API、Worker 后，PNG、JPEG、扫描及混合 PDF 的完整 OCR 正文均已进入有效索引并可检索，无扫描页遗漏。

**代码**：`rag_modules/parsing/ocr.py`；`tests/unit/parsing/test_ocr_engine.py`。

## 问题 4：可重试任务一直停留在 retry_wait

**现象**：OCR 临时失败后，文档状态进入 `retry_wait`。即使超过 `available_at`，任务总进度仍停在 89%，Beat 周期运行但不重新投递。

**原因**：持久化认领逻辑支持 `pending`、`queued`、`retry_wait`，但 Beat 派发查询只选择 `pending`，漏掉到期的 `retry_wait`。

**解决方案**：派发查询同时选择 `pending` 和 `retry_wait`，继续以 `available_at <= 当前时间` 限定；成功发送后写入 `queued`、任务 ID 和派发次数。

**验证结果**：新增真实数据库查询回归，验证到期重试重新排队、未来重试保持等待；修复前失败、修复后通过。真实 Worker 重启后自动投递先前等待的 JPEG，原 9 文档任务达到 100%。

**代码**：`rag_modules/tasks/maintenance_tasks.py`；`tests/unit/tasks/test_recovery_tasks.py`。

## 问题 5：页面选择经济索引仍导致 INDEX_COMMAND_INVALID

**现象**：浏览器中默认选中高质量索引后切换“经济”，预览正常，正式索引立即失败，文档错误为 `INDEX_COMMAND_INVALID`。

**原因**：前端切换索引方式后保留默认 `embedding_model=bge-m3` 并传给后端；后端原样保存。Worker 的经济索引命令明确要求 `embedding_model is None`，因此拒绝执行。

**解决方案**：前端预览与确认共用请求构造函数，经济索引发送 `embedding_model=null`；后端对经济索引的模型字段也进行归一化，使任务、索引版本、配置快照和哈希保持一致。

**验证结果**：后端新增残留模型选择回归，前端流程用例增加从高质量切到经济并检查两次请求，修复前分别失败、修复后通过。真实浏览器已完成图片预览和 Worker 索引。

**代码**：`frontend/src/components/IndexingPanel.jsx`、`rag_modules/services/process_rule_service.py`；对应前后端回归测试。

## 问题 6：第二次索引显示完成，但新正文未生效

**现象**：9 文档重建的任务文档全部完成，混合 PDF 的新分段中也有完整 OCR 文字；但读取可见分段仍得到旧的 `Alphabeta`。Worker 最终提交报 `uq_dataset_indexes_one_active` 唯一约束错误，新分段仍处于 `indexing`。

**原因**：索引切换同时修改旧版本为 `retired`、新版本为 `active`，ORM 可能按主键顺序先激活新版本。PostgreSQL 的“每个知识库最多一个 active 索引”即时唯一约束此时失败。已有 SQLite 测试为了兼容建表删除了该索引，未覆盖这项实际约束。

**解决方案**：保持同一事务和现有锁，旧版本退休后先 flush，再激活新版本。增加启用真实部分唯一索引、且新版本主键排在旧版本前面的回归，确保成功切换不依赖随机 ID 顺序。

**验证结果**：新增部分唯一索引回归修复前失败、修复后通过，版本切换相关 6 项测试通过。真实 PostgreSQL 再次完成版本激活后，全文检索和 SSE 均已召回图片、扫描及混合 PDF 的 OCR 正文。

**代码**：`rag_modules/repositories/indexing_repository.py`；`tests/unit/repositories/test_index_version_switch.py`。

## 问题 7：本地 OCR 开关影响原有 PDF 单测

**现象**：直接运行解析测试出现 4 项失败，预期 `PDF_EMPTY_PAGE` / `OCR_NOT_ENABLED`，实际返回 `OCR_PAGE_FAILED` / `OCR_DEPENDENCY_MISSING`。

**原因**：本地 `.env` 已开启 OCR；宿主机虚拟环境按默认安装不包含 Paddle 等可选依赖。测试隐式依赖“默认 OCR 关闭”，读入本地配置后行为改变。

**解决方案**：在测试文本层与 OCR 关闭行为的模块中显式固定 `ocr_enabled=false`，通过 monkeypatch 在每项测试后恢复，避免修改实际应用配置。真实 OCR 验收继续在已装依赖的容器中执行。

**验证结果**：原相关 223 项测试在关闭测试环境开关后通过；加入隔离 fixture 后，无需额外环境变量的后端回归通过。最终共 680 项通过。

**代码**：`tests/unit/parsing/test_pdf_docx.py`。

## 问题 8：手工修改分段并保存返回 500

**现象**：OCR 索引和检索成功后，调用 `PATCH /api/knowledge_base/{id}/documents/{id}/segments` 保存修改返回 500，无法创建修订任务。

**原因**：`document_revisions.indexing_job_id` 存在即时外键约束，原实现把 revision、job 和 job-document 一次性加入会话提交，实际先插入了引用尚不存在任务的修订，触发 `fk_document_revisions_indexing_job`。

**解决方案**：在同一事务中先保存索引任务并 flush，再保存引用该任务的修订和文档任务。任务中的 `revision_id` 本身无数据库外键，故该顺序满足当前表结构约束。

**验证结果**：启用真实外键约束的回归通过。新镜像上的 API 和浏览器实际保存均返回 202，Worker 完成后可分别检索到 `MANUALREV2026` 和 `BROWSERREV2026`，原图片下载 SHA-256 不变。

**代码**：`rag_modules/services/document_revision_service.py`；`tests/unit/repositories/test_process_rule_repository.py`。

## 问题 9：重索引任务使用了数据库约束不接受的枚举

**现象**：修复修订的外键顺序后，保存分段仍返回 500，错误变为 `ck_indexing_jobs_job_type` CHECK 约束失败。核对整库重建还发现其 `scope` 值也不在数据库允许范围内。

**原因**：实际迁移允许的任务类型是 `initial_index`、`add_documents`、`reindex_documents`、`reindex_dataset`，但修订和单文档重建代码使用 `document_reindex`；范围允许 `selected_documents`、`entire_dataset`，整库重建却写 `all_documents`。ORM 元数据没有包含这些 CHECK 约束，原 SQLite 单测无法检出。

**解决方案**：写入路径统一遵循已有数据库契约，文档重索引写 `reindex_documents`，整库重建写 `entire_dataset`，不放宽数据库约束；Worker 的修订分段命名空间兼容新的规范名称及旧名称。回归测试在独立复制的表元数据中显式加入原迁移的 CHECK 约束，并启用外键。

**验证结果**：加入真实约束后，修订和整库重建两项用例修复前失败、修复后通过。实际 PostgreSQL 中修订和整库重建均已执行成功，未修改或放宽数据库约束。

**代码**：`rag_modules/services/document_revision_service.py`、`rag_modules/repositories/indexing_repository.py`、`rag_modules/repositories/knowledge_base_repository.py`、`rag_modules/tasks/indexing_tasks.py`。

## 问题 10：修订任务已创建，但 Worker 执行时报 NameError

**现象**：保存分段接口返回 202，Worker 处理修订时在构造命名空间分段处报 `NameError: name 'PreviewSegment' is not defined`，任务进入重试。

**原因**：索引引擎的修订命名空间分支构造 `PreviewSegment`，模块却未导入该类型。普通首次索引不进入此分支，原测试未运行带命名空间的修订任务。

**解决方案**：补齐正式导入；新增带 `revision_segments` 和 `segment_namespace` 的引擎回归，验证直接索引人工修改正文、生成关键词，且不重新读取原文件或调用解析器。

**验证结果**：新增回归修复前以同样的 NameError 失败、修复后通过。原失败修订自动重试成功，新创建的 API 和浏览器修订也成功完成并检索到修改内容。

**代码**：`rag_modules/indexing/engine.py`；`tests/unit/indexing/test_document_engine.py`。

## 问题 11：确认的索引配置没有保存到知识库，后续重建配置错误

**现象**：已完成经济索引的知识库，在设置接口中仍显示 `high_quality`、无模型及空处理规则；整库重建会读取这份初始配置，而不是用户确认的分段与索引方式。

**原因**：确认接口只保存不可变任务快照和处理规则，没有同步更新 `datasets` 中用于设置展示及后续重建的当前索引配置。

**解决方案**：在确认的同一事务中同步保存索引方式、模型及提供方、分段处理规则和更新人/时间，合并 `partial_user_config`，保留无关配置。活动索引版本仍由成功完成的最终切换决定。

**验证结果**：新增“从初始高质量配置确认经济索引，再读取设置并创建重建任务”的真实数据库回归，修复前失败、修复后通过。实际设置接口返回 economy 与正确分段参数，9 文档整库重建成功，重建后仍召回 4 份 OCR 来源。

**代码**：`rag_modules/repositories/process_rule_repository.py`；`tests/unit/repositories/test_process_rule_repository.py`。

## 问题 12：编辑器混入退休索引分段，读取修订号为空

**现象**：重建与修订测试后，分段列表同时返回当前索引和退休索引的内容，可能出现相同 position 的两份正文。再次修订时会把多版本内容一起提交，部分输入合并后触发 `Preview segments produce duplicate deterministic IDs`。同时，分段 GET 接口始终返回 `revision=null`，编辑器无法携带真实版本号进行冲突检测。

**原因**：文档分段查询只筛选 `completed` 和未删除，没有限定所属索引必须 `active`；修订若在旧索引退休后完成，仍会留下退休版本上的 completed 行。接口也未从 `document_revisions` 读取实际最新版本。

**解决方案**：分段查询只返回当前活动索引的有效分段；仅在没有活动索引时回退读取无版本标识的旧数据。GET 接口返回文档最新修订版本（无修订为 0），编辑器原有的 `base_version` 提交即可启用服务端冲突检测。只读审查另发现问题 2 的首次单文档重建入口未覆盖，已同时补齐该入口的外键顺序回归。

**验证结果**：新增 retired/active/legacy 三类分段的真实数据库回归与接口修订号断言，修复前均失败、修复后通过；首次单文档重建外键回归亦通过。新镜像实际编辑流程只读取当前正文、携带真实修订号，保存和检索均通过。

**代码**：`rag_modules/repositories/document_repository.py`、`rag_modules/services/document_service.py`、`rag_modules/api/file_api.py`；对应仓储和 API 回归测试。

## 问题 13：并发修订可能返回“旧正文 + 新版本号”

**现象**：代码审查使用实际仓储/服务复现：用户 A 的修订 v1 仍在索引，用户 B 打开编辑器拿到旧正文和版本号 1；A 完成后，B 保存旧正文，服务误认为其基于 v1 编辑而接受，可能覆盖 A 的内容。

**原因**：正文只读取已激活分段，版本号却取包括 pending 在内的最新修订；原接口还先读正文、再读版本，修订恰好在两次查询之间完成也会产生不一致。

**解决方案**：先读取编辑版本，最新修订仍在途时返回 409 并提示完成后重载，再读取正文。若有新修订夹在两次读取之间，只可能获得较旧 token，保存端会按现有版本比较拒绝。完成、失败和取消均是允许重新编辑的终态，失败/取消版本仍返回真实编号，避免永久冲突。

**验证结果**：服务层“在途拒绝、完成/失败/取消后允许”和 API “拒绝在途修订前不读取旧正文”回归通过。审查确认读序保证对新修订并发成立。

**代码**：`rag_modules/services/document_service.py`、`rag_modules/repositories/document_repository.py`、`rag_modules/api/file_api.py`。

## 环境限制：高质量索引所需 Embedding 服务未启动

**现象**：从应用容器访问配置中的 `http://host.docker.internal:8001/v1/models` 连接失败，宿主机未发现对应监听服务。

**原因**：当前 Docker Compose 包含业务应用与存储/队列服务，没有启动实际的 Embedding 模型服务。

**解决方案**：启动与模型目录 `bge-m3` 对应的真实 OpenAI-compatible Embedding 服务，或配置可用的服务地址及凭据，并重启 API、Worker 后验证高质量索引和向量召回。本次先使用经济索引完成真实 OCR、持久化、全文检索流程；不使用假向量替代实际模型验收。

**验证结果**：MinIO、Milvus 的真实集成测试已运行，4 项通过；高质量索引和真实向量语义召回暂未验收通过。

## 测试操作中的差异（非产品缺陷）

- HTTP 测试客户端自动猜测 Markdown MIME 时上传被拒绝；按接口约定显式传 `text/markdown` 后成功。
- 扫描 PDF 的 OCR 将 `OCR TEST 123` 识别为 `OCR TEST123`，字母和数字正确但空格不同。验收比较允许空格差异，原识别文本保留用于检查。
- 浏览器确认索引后会自动关闭配置面板，测试脚本不应再等待面板中的“返回文档”按钮。该项属于测试脚本定位修正。

## 最终验收结果

验收日期：2026-10-27（Asia/Shanghai）。本次修复均保留在工作区，未提交 Git。

| 验收项 | 结果 | 证据 |
|---|---|---|
| 后端单元/API 回归 | **680 通过**，2 条依赖弃用警告 | `data/flow-test-2026-10-27/backend-tests.log` |
| 前端构建 | 通过 | `frontend-build.log` |
| 前端 Playwright 既有回归 | **17 通过**；这些用例模拟 API，单独记录 | `frontend-tests.log` |
| MinIO / Milvus 真实集成回归 | **4 通过** | `integration-tests.log` |
| 新镜像内实际 Milvus 调用 | 建集合、写入、计数、删除、清理通过 | `milvus-new-image.log` |
| 9 格式文件上传、预览、正式索引 | 通过 | `results.json`、`run.log` |
| PNG / JPEG / 扫描 PDF / 混合 PDF 的中文、英文及编号 | 真实模型识别并召回通过 | `results.json` |
| 混合 PDF 文本层与扫描页、置信度与文字框 | 保留完整正文及来源元数据 | `results.json` |
| Paddle 连续跨事件循环调用 | **10 次通过** | `ocr-thread-fixed.log` |
| 重复上传与伪造图片签名 | 去重和 `INVALID_FILE_SIGNATURE` 符合预期 | `results.json` |
| 空白图片、加密 PDF、损坏 PDF | 分别返回 `OCR_NO_EXTRACTABLE_TEXT`、`PDF_ENCRYPTED`、`PDF_MALFORMED` | `negative-parser-checks.log` |
| 全文与 SSE 检索 | 召回 4 份 OCR 来源 | `results.json`、`retrieval.sse` |
| API 人工修订后检索 | `MANUALREV2026` 命中；原始下载 SHA-256 不变 | `results.json` |
| 9 文档整库重建与版本切换 | 完成后仍召回 4 份 OCR 来源 | `results.json` 的 `rebuild_passed=true` |
| 真实浏览器完整流程 | 预览、索引、设置、检索、来源跳转、编辑、修订后 `BROWSERREV2026` 召回均通过；页面脚本错误为 0 | `browser-results.json`、`browser.log` |
| 新 Docker 镜像重建并重新创建 API / Worker | 已完成；两个容器中 115 个 Python 源文件与工作区一致 | `docker-build.log`、`docker-runtime-build.log`、`runtime-verification.json` |

表中未带完整路径的产物均位于 `data/flow-test-2026-10-27/`。`results.json` 保留调试期间的历史错误响应，最终 `status=passed` 与末尾成功响应才代表本轮结论；文档问题记录保留这些失败的原因与修复过程。

浏览器截图：`browser-preview.png`、`browser-completed.png`、`browser-evidence.png`、`browser-revision-evidence.png`。验收知识库保留在控制台：**PaddleOCR 流程验收 2026-10-27**、**PaddleOCR 浏览器验收 2026-10-27**。

Docker 构建依赖耗时较长，期间源码继续修复，因此在完整依赖镜像构建成功后，使用同一依赖镜像追加复制最终源码，再重新创建 API 和 Worker。重新创建后再次执行真实 API 与浏览器流程并通过。正常从最终工作区运行 `docker compose --profile app up -d --build api worker` 也会包含全部源码修复。

仍未验收：真实 Embedding 生成、高质量索引的端到端语义召回，以及 GPU OCR。当前可直接使用经济索引和全文检索验证 OCR 文档处理。
