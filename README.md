# Graph-RAG

Graph-RAG 是一个基于 FastAPI 和 React 的知识库管理与检索项目。项目正在从知识库管理原型演进为完整的异步索引系统：PostgreSQL 保存业务元数据和正文，MinIO 保存原始文件，RabbitMQ/Celery 执行持久化索引任务，OpenAI-compatible API 生成 Embedding，Milvus 保存高质量向量，Neo4j 可选保存实体关系图。

## 项目架构

完整架构、组件职责、数据模型、索引流程、可靠性设计和当前实施状态见：

- [项目架构设计](docs/architecture.md)
- [文件上传、解析、索引与检索整体链路](docs/document-processing-flow.md)
- [Dify 风格知识库创建与完整索引设计](docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md)
- [完整索引实施路线图](docs/superpowers/plans/2026-08-31-dataset-indexing-roadmap.md)

核心数据边界：

- PostgreSQL：`datasets`、`documents`、`document_segments`、索引版本和任务状态。
- MinIO：原始上传文件；目标架构使用独立的 `graph-rag-uploads` bucket。
- RabbitMQ：Celery broker，只投递任务标识，不保存业务事实。
- OpenAI-compatible API：高质量索引的 Embedding 生成。
- Milvus：高质量索引向量，向量 ID 与 PostgreSQL segment ID 对齐。
- Neo4j：可选的实体关系图；启用后从分段抽取实体、属性和关系，支持图检索。

