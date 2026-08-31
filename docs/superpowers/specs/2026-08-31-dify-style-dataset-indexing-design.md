# Dify 风格知识库创建与完整索引设计

日期：2026-08-31

状态：已由用户确认

范围：知识库创建、文档上传、真实分段预览、持久化索引任务、OpenAI-compatible Embedding、Milvus 向量索引及 PostgreSQL 元数据

## 1. 背景与目标

当前知识库创建页面把知识库属性、文件上传、检索参数和 Milvus 连接参数放在同一个弹窗中，与期望的 Dify 风格流程不一致。当前文件接口只把文件写入本地目录，尚未形成文档记录、解析、分段、Embedding、Milvus 写入和任务恢复的完整链路。

本设计将流程调整为：

1. 先创建空知识库。
2. 创建成功后进入该知识库的文档页面。
3. 上传一个或多个文件到 MinIO。
4. 选择高质量或经济索引。
5. 配置普通分段或父子分段并查看真实预览。
6. 确认后创建持久化索引任务。
7. Celery 通过 RabbitMQ 调度独立 Worker 完成解析、分段和索引。
8. PostgreSQL 保存全部业务元数据、任务状态和正文；Milvus 只保存高质量索引的向量。

前端流程参考 Dify 的空知识库创建和分步骤文档处理设计：

- <https://github.com/langgenius/dify/blob/main/web/app/components/datasets/create/empty-dataset-creation-modal/index.tsx>
- <https://github.com/langgenius/dify/blob/main/web/app/components/datasets/create/index.tsx>
- <https://github.com/langgenius/dify/blob/main/web/app/components/datasets/create/step-two/index.tsx>

## 2. 已确认的架构决策

- 使用现有 `datasets`、`documents`、`document_segments`，不再使用 `knowledge_bases` 表或 JSON 文件作为业务数据源。
- 后端从 PostgreSQL 读取知识库和文档元数据。
- OpenAI-compatible Embeddings API 负责生成向量。
- Milvus 负责保存高质量索引向量。
- PostgreSQL 负责保存知识库、文档、分段、父子关系、关键词、源定位、索引版本和任务状态。
- `document_segments.vector` 本期保持 `NULL`，不与 Milvus 重复保存向量。
- 后端预配置多个 Embedding 模型，前端可选择；密钥不返回前端。
- RabbitMQ 是 Celery broker，不使用 Redis。
- Celery result backend 不作为业务状态来源；前端只查询 PostgreSQL 中的任务状态。
- 原始文件存入 MinIO 的应用专用 bucket。
- 新增持久化任务表和独立 Worker，支持重试、取消、心跳、租约和服务重启恢复。
- 不在前端展示 Milvus 主机、端口、collection、维度或索引参数。

## 3. 范围与非目标

### 3.1 本期范围

- 空知识库创建。
- 文档列表和批量上传。
- `.txt`、`.md`、`.pdf`、`.docx`、`.xls`、`.xlsx`、`.csv` 解析。
- 普通分段。
- 父子分段。
- 真实分段预览。
- 高质量索引：Embedding + Milvus。
- 经济索引：关键词提取 + PostgreSQL GIN 索引。
- PostgreSQL 持久化任务状态。
- RabbitMQ/Celery 异步执行。
- 任务查询、取消和失败文档重试。
- 文档、知识库和失败索引的补偿清理。
- 前后端单元、集成和端到端测试。

### 3.2 非目标

- 扫描 PDF OCR。
- `.doc`、`.ppt`、`.pptx` 等未确认格式。
- 图片理解、图片 OCR 和音视频解析。
- 使用 PostgreSQL pgvector 保存向量。
- 在页面允许用户编辑 Milvus 连接参数。
- 本次索引改造之外的完整问答或 Graph-RAG 检索编排。

## 4. 系统架构

