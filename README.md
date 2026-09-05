# Graph-RAG

Graph-RAG 是一个基于 FastAPI 和 React 的知识库管理与检索项目。项目正在从知识库管理原型演进为完整的异步索引系统：PostgreSQL 保存业务元数据和正文，MinIO 保存原始文件，RabbitMQ/Celery 执行持久化索引任务，OpenAI-compatible API 生成 Embedding，Milvus 保存高质量向量，Neo4j 承载后续图关系检索。

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
- Neo4j：图实体和关系；当前 Python Graph-RAG 检索模块仍处于预留阶段。

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

生产模式可以先构建前端，再由 FastAPI 托管静态文件：

```bash
cd frontend
npm run build
cd ..
uvicorn main:app --reload
```

访问 `http://127.0.0.1:8000`。

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
