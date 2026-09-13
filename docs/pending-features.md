# Graph-RAG 待实现功能清单

最后更新：2026-09-09

文档状态：待实现功能基线

## 1. 文档目的

本文记录当前 Graph-RAG 项目尚未完成的产品与工程能力，并作为后续需求拆分、实施排序和验收的统一入口。

本文只描述待实现事项，不表示相关功能已经上线。实际状态以代码、迁移和自动化测试结果为准。

状态标记：

- `[ ]`：尚未实现或尚未形成可用闭环。
- `[~]`：已有模型、适配器或执行原语，但尚未接入完整业务流程。
- `[x]`：已实现并经过相应验证；完成后才可使用此标记。
- `P0`：形成最小可用知识库必须完成。
- `P1`：多人使用、知识治理和稳定运行所需。
- `P2`：增强能力或规模化、生产化能力。

## 2. 当前能力边界

当前项目已经具备以下基础能力，不应再将它们描述为“完全未实现”：

- 知识库创建、列表、详情、统计和软删除。
- 文档批量上传、格式校验、大小限制、压缩包安全检查和重复文件识别。
- 使用 MinIO 保存上传的原始文件。
- TXT、Markdown、PDF、DOCX、XLS、XLSX、CSV 解析。
- 普通分段和父子分段。
- 使用正式 parser 和 segmenter 生成真实分段预览。
- OpenAI-compatible Embedding 客户端。
- 经济索引关键词提取。
- PostgreSQL 分段暂存和激活相关原语。
- Milvus collection 创建、向量写入、计数和删除相关原语。
- 单文档索引执行引擎。
- `dataset_indexes`、`indexing_jobs`、`indexing_job_documents` 表结构。
- RabbitMQ/Celery broker 和队列基础配置。
- 基础 React 知识库和文档管理控制台。

但当前能力主要停留在“知识库管理、文件上传和索引底层原语”。完整业务链路仍未打通：

```text
上传与配置
  → 创建 Dataset / Document / ProcessRule
  → 创建持久化索引任务
  → Worker 解析、分段、Embedding 或关键词提取
  → 激活索引版本
  → 检索、重排和生成回答
```

## 3. P0：上传后统一创建 Dataset、Document 和 ProcessRule

### 3.1 当前状态

当前前端流程是：

```text
先创建空 Dataset
  → 进入文档页面
  → 上传文件到 MinIO
  → 创建 Document，状态为 waiting
```

当前存在以下差异：

- `Dataset` 在文件上传之前通过独立接口创建。
- 文件上传成功后会创建 `Document`，但不会继续创建和投递索引任务。
- `Document.dataset_process_rule_id` 字段已经存在，但上传时未赋值。
- 当前没有独立的 `ProcessRule` ORM 实体、repository 和迁移表。
- `Dataset.partial_user_config` 中只有 `{"process_rule": null}` 占位。
- `DatasetIndexRecord.process_rule` 和 `IndexingJobRecord.process_rule` 是不可变 JSON 配置快照，不等同于可管理的 `ProcessRule` 领域实体。

### 3.2 目标流程

- [ ] 提供一个统一的知识库初始化/摄取流程，接收知识库元数据、文件和处理规则。
- [ ] 后端在请求开始时生成 `dataset_id` 和各 `document_id`，供 MinIO object key 和数据库记录共同使用。
- [ ] 完成所有请求级参数、文件格式、MIME、magic、大小和压缩包安全校验后，再执行持久化。
- [ ] 将通过校验的原始文件上传至 MinIO。
- [ ] 文件上传成功后，在 PostgreSQL 中创建一个 `Dataset`。
- [ ] 为该 Dataset 创建一个有效的 `ProcessRule`。
- [ ] 为每个已接受文件创建一个 `Document`，并关联 `dataset_id` 与 `dataset_process_rule_id`。
- [ ] Document 初始状态为 `waiting`，并保存完整的 MinIO 对象引用、文件摘要和原始文件名。
- [ ] PostgreSQL 事务提交成功后，创建持久化 `IndexingJob` 和 `IndexingJobDocument`，再投递 RabbitMQ。
- [ ] API 响应返回已创建的 Dataset、ProcessRule、Document 列表、被拒绝文件和索引任务标识。

