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