```text
React 前端
   │
   ▼
FastAPI API ─────────────── PostgreSQL
   │                           │
   │ 上传原始文件              │ datasets
   ▼                           │ documents
MinIO                          │ document_segments
                               │ dataset_indexes
FastAPI API                    │ indexing_jobs
   │                           │ indexing_job_documents
   │ 提交 job_id               │
   ▼                           │
RabbitMQ ◄──────────── Celery Beat
   │
   ▼
Celery Index Worker
   ├── 从 MinIO 读取原始文件
   ├── 解析支持的文档格式
   ├── 执行普通分段或父子分段
   ├── 高质量：调用 OpenAI-compatible Embeddings API
   ├── 高质量：写入 Milvus
   ├── 经济：生成关键词
   └── 将状态、正文和元数据写回 PostgreSQL
```

RabbitMQ 只承担可靠消息投递。PostgreSQL 是任务状态和业务结果的事实来源。Celery 消息体只包含 `job_id` 或 `job_document_id`，不包含文件、正文、API Key 或完整处理配置。

## 5. 用户流程

### 5.1 创建知识库

创建弹窗只包含名称、描述和权限。创建请求不包含文件、分段参数、模型选择或 Milvus 配置。成功后跳转到 `/datasets/{dataset_id}/documents`。

创建时只写入 `datasets`，不提前创建 Milvus collection。只有高质量索引第一次得到真实向量维度后才创建 collection。

### 5.2 上传文件

文档页面允许批量拖拽和选择文件。API 流式计算 SHA-256，将文件写入 `graph-rag-uploads` bucket，并在 `documents.data_source_info` 中保存：

```json
{
  "storage": "minio",
  "bucket": "graph-rag-uploads",
  "object_key": "datasets/{dataset_id}/documents/{document_id}/source.pdf",
  "original_filename": "source.pdf",
  "content_type": "application/pdf",
  "size": 123456,
  "sha256": "..."
}
```

上传只创建待处理文档，不自动开始索引。

### 5.3 配置和预览

索引方式：

- 高质量：选择后端配置的 Embedding 模型，生成向量并写入 Milvus。
- 经济：不选择模型，不调用 Embedding，不写 Milvus，只生成关键词。

分段方式：

- 普通分段：支持高质量或经济索引。
- 父子分段：只支持高质量索引；选择父子分段时前端自动切换为高质量，后端再次校验。

预览从 MinIO 读取真实文件，调用与正式 Worker 相同的解析器和分段器；预览不写分段、不调用 Embedding、不写 Milvus。

### 5.4 保存并处理

API 在 PostgreSQL 事务中保存配置快照、创建 `indexing_jobs` 和 `indexing_job_documents`。事务提交后向 RabbitMQ 投递任务，立即返回 `202 Accepted` 和 `job_id`。

前端轮询 PostgreSQL 驱动的任务详情接口，并展示总体进度、每个文档阶段、warning、失败原因、重试和取消入口。

## 6. 数据模型

### 6.1 `indexing_jobs`

记录一次业务索引任务，主要字段：

- `id varchar(36)`：主键。
- `dataset_id varchar(36)`：知识库。
- `target_index_id varchar(36)`：构建或使用的索引版本。
- `retry_of_job_id varchar(36)`：可空，指向被重试的原任务。
- `job_type`：`initial_index`、`add_documents`、`reindex_documents`、`reindex_dataset`。
- `scope`：`selected_documents` 或 `entire_dataset`。
- `status`：`pending`、`queued`、`running`、`completed`、`partial_success`、`failed`、`retry_wait`、`cancelled`。
- `current_stage`：下载、解析、分段、Embedding、写索引、激活或清理。
- `indexing_technique`：`high_quality` 或 `economy`。
- `segmentation_mode`：`general` 或 `parent_child`。
- `embedding_model_provider`、`embedding_model`。
- `process_rule jsonb`：任务不可变处理规则快照。
- `retrieval_config jsonb`：任务不可变检索配置快照。
- 文档、分段和进度统计。
- `celery_task_id`、`attempt`、`max_attempts`、`available_at`。
- `heartbeat_at`、`lease_expires_at`、开始/完成/取消时间。
- `error`、`created_by`、审计时间。