推荐目标顺序：

```text
接收知识库配置、处理规则和文件
  → 预生成 Dataset/Document ID
  → 校验所有输入
  → 上传已接受文件至 MinIO
  → PostgreSQL 单事务创建 Dataset、ProcessRule、Document
  → 提交事务
  → 创建/投递索引任务
  → 返回创建结果
```

### 3.3 ProcessRule 领域模型

- [ ] 新增 `ProcessRuleRecord` ORM 模型及 Alembic 迁移。
- [ ] ProcessRule 至少关联 `dataset_id`，并记录分段模式、分隔符、最大块长度、重叠长度等配置。
- [ ] 父子分段规则记录父块模式、父块长度、子块长度和子块重叠。
- [ ] 规则包含版本或不可变快照语义，防止修改规则后影响正在运行的任务。
- [ ] Dataset 明确记录当前生效的 ProcessRule。
- [ ] Document 明确记录创建或索引时使用的 ProcessRule。
- [ ] IndexingJob 和 DatasetIndex 继续保存完整 JSON 快照，确保历史任务可复现。
- [ ] 增加 ProcessRule 创建、查询、修改和校验服务；是否公开独立 REST API由产品流程决定。

### 3.4 一致性与失败补偿

- [ ] Dataset、ProcessRule 和同批 Document 的数据库写入使用明确的事务边界。
- [ ] MinIO 上传成功但数据库创建失败时，删除本次请求已上传的精确 object key。
- [ ] 补偿删除失败时记录可重试的清理任务，不覆盖原始数据库异常。
- [ ] 数据库提交成功但 RabbitMQ 暂时不可用时，保留 `pending` 任务，由 Beat 或 outbox 后续补投。
- [ ] 重复提交使用幂等键或文件 Hash，不重复创建 Dataset、ProcessRule、Document 或索引任务。
- [ ] 批量文件部分不合法时，响应中明确区分已接受文件和被拒绝文件。
- [ ] 明确批量请求的业务语义：至少存在一个有效文件才允许创建 Dataset；所有文件都无效时不得留下空 Dataset 或 ProcessRule。

### 3.5 验收标准

- [ ] 一次有效请求后，PostgreSQL 中恰好存在一个 Dataset、一个生效 ProcessRule 和与有效文件数相等的 Document。
- [ ] 每个 Document 均关联正确的 Dataset 和 ProcessRule。
- [ ] 每个 Document 的 MinIO object key、大小、MIME、SHA-256 和原始文件名与实际对象一致。
- [ ] 所有文件都无效时不创建任何业务记录和 MinIO 对象。
- [ ] MinIO 上传中途失败时，不创建不完整的 Dataset/ProcessRule/Document 组合。
- [ ] PostgreSQL 提交失败时，本次已上传对象能够立即补偿删除或进入持久清理队列。
- [ ] 客户端重试同一幂等请求时不会产生重复记录或重复对象。
- [ ] 数据库提交后即使 RabbitMQ 不可用，索引任务仍可在消息系统恢复后继续执行。

## 4. P0：修复现有接口契约回归

- [ ] 统一文档上传的规范 API 路径。
- [ ] 在兼容期同时支持以下路径，或提供明确迁移策略：

  ```text
  POST /api/knowledge_base/{dataset_id}/documents
  POST /api/knowledge_base/{dataset_id}/documents/upload
  ```

- [ ] 修复当前因 `/documents` 返回 `405 Method Not Allowed` 导致的 6 个后端测试失败。
- [ ] 更新前端、OpenAPI、README 和架构文档，保证只宣称实际可用的路径。
- [ ] 为规范路径和兼容路径增加相同的鉴权、错误映射和批量上传测试。

