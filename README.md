# Graph-RAG

Graph-RAG 是一个基于 FastAPI 和 React 的知识库管理与检索项目。项目正在从知识库管理原型演进为完整的异步索引系统：PostgreSQL 保存业务元数据和正文，MinIO 保存原始文件，RabbitMQ/Celery 执行持久化索引任务，OpenAI-compatible API 生成 Embedding，Milvus 保存高质量向量，Neo4j 可选保存实体关系图。

## 项目架构

完整架构、组件职责、数据模型、索引流程、可靠性设计和当前实施状态见：

- [项目架构设计](docs/architecture.md)
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

本次控制台修复的原因、改动清单、状态规则及验证结果见 [2026-09-05 复盘记录](docs/superpowers/retrospectives/2026-09-05-console-review-fixes.md)。

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

高质量索引可选择父子分段，并设置“按段落”或“全文”父块模式、父块长度、子块长度和子块重叠。预览会把子块列在对应父块下；索引时只为子块生成向量。检索命中子块后展示父块上下文，同一父块的多个命中只占一个结果名额。经济索引只支持普通分段。

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