需要为 `(status, available_at)` 和运行中任务的 `lease_expires_at` 建立索引。

### 6.2 `indexing_job_documents`

记录任务中每个文档的独立状态：

- `id`、`job_id`、`document_id`。
- `status`、`current_stage`、`progress`。
- `total_segments`、`processed_segments`、`embedded_segments`。
- `attempt`、开始/完成/心跳时间。
- `error_code`、`error`。
- 唯一约束 `(job_id, document_id)`。

一个文档失败不回滚其他新增文档的成功结果。失败文档重试会创建新任务，原任务保留用于审计。

### 6.3 `dataset_indexes`

记录知识库的可切换索引版本：

- `id`、`dataset_id`、`created_by_job_id`。
- `index_type`：`high_quality` 或 `economy`。
- `status`：`building`、`active`、`retired`、`failed`、`deleting`。
- 模型、向量维度、距离度量。
- 向量存储 provider 和 collection name。
- 处理规则、检索配置和 `config_hash`。
- 激活、退役、创建、更新和软删除时间。

PostgreSQL 部分唯一索引保证每个知识库最多一个未删除的 `active` 索引版本。

高质量 collection 命名：

```text
graph_rag_{dataset_id}_{index_id}
```

经济索引的 collection name、维度和向量 provider 均为空。

### 6.4 扩展 `document_segments`

新增：

- `dataset_index_id varchar(36)`。
- `indexing_job_id varchar(36)`。
- `source_metadata jsonb`。
- `content_hash varchar(64)`。

`source_metadata` 保存 PDF 页码、Markdown 标题路径、Excel sheet/表头/行号或 CSV 行号。新增索引支持按 active 索引版本、文档和 completed 状态快速查询，并为 `keywords` 建立 GIN 索引。

父块和子块都存入 `document_segments`。父块 `parent_id = NULL`，子块通过 `parent_id` 关联父块。父子模式只对子块生成向量；向量命中子块后从 PostgreSQL 返回父块正文。

### 6.5 现有表职责

- `datasets`：知识库展示字段、当前索引技术、当前模型、处理规则和检索配置。
- `documents`：MinIO 对象信息、用户可见索引状态、最近错误和估算 token 数。
- `document_segments`：正文、顺序、问题答案、关键词、父子关系、源定位和索引状态。

## 7. 安全索引版本与切换

PostgreSQL 与 Milvus 无法共享事务。完整重建不得先删除旧索引，而应：

1. 保持旧 `dataset_indexes` 为 `active`。
2. 创建新的 `building` 索引版本和新 collection。
3. 解析全部目标文档并生成 staging 分段。
4. 生成并写入全部向量。
5. 校验分段数与向量数。
6. 在 PostgreSQL 短事务内将旧版本设为 `retired`、新版本设为 `active`、新分段设为 `completed`。
7. 延迟清理旧 collection。

完整重建任一文档最终失败时不切换，新版本保持失败状态，旧版本继续提供服务。

相同配置下追加文档使用当前 active collection。单文档重建先写新 segment ID 和向量，再在 PostgreSQL 激活新分段并软删除旧分段，最后幂等删除旧向量。检索结果必须回查 PostgreSQL，只接受 completed 且未软删除的分段。

## 8. 后端配置

### 8.1 Embedding

新增 `EmbeddingSettings` 和模型定义。环境变量示例：

```dotenv
EMBEDDING__PROVIDER=openai_compatible
EMBEDDING__BASE_URL=http://embedding-server:8000/v1
EMBEDDING__API_KEY=<secret>
EMBEDDING__DEFAULT_MODEL=bge-m3
EMBEDDING__MAX_RETRIES=3
EMBEDDING__MODELS=[{"id":"bge-m3","model":"bge-m3","display_name":"BGE-M3","batch_size":32,"max_input_characters":8000}]
```