> 当前代码处于分阶段演进状态。已实现能力和目标能力的区别以[架构状态说明](docs/architecture.md#2-架构状态说明)为准。

## 启动

安装后端依赖：

```bash
python -m pip install -e .
```

安装前端依赖：

```bash
cd frontend
npm install
```

开发配置读取项目根目录 `.env`，兼容旧的 `rag_modules/config/.env`；系统环境变量优先于两者，根目录文件优先于旧文件。文件上传需要配置 `OBJECT_STORAGE__ACCESS_KEY` 和 `OBJECT_STORAGE__SECRET_KEY`，仅 MinIO 健康检查成功并不能确认上传权限。修改 `.env` 后需重启后端以重新加载配置。

升级已有 PostgreSQL 业务库的索引表结构后再启动新版 API：

```bash
alembic upgrade head
```

该迁移要求已有 `datasets`、`documents`、`document_segments` 表及 `vector` 扩展，不是空数据库初始化脚本。知识库状态和统计查询依赖迁移新增的索引版本、任务表。

检索的“启用重排”通过独立的 Jina/Cohere 兼容 `/rerank` 服务执行（例如 [vLLM 重排接口](https://docs.vllm.ai/en/latest/models/pooling_models/scoring/)）。在 `.env` 设置 `RERANK__BASE_URL`（例如 `http://localhost:8002/v1`，不含 `/rerank`）、`RERANK__MODEL`、可选的 `RERANK__API_KEY` 和 `RERANK__REQUEST_TIMEOUT`。模型名称需与重排服务实际部署名称一致；Embedding 服务本身不一定提供重排接口。普通与流式检索共用此依赖，只有启用重排且有召回候选时才请求模型；超时或返回无效结果时保留原召回顺序，并记录 `rerank_failed` 日志。Docker 访问宿主机服务时，应将地址中的 `localhost` 改为 `host.docker.internal`。

文档软删除后，Worker 会清理 MinIO 原文件、各索引版本的 Milvus 向量、Neo4j 来源分段与关系及不再被其他文档引用的实体，并软删除 PostgreSQL 分段。升级迁移 `20261116_01` 后，`documents.cleanup_completed_at` 仅在各项清理成功后填写；旧版已删除的文档也会自动进入补偿扫描。Celery Beat 每 60 秒按批次补投未完成的清理任务，Broker 或单个存储故障不会丢失清理意图，也不会阻止其他存储的清理。运行中的索引任务会在检查点观察文档删除，清理任务等待运行状态结束后执行。即使租约恢复后旧 Worker 的远程写入才完成，Worker 退出时也会重新打开该文档的清理状态；这次补偿写入若遇到数据库故障，将通过 Celery 持续退避重试，覆盖重投时文档已删除、无需再次构建索引的情况。请保持消费 `maintenance` 队列的 Worker 和 Beat 运行。

清理历史 Neo4j 数据使用 `GRAPH__URI`、`GRAPH__USERNAME`、`GRAPH__PASSWORD` 和 `GRAPH__DATABASE`，即使已将 `GRAPH__ENABLED` 关闭也会尝试清理；关闭新索引的图谱能力不会把历史图谱直接标记为已清理。Neo4j 不可用或凭据缺失时，任务保留待补偿状态并记录 `document_cleanup_pending`，需要恢复连接后才能完成清理。

本次控制台修复的原因、改动清单、状态规则及验证结果见 [2026-09-05 复盘记录](docs/superpowers/retrospectives/2026-09-05-console-review-fixes.md)。

重排未接入、文档跨存储清理与补偿、人工修订在重建和迁移中丢失等问题的根因、修复及验证边界见 [2026-11-16 问题记录](docs/superpowers/retrospectives/2026-11-16-rerank-cleanup-revision-fixes.md)。

开发模式需要分别启动后端和前端：

```bash
uvicorn main:app --reload
```

```bash
cd frontend
npm run dev
```

访问 `http://127.0.0.1:5173`。

## 控制台建立索引

在控制台点击“新建知识库”，填写名称并创建。新知识库打开后，上传文件会自动进入索引配置；先预览分段，再确认启动索引。页面随后显示任务总进度、文档级阶段及错误。离开后重新打开知识库，可继续查看最近的索引任务；有失败文档时可在进度面板重试。

上传和索引是两个明确的步骤：上传成功后若关闭配置面板，文档仍保留在知识库中，可通过“配置索引”继续。索引任务运行需要 PostgreSQL、MinIO、RabbitMQ、Celery Worker 与对应模型或 Milvus 服务可用。

已有有效索引的知识库追加文件时，配置面板会沿用已保存的索引方式、Embedding 模型和分段规则；确认后只处理本次选择的文档，并保留其他文档的检索能力。索引参数与有效索引不一致，或前一个索引任务尚未发布完成时，接口返回 `409`，避免覆盖原有索引。调整全库索引配置请使用知识库设置及全库重建流程。

删除知识库后，检索、原文件下载、分段编辑和索引任务等相关接口返回 `404`，历史上仅标记知识库删除、尚未清理其文档的记录也适用。本次修复不会自动恢复此前因追加索引而被错误退役的分段；受影响的知识库可在确认原文件仍可用后执行全库重建。

上述追加索引、删除后访问和长文档处理问题的原因、解决方案、验证结果与限制，统一记录在[索引与知识库生命周期修复记录](docs/superpowers/retrospectives/2026-11-10-indexing-lifecycle-fixes.md)。

高质量索引可选择父子分段，并设置“按段落”或“全文”父块模式、父块长度、子块长度和子块重叠。预览会把子块列在对应父块下；索引时只为子块生成向量。检索命中子块后展示父块上下文，同一父块的多个命中只占一个结果名额。经济索引只支持普通分段。

长文档索引默认允许 4 个 Embedding 请求和 4 个图谱抽取请求并发，通过 `EMBEDDING__MAX_CONCURRENCY`、`GRAPH__MAX_CONCURRENCY` 调整；设为 `1` 可恢复串行。写入向量、图数据和 PostgreSQL 状态仍由单个消费流程协调，不并发共用数据库 session。模型服务等待期间每 10 秒检查取消并续租。

索引任务默认使用 900 秒时间片：到期后保留已完成向量和图谱检查点，重新入队续跑，不消耗失败重试次数；需要现有 Celery Beat 补投器运行。图谱检查点绑定分段正文、模型和抽取配置，成功的空抽取也会记录；配置变化后不会复用不兼容结果。`INDEXING__TASK_SLICE_SECONDS`、`INDEXING__TASK_SOFT_TIME_LIMIT_SECONDS`、`INDEXING__TASK_TIME_LIMIT_SECONDS` 可配置，时间片与心跳间隔之和必须小于软时限，软时限必须小于硬时限。进度在续跑时保留。

本地 TEI 配置允许最多 64 个输入并发、32 个请求参与批处理，应用每批默认 16 段，以避免原先容量为 4 时的大批量请求立即返回 429。CPU 模型吞吐可能受计算能力限制；提高并发或批量不保证提速，应使用真实文档测量。真实长文档验收用例为 `tests/integration/test_long_document_indexing.py`，通过 `RUN_LONG_INDEXING=1`、`PERF_SOURCE_PDF` 显式启用；该用例使用独立 PostgreSQL schema，并清理自己创建的对象、向量和图数据。

## 知识库证据检索

在知识库文档页打开“检索证据”，输入问题后，后端从当前有效索引中检索可见分段，并按已保存的检索参数召回证据。接口 `POST /api/knowledge_base/{dataset_id}/retrieve/stream` 通过 SSE 逐条返回命中分段。页面直接显示原文、文档名称和源定位；点击来源编号可跳到对应分段卡片，也可下载原文件。没有命中时显示空结果，关闭页面或点击“停止检索”会取消浏览器请求。证据检索不需要 Chat 模型服务。

生产模式可以先构建前端，再由 FastAPI 托管静态文件：

```bash
cd frontend
npm run build
cd ..
uvicorn main:app --reload
```

访问 `http://127.0.0.1:8000`。

FastAPI 启动时会检测项目内的 `frontend/dist/index.html`，存在时托管首页及构建资源。若先启动 API 再构建前端，需要重启 API；未构建前端时，API 仍可独立启动。当前 Docker 应用镜像只包含后端，前端可按开发方式单独运行。

功能测试、问题现象、原因、修复及复验结果见 [2026-10-27 项目功能测试记录](docs/2026-10-27-functional-test.md)。

本地 Docker 的真实 Embedding、高质量/父子索引、混合检索、CPU OCR 与 Neo4j 抽取/图检索已补验，复启命令、模型配置和修复记录见 [实际环境能力验收](docs/2026-10-27-real-environment-test.md)。已有 BGE 权重和原生 Ollama 模型时，可使用 `docker-compose.local-models.yml` 与主 Compose 文件合并启动。

## Neo4j 实体关系图

设置 `.env` 中的 `GRAPH__ENABLED=true`、`GRAPH__URI`、`GRAPH__USERNAME`、`GRAPH__PASSWORD` 和 `GRAPH__EXTRACTION_MODEL` 后重启 API 与索引 Worker。`GRAPH__EXTRACTION_BASE_URL` 和 `GRAPH__EXTRACTION_API_KEY` 指向兼容 `/chat/completions` 且支持 JSON 响应格式的模型服务。Neo4j 已在 `docker-compose.yml` 中定义；容器内 Worker 使用 `bolt://neo4j:7687`，本机运行使用 `bolt://localhost:7687`。

启用后，文档索引会按分段抽取实体、属性和关系，并以知识库索引版本与来源分段 ID 写入 Neo4j。抽取提示参考 [腾讯 Youtu-GraphRAG 的 Schema 引导抽取](https://github.com/TencentCloudADP/youtu-graphrag/blob/main/README-CN.md)：优先使用预设实体和关系类型，校验实体确实出现在原文，并且关系端点存在。已有文档需要重建索引才能生成图。知识库设置中的“图检索 (Neo4j)”按问题中出现的实体名称召回其邻接实体所在分段，结果仍经过 PostgreSQL 中的文档可见性和有效索引过滤。该模式适合明确提到实体的问题；当前未实现社区摘要和复杂问题分解。

关闭 `GRAPH__ENABLED` 后不连接 Neo4j，也不会调用抽取模型；选择图检索时后端会返回配置错误。PostgreSQL 继续保存业务元数据和正文，Milvus 继续保存高质量索引向量。

## Milvus

当前后端已有 Milvus 连接和基础 collection provision adapter。现有原型在创建知识库时会提前 provision collection；目标架构会把它调整为：创建空知识库时不连接 Milvus，只有高质量索引取得第一批真实 Embedding 维度后，才由后端按索引版本创建 collection。

Milvus 连接信息只属于后端配置，不应出现在创建知识库页面。当前配置通过嵌套环境变量或 `config_{APP_ENV}.yaml` 读取，例如：

```bash
export VECTOR_STORE__PROVIDER="milvus"
export VECTOR_STORE__URI="http://localhost:19530"
export VECTOR_STORE__TOKEN=""
export VECTOR_STORE__USER=""
export VECTOR_STORE__PASSWORD=""
export VECTOR_STORE__DATABASE="default"
export VECTOR_STORE__COLLECTION_PREFIX="graph_rag"
export VECTOR_STORE__ENABLED=true
```

本期目标架构不向 PostgreSQL 的 `document_segments.vector` 重复写入向量；分段正文和元数据保存在 PostgreSQL，Embedding 向量保存在 Milvus。

## Python 说明

`@classmethod` 会把方法变成“类方法”。它的第一个参数通常写成 `cls`，表示当前类本身，而不是某个实例。

常见用途：
- 直接通过类调用，不需要先创建对象
- 访问或修改类属性
- 作为工厂方法，返回当前类的实例

对比关系：
- `self`：实例方法，操作单个对象
- `cls`：类方法，操作整个类
- `@staticmethod`：既不需要 `self`，也不需要 `cls`，只是放在类里的普通函数

示例：

```python
class User:
    count = 0

    def __init__(self, name):
        self.name = name
        User.count += 1

    @classmethod
    def from_string(cls, s):
        return cls(s.strip())
```

`User.from_string(" Alice ")` 会调用类方法 `from_string`，其中 `cls` 指向 `User`，所以它会返回一个 `User` 对象。
### OCR 文档解析

完整安装、配置、清洗规则、预览和手动修订流程，以及常见问题见 [PaddleOCR 文档解析与清洗说明](docs/paddleocr.md)。

默认宿主机安装不会拉取 PaddleOCR、PaddlePaddle 或图像渲染依赖。Docker CPU 镜像会安装 `.[ocr]`，API 和 Worker 共用该镜像。准备 `.env` 后，设置 `PARSER__OCR_ENABLED=true` 并启动应用容器：

```bash
docker compose --profile app up -d --build api worker
```

首次启动应用容器前，在 PostgreSQL 中升级表结构：

```bash
docker compose --profile app run --rm api python -m alembic upgrade head
```

API 地址为 `http://localhost:8000`。Compose 的 `app` profile 启动 API 与索引 Worker（Worker 同时运行 Celery Beat）；普通 `docker compose up -d` 仍只启动基础设施。前端仍可按上面的本地开发方式单独运行。模型会保存到 `paddle_models` named volume，首次识别需要下载模型。

容器内使用 `postgres`、`minio`、`rabbitmq` 和 `standalone` 作为服务地址。调用宿主机上的 Embedding API 时，Compose 默认使用 `http://host.docker.internal:8001/v1`，可在 `.env` 覆盖 `EMBEDDING__BASE_URL`。

当前按 `linux/amd64` 构建 CPU 镜像，以兼容 PaddlePaddle 的 Linux x86_64 wheel；在 Apple Silicon Docker Desktop 上会使用架构仿真，首次构建和 OCR 推理会较慢。Docker Desktop 不提供 NVIDIA CUDA GPU 直通；GPU 部署需要 Linux/NVIDIA 主机和匹配的 PaddlePaddle GPU 镜像。宿主机直接运行 API/Worker 时，仍可按 [OCR 文档](docs/paddleocr.md) 安装 `pip install -e '.[ocr]'`。

启用后，`.png`、`.jpg`、`.jpeg` 直接使用 OCR；PDF 逐页优先使用文本层，只有没有文本的页面才渲染并 OCR。OCR 先执行标准清洗，用户在分段预览后仍可使用现有分段编辑器手动修改；保存会创建 revision，原始上传文件保持不变。

人工修订会作为独立的持久化输入保留。新建全库重建、配置索引或兼容上传索引任务时，每篇文档会绑定当时最新的已保存修订；没有修订的文档才重新解析原文件。保存成功但索引失败或取消的修订仍是用户已保存的内容，后续重建会使用它；重试既有任务则保持原先绑定的版本，不会读取排队期间新增的修订。高质量索引保留人工修订的分段边界与父子结构，自动分段规则的变更应用于未修订文档；切换到经济模式时，快照中的各段作为独立普通分段索引，原始父子结构仍完整保存在修订历史中。绑定的快照丢失或无效时，任务会明确失败，不会回退到原文件而覆盖修订。

新版 ZIP 归档使用 `format_version: 2`，在原文件、配置和文档元数据之外增加 `revisions.json`，保存各文档的修订历史、版本、作者、时间、分段内容、问答、关键词、来源位置及父子引用，并在 manifest 中校验大小和 SHA-256。导入时为文档、修订及分段生成新标识，同步重映射父子引用；修订以 `saved` 状态保存，不携带旧环境的任务或索引状态，首次重建即可消费。导入前会校验修订归属、版本唯一性及分段结构。仍可导入旧版 v1 归档；旧归档没有携带的人工修订无法从原文件恢复，需要从仍保留修订记录的原知识库重新导出。

启用上述任务输入绑定前运行 `alembic upgrade head`（迁移 `20261116_02`），并重启新版 API 和 Worker。已有单文档修订任务的显式引用会迁移到文档任务记录；后续新建的多文档任务可分别使用各文档自己的修订版本。
