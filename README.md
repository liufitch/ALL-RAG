# Graph-RAG

FastAPI + React 知识库管理台原型。

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

后端默认通过环境变量连接 Milvus：

```bash
export MILVUS_URI="http://localhost:19530"
export MILVUS_TOKEN=""
export MILVUS_USER=""
export MILVUS_PASSWORD=""
export MILVUS_DATABASE="default"
export MILVUS_COLLECTION_PREFIX="graph_rag"
export MILVUS_ENABLED=true
```

创建知识库时会自动检查并创建对应的 Milvus collection。