API Key 只在服务端使用。模型列表接口只返回 ID、显示名称、provider 和默认标记。

Embedding 客户端调用 `{base_url}/embeddings`，发送 `model` 和批量 `input`。客户端需要恢复响应顺序、校验数量、维度和有限数值；对 429、502、503、504 指数退避，对 400、401、403 等不可恢复错误快速失败。第一批真实向量确定维度，之后锁定该索引版本的维度。

### 8.2 MinIO

```dotenv
OBJECT_STORAGE__PROVIDER=minio
OBJECT_STORAGE__ENDPOINT=minio:9000
OBJECT_STORAGE__ACCESS_KEY=<secret>
OBJECT_STORAGE__SECRET_KEY=<secret>
OBJECT_STORAGE__SECURE=false
OBJECT_STORAGE__BUCKET=graph-rag-uploads
```

应用检查并创建专用 bucket，但不操作 Milvus 自己使用的 bucket。对象 key 使用后端 ID，不直接拼接用户文件名。

### 8.3 RabbitMQ/Celery

Compose 新增 `rabbitmq`、`celery-worker`、`celery-beat`。RabbitMQ 使用持久化 queue、持久化消息、独立用户和 virtual host。

Celery 关键配置：

```text
task_ignore_result = true
task_acks_late = true
task_reject_on_worker_lost = true
worker_prefetch_multiplier = 1
```

业务状态全部写 PostgreSQL，不依赖 Celery result backend。

## 9. 文件解析

所有解析器输出统一的 `ParsedDocument` 和 `ParsedBlock`，包含文本、block type、source metadata 和 warnings。预览与正式索引复用同一服务。

### 9.1 TXT

优先 UTF-8/UTF-8 BOM，失败后进行编码探测；置信度过低时失败。统一换行，保留段落边界和源行号，清除 NUL 等无意义控制字符。

### 9.2 Markdown

保留标题层级、列表、代码块、表格和链接可见文本。YAML front matter 进入 metadata，不直接混入正文。代码块优先保持完整，超长时才安全拆分。

### 9.3 PDF

按页读取文本并保存页码，处理常见断词和多余换行。空页产生 warning。加密且不可读、超过页数限制或无文本层的 PDF 失败。本期不包含 OCR。

### 9.4 DOCX

按文档顺序读取段落和表格，依据标题样式保存 heading path，保留列表层级。页眉页脚默认排除，图片不 OCR，只支持 `.docx`。

### 9.5 XLS/XLSX

按 sheet 读取，首个有效数据行默认作为表头。每一行转换为“列名：值”的自描述文本并保留真实行号。隐藏/空 sheet 跳过并产生 warning。公式不执行，优先读缓存值，无缓存时保留公式文本并提示。

### 9.6 CSV

探测编码和常见分隔符，支持引号内换行，首个有效数据行默认作为表头。作为单个逻辑 sheet 处理，保留真实行号。

### 9.7 安全和限制

后端配置文件大小、PDF 页数、表格行列数、单元格长度、ZIP 解压后大小、预览文件大小、预览时间和返回块数。拒绝路径逃逸、压缩炸弹、宏执行和外部链接执行。

## 10. 分段和关键词

### 10.1 普通分段

递归边界优先级：用户分隔符、段落空行、单换行、中英文句末标点、空格、字符硬切。默认最大 1024 字符、重叠 100 字符。UI 明确显示“字符”，不误称 token。

代码块、Markdown 表格和表格行优先视为原子块；超过上限时仍需拆分。不生成空白块，按顺序写入 position，并根据标准化文本和源定位计算 `content_hash`。

### 10.2 父子分段

父块支持段落模式和全文模式。默认父块上限 4096 字符，子块上限 512 字符，子块重叠 50 字符。超长全文父块自动降级拆分并产生 warning。

Excel/XLSX 每个 sheet 或连续行组作为父块，行或小行组作为子块；CSV 整体或连续行组作为父块，行作为子块。子块始终携带列名。