验收标准：后端快速回归中现有上传契约测试全部通过，不再出现路径不一致导致的 405。

## 5. P0：异步索引任务闭环

### 5.1 任务 API

- [ ] `POST /api/knowledge_base/{dataset_id}/indexing/jobs`：创建索引任务。
- [ ] `GET /api/knowledge_base/{dataset_id}/indexing/jobs`：分页查询任务列表。
- [ ] `GET /api/knowledge_base/{dataset_id}/indexing/jobs/{job_id}`：查询任务与文档级进度。
- [ ] `POST /api/knowledge_base/{dataset_id}/indexing/jobs/{job_id}/retry`：重试失败任务或失败文档。
- [ ] `POST /api/knowledge_base/{dataset_id}/indexing/jobs/{job_id}/cancel`：请求取消任务。
- [ ] API 只创建持久状态和投递任务，不在请求线程执行完整索引。

### 5.2 Celery 编排

- [ ] 注册真实的 Celery 索引任务，而不只是配置 broker 和队列。
- [ ] Worker 根据任务 ID从 PostgreSQL 加载不可变任务快照。
- [ ] Worker 串接现有 MinIO、parser、segmenter、Embedding、关键词、segment repository 和 Milvus 原语。
- [ ] 持久化任务阶段、百分比、文档计数、分段计数、warnings 和脱敏错误。
- [ ] 实现 Worker 心跳和任务租约。
- [ ] 实现至少一次投递下的幂等执行。
- [ ] 实现失败重试、指数退避、最大尝试次数和不可重试错误终止。
- [ ] 实现用户取消检查和安全取消点。
- [ ] 实现 Celery Beat 对 pending 任务补投、过期租约恢复和延迟清理。

### 5.3 索引版本

- [ ] 创建 building 状态的 DatasetIndex。
- [ ] 高质量索引根据第一批真实 Embedding 锁定向量维度并创建 Milvus collection。
- [ ] 经济索引只生成 PostgreSQL 关键词，不调用 Embedding、不创建 Milvus collection。
- [ ] 文档索引成功后激活新分段并使旧分段失效。
- [ ] 全量重建全部校验成功后原子激活新 DatasetIndex。
- [ ] 全量重建失败时保留旧 active DatasetIndex 可查询。
- [ ] 异步清理 retired/failed 索引对应的旧向量、分段和 collection。

### 5.4 验收标准

- [ ] 上传完成后 Document 能从 `waiting` 自动推进到 `completed` 或稳定的失败状态。
- [ ] Worker 在解析、Embedding 或 Milvus 写入期间退出后，任务能够恢复。
- [ ] RabbitMQ 重复投递不会生成重复分段或重复业务记录。
- [ ] 失败文档可单独重试，成功文档无需重复处理。
- [ ] 用户取消后任务不再进入后续昂贵阶段。
- [ ] 重建失败不会中断旧索引的检索服务。

## 6. P0：索引配置与前端流程

- [ ] 将知识库创建、文件选择、索引配置、分段预览和任务进度组织成清晰的前端流程。
- [ ] 接入已有 `/api/indexing/options`，显示后端允许的文件格式、模型和限制。
- [ ] 支持高质量索引和经济索引选择。
- [ ] 支持普通分段和父子分段参数编辑。
- [ ] 接入真实 `/indexing/preview` API，展示内容、源定位和解析警告。
- [ ] 允许用户确认 ProcessRule 后启动索引。
- [ ] 展示任务总进度、文档级进度、当前阶段、warnings 和失败原因。
- [ ] 支持取消、重试和失败文档单独重试。
- [ ] 轮询时避免过期请求覆盖较新的任务状态。

验收标准：用户无需调用 API，即可在控制台完成“选择文件和配置 → 创建业务记录 → 预览 → 建立索引 → 查看结果”的完整流程。

## 7. P0：检索能力

### 7.1 检索 API