### 10.3 经济索引

经济模式只支持普通分段。对中文、英文、数字标识进行分词、停用词过滤和频率评分，每块保存有限数量关键词到 `keywords`，使用 PostgreSQL GIN 索引。经济模式不调用 Embedding、不创建或写入 Milvus。

## 11. Milvus schema

每个高质量索引版本拥有一个 collection，字段包括：

- `id VARCHAR`：`document_segments.id`，主键。
- `embedding FLOAT_VECTOR`。
- `dataset_id VARCHAR`。
- `document_id VARCHAR`。
- `dataset_index_id VARCHAR`。
- `parent_id VARCHAR`，可空。
- `position INT64`。

正文不复制到 Milvus。默认由后端配置 COSINE + HNSW，建议初始参数 `M=16`、`efConstruction=200`。第一批 Embedding 决定维度后创建 schema、向量索引并加载 collection。父子模式只 upsert 子块。

## 12. API 契约

主要接口：

```text
POST   /api/knowledge_base
GET    /api/knowledge_base/list
GET    /api/knowledge_base/{dataset_id}
DELETE /api/knowledge_base/{dataset_id}

GET    /api/indexing/options

POST   /api/knowledge_base/{dataset_id}/documents
GET    /api/knowledge_base/{dataset_id}/documents
DELETE /api/knowledge_base/{dataset_id}/documents/{document_id}

POST   /api/knowledge_base/{dataset_id}/indexing/preview
POST   /api/knowledge_base/{dataset_id}/indexing/jobs
GET    /api/knowledge_base/{dataset_id}/indexing/jobs
GET    /api/knowledge_base/{dataset_id}/indexing/jobs/{job_id}
POST   /api/knowledge_base/{dataset_id}/indexing/jobs/{job_id}/retry
POST   /api/knowledge_base/{dataset_id}/indexing/jobs/{job_id}/cancel
```

创建任务时，后端根据有效索引和配置 hash 决定是初次索引、追加文档、部分重建还是全量重建。改变模型、维度、索引技术或全局分段配置必须扩展为全知识库重建，并由前端二次确认。

统一错误响应包含稳定 `code`、用户消息、脱敏 detail 和 request ID。

## 13. Celery 任务模型

任务：

```text
dispatch_indexing_job(job_id)
index_document(job_document_id)
finalize_indexing_job(job_id)
cleanup_failed_index(index_id)
cleanup_document_vectors(document_id)
cleanup_dataset_resources(dataset_id)
recover_stale_jobs()
dispatch_pending_jobs()
```

不使用依赖 result backend 的 chord。dispatcher 为每个文档发送任务；每个文档完成后在 PostgreSQL 中检查任务汇总状态，最后一个终态文档通过行锁触发 finalize。

建议队列：

- `indexing`：下载、解析、分段、Embedding、Milvus。
- `maintenance`：恢复、补偿和清理。

文档阶段：claim、download、parse、split、persist staging segments、embed or keywords、write vectors、validate、complete。每阶段更新进度和心跳，并在文档、分段批次、Embedding 批次和 Milvus 批次之间检查取消标记。

## 14. 幂等、恢复和补偿

- RabbitMQ/Celery 是至少一次投递，重复消息是预期情况。
- `(job_id, document_id)` 唯一。
- Worker 使用条件更新和租约领取任务。
- 已完成任务收到重复消息直接成功返回。
- segment ID 使用包含 index、document、parent、position 和 content hash 的稳定 UUIDv5。
- Milvus 使用 upsert。
- active 索引版本由 PostgreSQL 唯一约束保护。
- API 先提交 PostgreSQL 再投递 RabbitMQ；投递失败时任务保留 pending。
- Celery Beat 补发 pending 任务、恢复租约过期任务并清理外部残留资源。
- 重试退避建议为 30 秒、2 分钟、10 分钟。
- 429、常见网关错误、MinIO/Milvus/PostgreSQL 短暂故障可重试。
- 文件损坏、无文本 PDF、401/403、模型不存在、维度不一致和非法配置不可自动重试。
- 清理 collection、向量和 MinIO 对象均幂等；目标不存在视为成功。

## 15. 前端结构

页面：

```text
/datasets
/datasets/{dataset_id}/documents
/datasets/{dataset_id}/process
```

创建弹窗只保留名称、描述和权限。文档页分为上传、配置/预览、处理进度三步。配置页左侧为索引和分段设置，右侧为真实普通分段列表或父子树预览。

高质量显示模型选择，经济隐藏模型选择；父子分段强制高质量。状态页轮询任务详情，展示总体和文档级进度、warning、错误、取消和重试入口。

当前集中在 `App.jsx` 的代码应拆为 API 层、页面、dataset feature 组件和轮询 hook，并通过 URL 恢复页面状态。

## 16. 删除行为

删除文档：软删除文档和分段，标记运行中任务取消，异步删除对应向量，并在保留期后删除 MinIO 对象。

删除知识库：软删除 dataset、documents 和 segments，取消未完成任务，异步清理所有 collection 和对象。API 返回 204，不等待外部资源清理完成。

旧 JSON 文件和 `main.py` 中无效的 `knowledge_bases.json` 引用应在实现中一并删除。

## 17. 数据库迁移

加入 Alembic，不重建现有三张业务表。首个迁移仅创建新增表、外键和索引，并为 `document_segments` 增加字段。迁移前检查现有表和 pgvector 扩展，提供升级和回滚；应用启动时不隐式执行破坏性 DDL。

## 18. 测试与验收

### 18.1 后端

- 配置解析、默认模型和敏感字段脱敏。
- 每种文件格式的正常、warning 和失败样例。
- 普通/父子分段、overlap、超长文本、表格和空内容。
- OpenAI-compatible 正常、乱序、数量/维度错误、429 重试、401 快速失败、超时和非有限向量。
- 真实 PostgreSQL 迁移、约束、任务竞争、最终汇总和关键词 GIN。
- MinIO bucket、流式上传、读取、对象 key 安全、服务异常和删除幂等。
- RabbitMQ/Celery 正常投递、重复消息、acks late、Worker 崩溃、补发、租约恢复、取消和重试。
- Milvus 自动维度、schema、HNSW/COSINE、批量 upsert、父子只写子块、版本切换和清理。

### 18.2 前端

- 创建弹窗不存在 Milvus 字段。
- 创建后跳转文档页。
- 批量上传和部分失败。
- 高质量/经济切换。
- 父子模式强制高质量。
- 普通和父子真实预览。
- 任务创建、轮询、取消、失败重试和终态停止。
- 页面刷新后路由恢复。
- 生产构建通过。

### 18.3 端到端完成条件

必须使用真实 PostgreSQL、RabbitMQ、MinIO、Milvus 和 mock OpenAI-compatible 服务跑通：

1. 创建空知识库。
2. 上传所有支持格式。
3. 真实预览普通和父子块。
4. 高质量模式生成向量并写入 Milvus。
5. 父子模式仅子块有向量。
6. 经济模式只生成关键词。
7. PostgreSQL 中可以查询任务和文档进度。
8. 重复消息不产生重复分段或向量。
9. Worker 重启后任务恢复。
10. 全量重建失败时旧索引继续 active。
11. 删除行为产生并完成幂等清理。
12. 不再依赖 `knowledge_bases.json`。

## 19. 实施约束

- 采用测试驱动开发：先写失败测试，再实现最小功能。
- 保持 API、领域服务、repository、parser、splitter、Embedding、storage 和 vector store 之间边界清晰。
- 不把同步 MinIO/Milvus SDK 的阻塞调用直接放在 FastAPI event loop 中。
- 不记录密钥、完整正文或完整向量。
- 不覆盖工作区中与本功能无关的用户改动。
- 每个实施阶段都运行相应的后端测试、前端测试和构建验证。