- [ ] 提供 `POST /api/knowledge_base/{dataset_id}/retrieve`。
- [ ] 只查询 active DatasetIndex。
- [ ] 排除已删除、已归档、已禁用、未完成和旧版本分段。
- [ ] 返回分段正文、分数、命中方式、文档 ID、文件名和源定位。
- [ ] 返回 PDF 页码、标题路径、sheet、行号等可用元数据。
- [ ] 父子模式对子块召回并返回父块上下文。
- [ ] 支持 Top-K、相似度阈值和元数据过滤。

### 7.2 向量检索

- [ ] 为 `VectorStoreProvider` 增加 search 接口。
- [ ] 实现 Milvus ANN search 和 dataset/index 范围过滤。
- [ ] 使用查询 Embedding 与索引版本锁定的模型和维度。
- [ ] 对命中的 segment ID回查 PostgreSQL，以确认可见性并取得正文。

### 7.3 关键词与混合检索

- [ ] 使用 PostgreSQL 关键词数组和 GIN 索引实现经济模式检索。
- [ ] 支持向量召回与关键词召回融合。
- [ ] 实现结果去重、分数归一化和相邻块合并。
- [ ] 支持可配置的 rerank，并在未配置时安全降级。
- [ ] 记录 rerank 前后排名，便于调试和评测。

验收标准：已完成索引的知识库能够稳定返回带来源定位的 Top-K 证据；禁用、删除和旧版本内容不会被召回。

## 8. P0：知识库问答与引用

- [ ] 接入 OpenAI-compatible Chat/Responses 生成模型。
- [ ] 提供问答 API，并支持 SSE 或等价的流式输出。
- [ ] 实现查询改写、检索、上下文组装、生成和引用映射。
- [ ] 使用 token budget 限制上下文大小。
- [ ] 回答附带文档、页码、标题、sheet 或行号等引用。
- [ ] 前端支持点击引用查看对应原文和分段。
- [ ] 无足够证据时明确拒答或说明知识库中没有相关信息。
- [ ] 防止文档中的提示注入覆盖系统规则。
- [ ] 对模型超时、限流、鉴权失败和无效响应提供稳定错误码。

验收标准：用户提问后能够获得流式、基于知识库证据的回答，并可从答案引用定位到原始文档。

## 9. P1：文档治理

- [ ] 查看文档详情、大小、类型、Hash、上传人和更新时间。
- [ ] 下载原始文件。
- [ ] 删除文档，并异步清理 MinIO 对象、PostgreSQL 分段、Milvus 向量和 Neo4j 图数据。
- [ ] 重命名文档。
- [ ] 启用和禁用文档。
- [ ] 归档和恢复文档。
- [ ] 单文档重新索引。
- [ ] 批量删除、批量重试和批量重新索引。
- [ ] 查看解析 warnings、索引错误、重试次数和任务历史。
- [ ] 查看、编辑、禁用或删除单个分段。
- [ ] 支持人工补充分段关键词、问题和答案。
- [ ] 支持不可变文档 revision 和历史版本查看。

## 10. P1：知识库设置与治理

- [ ] 修改知识库名称、描述和权限。
- [ ] 设置标签和分类。
- [ ] 修改索引技术、Embedding 模型、ProcessRule 和检索配置。
- [ ] 配置 Top-K、相似度阈值和 rerank。
- [ ] 配置变化后计算影响范围，并触发必要的文档级或全量重建。
- [ ] 克隆知识库。
- [ ] 导入、导出和迁移知识库。
- [ ] 展示知识库文档数、分段数、存储量、调用量和索引历史。

## 11. P1：身份、权限和多租户

当前 `created_by` 和上传操作者仍使用硬编码的 `current-user`。现有“私有/团队”主要是数据字段和筛选条件，还不是服务端安全边界。

- [ ] 登录、登出和身份认证。
- [ ] JWT、Session 或统一身份系统集成。
- [ ] 用户、团队和工作空间/租户模型。
- [ ] 所有查询和写操作执行服务端租户范围校验。
- [ ] 知识库所有者、管理员、编辑者和查看者角色。
- [ ] 私有、团队和公开权限的真实授权规则。
- [ ] API Key 和服务账号。
- [ ] 权限变更和敏感操作审计。
- [ ] 防止通过猜测 dataset/document/job ID跨库访问。

验收标准：不同租户和角色只能读取或修改其被授权的知识库及相关文档、任务、分段和问答记录。

## 12. P1：外部数据源与增量同步

- [ ] 实现 `DocumentRevision`，记录不可变原始内容版本。
- [ ] 使用稳定源 ID、内容 Hash、处理规则 Hash 和模型配置 Hash 判断变化范围。
- [ ] 更新时先构建并验证新 revision，再切换 active revision。
- [ ] 新 revision 失败时保留旧 revision 可查询。
- [ ] 实现通用 Connector、SourceDocument、SourceEvent 和 transactional outbox。
- [ ] 支持手动同步、增量轮询、完整对账和签名 Webhook。
- [ ] 支持凭据引用和轮换，凭据不得进入日志、消息或 API 响应。
- [ ] 处理重复事件、乱序事件、分页中断和删除确认。
- [ ] 提供同步历史、游标、错误详情和手动重试。
- [ ] 按需求实现网页、对象存储、数据库、Git、Confluence、Notion、飞书等连接器。

## 13. P1：真正的 Graph-RAG

当前 Neo4j Compose 和静态 CSV/Cypher 初始化脚本已经存在，但图数据准备、图检索、混合检索、查询路由和生成编排模块仍是占位。

- [ ] 从 active document segments 抽取实体、关系、事件和属性。
- [ ] 实体归一化、别名处理、消歧和重复合并。
- [ ] 图节点和关系可追溯到 dataset、document、revision、segment 和 index version。
- [ ] 文档重建、禁用和删除时同步使旧图数据失效。
- [ ] 实现 Neo4j 邻居、路径、社区或子图检索。
- [ ] 实现查询意图路由，选择向量、关键词、图检索或组合检索。
- [ ] 合并、去重和重排文本证据与图证据。
- [ ] 将图证据以可引用形式交给生成模型。
- [ ] 建立普通 RAG 与 Graph-RAG 的质量、延迟和成本对比评测。

## 14. P2：可观测性与运维

- [ ] 提供 `/health/live` 和 `/health/ready`。
- [ ] 就绪检查覆盖 PostgreSQL、MinIO、RabbitMQ、Milvus、Neo4j 和模型服务。
- [ ] 统一结构化日志和脱敏规则。
- [ ] Request ID贯穿 HTTP 请求、数据库任务、RabbitMQ 消息和 Worker 日志。
- [ ] Prometheus-compatible 指标。
- [ ] OpenTelemetry 链路追踪。
- [ ] 索引成功率、失败率、重试率、队列积压和阶段耗时指标。
- [ ] 检索延迟、召回数量、rerank 延迟和空结果率指标。
- [ ] LLM 首字延迟、总延迟、token 和费用指标。
- [ ] 告警规则和运维看板。
- [ ] 管理员审计日志和清理积压视图。

## 15. P2：部署、备份与安全

- [ ] 后端 Dockerfile 和前端生产构建镜像。
- [ ] Docker Compose 增加 API、Celery Worker、Celery Beat 和前端/反向代理服务。
- [ ] 安全的自动迁移或显式迁移发布步骤。
- [ ] 优雅关闭和 Worker 水平扩容。
- [ ] TLS、可信代理、CORS 和安全响应头配置。
- [ ] API 限流、并发索引限制和用户/租户配额。
- [ ] 上传文件病毒扫描。
- [ ] 敏感信息检测和可配置脱敏。
- [ ] PostgreSQL、MinIO、Milvus 和 Neo4j 的备份恢复方案。
- [ ] 数据保留、导出和彻底删除策略。
- [ ] CI 执行后端、前端、迁移、集成和 E2E 测试。
- [ ] 根据部署目标提供 Kubernetes/Helm 或等价生产部署清单。

当前 README 声称 FastAPI 可以托管前端构建产物，但 `main.py` 尚未挂载静态文件；应选择并实现以下一种方式：

- [ ] 由 FastAPI 正确挂载前端静态文件及 SPA fallback；或
- [ ] 由独立 Web Server/反向代理托管前端，并修正文档。

## 16. P2：内容处理扩展

- [ ] 扫描版 PDF 和图片 OCR。
- [ ] PPT/PPTX。
- [ ] HTML、EPUB、JSON 等格式。
- [ ] 音视频转录。
- [ ] PDF 表格和版面结构提取。
- [ ] 图片、图表和公式理解。
- [ ] 密码保护文档的受控处理。
- [ ] 自定义 parser 插件机制。

这些能力不阻塞第一版纯文本知识库闭环，应根据真实数据来源决定优先级。

## 17. P2：检索和回答质量评测

- [ ] 建立标准问题、答案和来源引用数据集。
- [ ] 计算 Recall@K、Precision@K、MRR 和 nDCG。
- [ ] 比较向量、关键词、混合和图检索。
- [ ] 对分段大小、重叠、Top-K、阈值和 rerank 做参数实验。
- [ ] 评估答案正确性、忠实度、引用准确率和拒答率。
- [ ] 建立延迟、吞吐量和成本基准。
- [ ] 索引或检索变更进入主分支前运行离线回归。
- [ ] 将用户点赞、点踩和纠错反馈沉淀为评测样本。
- [ ] 增加 PostgreSQL → MinIO → RabbitMQ → Worker → Milvus → Retrieve → Answer 全链路 E2E 测试。

## 18. 当前验证基线

2026-09-09 本地只读盘点期间获得以下基线：

- 前端 Playwright：`11 passed`。
- 前端 `npm run build`：通过。
- 后端快速回归：`568 passed, 6 failed`。
- 6 个后端失败均集中在文档上传兼容路径：实现只注册 `/documents/upload`，测试仍要求 `/documents` 可用。
- 未在本次盘点中运行依赖真实 PostgreSQL、MinIO、RabbitMQ、Milvus 或 Neo4j 的完整基础设施 E2E。

后端测试需要从项目根目录正确设置包导入路径，例如：

```bash
PYTHONPATH=. .venv/bin/pytest tests/unit tests/api tests/test_api_routes.py -q
```

前端验证命令：

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

## 19. 推荐实施顺序

1. 修复文档上传路径契约回归。
2. 实现上传后统一创建 Dataset、Document、ProcessRule 的业务流程和补偿机制。
3. 打通持久化索引任务、Celery Worker、进度、重试、取消和版本激活。
4. 完成前端索引配置、真实预览和任务进度流程。
5. 实现 Milvus 向量检索、PostgreSQL 关键词检索和混合重排。
6. 实现带来源引用的流式知识库问答。
7. 补齐文档治理、知识库设置、身份权限和多租户隔离。
8. 补齐健康检查、指标、日志、部署、备份和全链路 E2E。
9. 实现文档 revision、外部数据源同步和对账。
10. 在普通 RAG 稳定后实现实体抽取、Neo4j 图检索和 Graph-RAG 融合。

## 20. 最小可用版本完成定义

以下条件全部满足后，项目才可以称为“最小可用知识库”：

- [ ] 用户能够提交知识库信息、处理规则和至少一个文件。
- [ ] 系统能够可靠创建 Dataset、ProcessRule 和 Document。
- [ ] 系统能够异步完成解析、分段和高质量或经济索引。
- [ ] 索引过程可查询、可恢复、可重试、可取消。
- [ ] 用户能够检索到正确且仍然有效的文档分段。
- [ ] 用户能够获得带可验证来源引用的回答。
- [ ] 文档删除、禁用或索引重建后，旧内容不会继续被召回。
- [ ] API 具有真实身份和权限校验，不依赖硬编码用户。
- [ ] 核心流程具有自动化回归和真实基础设施 E2E 验证。

