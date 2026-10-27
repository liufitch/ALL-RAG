# 2026-10-27 实际环境能力验收与修复记录

本轮补齐此前尚未验收的真实 Embedding、高质量索引、父子分段、向量与混合检索、CPU OCR、模型抽取及 Neo4j 图检索。所有模型调用、数据库和存储写入均使用实际本地服务；没有用假向量或固定抽取响应替代模型。

**结果：5 组真实能力测试全部通过，另有真实浏览器综合流程通过。** 测试中新发现的问题已修复、补回归并更新运行中的 API/Worker。此前的通用功能测试记录见 [项目功能测试记录](2026-10-27-functional-test.md)。所有改动保留在工作区，尚未提交 Git。

## 实际运行环境

| 组件 | 本轮实际使用 |
|---|---|
| 宿主机 | macOS Apple Silicon / Apple M5 Pro |
| Docker | Desktop，18 CPU，约 8 GB 内存；API/Worker 为 `linux/amd64` CPU 镜像 |
| 业务链路 | FastAPI → PostgreSQL/MinIO → RabbitMQ → Celery Worker |
| Embedding | 本地已有 `BAAI/bge-large-zh-v1.5` 权重，TEI 1.9.3 ARM64 CPU 容器，真实 1024 维向量 |
| Milvus | 本地实际 collection、upsert、检索、版本切换 |
| 抽取模型 | Qwen2.5-1.5B-Instruct Q4_K_M，Ollama 0.35.0，原生 macOS Metal 推理 |
| 图存储 | 现有 Neo4j 5.18，实际 Entity/SourceSegment/RELATED 写入与查询 |
| OCR | PaddleOCR 3.7.0、PaddlePaddle 3.2.2、PP-OCRv5 中文模型，Docker CPU 推理 |
| 浏览器 | Chromium，前端 `http://127.0.0.1:5173`，真实 API `http://127.0.0.1:8000` |

本地已有模型是 **BGE Large Chinese v1.5**，并非原配置中的 BGE-M3。应用模型目录、服务模型名及默认选项已按实际模型对齐；没有将另一模型假装成 BGE-M3。该模型的 1024 维和 512 token 上下文见 [BAAI 官方模型说明](https://huggingface.co/BAAI/bge-large-zh-v1.5)。

Qwen 模型文件大小为 `1117320736` 字节，SHA-256 为 `6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e`，与 [Qwen 官方 GGUF 仓库](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF) 的 LFS 哈希一致。

## 问题 1：旧 Embedding 容器的权重挂载路径已失效

**现象**：本地已有 `embedding` 容器与 TEI 镜像，但启动后反复退出，日志报 `config.json not found`。应用原来的 8001 端口也没有模型服务。

**原因**：旧容器挂载 `/Users/fitch/Documents/workspace/data-agent/embedding/bge-large-zh-v1.5`，实际完整权重位于 `data-agent2/embedding/bge-large-zh-v1.5`。旧服务端口为 8081，应用则配置为 8001，模型名也不同。

**解决方案**：复用已安装 TEI 镜像及正确位置的已有权重，建立由项目 Compose 管理的 `graph-rag-embedding` 服务。模型目录只读挂载，禁止自动创建不存在的宿主机路径；增加真实 `/health` 检查和 2 GiB 内存限制。应用模型目录配置为 `bge-large-zh-v1.5`。旧的失效容器已停止，未删除。

**验证**：`/health`、`/info` 和 `/v1/embeddings` 成功；3 条不同文本得到 3 条有限数值组成的 1024 维向量。应用与 Worker 均通过 Docker 网络访问服务。Compose 合并配置通过校验，新服务为 healthy。

**配置**：`docker-compose.local-models.yml`、`.env.example`。本机实际权重路径保存于被 Git 忽略的根目录 `.env`。

## 问题 2：无 API Key 的本地模型请求在发送前失败

**现象**：直接调用真实 TEI 成功，但应用高质量索引停在 stage/retry_wait。日志最终显示 `Embedding request failed`。真实 HTTP 诊断确认：不传认证头时返回 200，发送空认证头时抛出 `LocalProtocolError: Illegal header value b'Bearer '`。图抽取客户端存在同样写法。

**原因**：两个客户端无条件构造 `Authorization: Bearer {api_key}`。本地无需认证时 key 为空，HTTP 传输层拒绝结尾为空格的头值。原测试使用 HTTP mock，未经过真实协议编码。

**解决方案**：仅在配置了非空 key 时发送认证头；保留有 key 时的 Bearer 认证。

**验证**：新增真实 loopback HTTP 测试，覆盖 Embedding/Graph 两种客户端及有 key/无 key 四种组合。修复前两项空 key 用例失败，修复后四项通过。更新容器后，先前等待的真实高质量任务由 Beat 自动重新投递并完成。

**代码与回归**：`rag_modules/embeddings/openai_compatible.py`、`rag_modules/graph/extraction.py`、`tests/unit/test_local_model_http.py`。

## 问题 3：模型异常的任务重试策略与检索 HTTP 状态不正确

**现象**：Embedding 的明确错误被 Worker 记录为通用 `INDEXING_WORKER_ERROR`；认证失败、输入超限等永久错误也会进入重试。实际提交 600 字符查询（超过当前模型目录的 500 字符输入限制）时，非流式检索返回 500，SSE 也将其标为通用服务失败。

**原因**：`EmbeddingError` 含有稳定错误码、安全消息及 retryable 标记，但 Worker 只识别 `DocumentIndexingError`；检索 API 也没有处理该异常类型。

**解决方案**：Worker 按 `EmbeddingError` 自带的 retryable 策略持久化原错误码和安全消息。普通检索对 `EMBEDDING_INPUT_INVALID` 返回 422，其他模型服务错误返回 503；SSE 将输入拒绝映射为 `RETRIEVAL_INPUT_INVALID`。

**验证**：3 项 Worker 错误分类回归、4 项普通/SSE API 回归均先失败、修复后通过。真实超长分段任务在首次尝试即 failed，错误码为 `EMBEDDING_INPUT_INVALID`，没有激活失败索引；真实过长查询返回 422，SSE 返回明确输入错误事件。

**代码与回归**：`rag_modules/tasks/indexing_tasks.py`、`rag_modules/api/retrieval_api.py`、`tests/unit/tasks/test_indexing_tasks.py`、`tests/api/test_retrieval_model_errors.py`、真实能力用例。

## 问题 4：高质量索引缺少关键词，全文无结果、混合检索退化

**现象**：真实语义查询能够召回水果和备份文档，但对已完成高质量索引的知识库查询“苹果 维生素”，`full_text` 返回空结果，`hybrid` 只有 vector 命中。

**原因**：高质量分支只生成向量，没有写入检索层所依赖的 `document_segments.keywords`。关键词仓储写入又只接受经济分段，因此仅在检索端切换模式无法解决。排查还发现：若关键词已生成、后续图抽取失败，原分段重试会将这些生成字段与空预览字段比较，误判为确定 ID 冲突。

**解决方案**：高质量分段完成向量写入后，为缺少关键词的普通块/子块生成并保存关键词，父块不写向量与关键词。保留非空的显式修订关键词和重试缓存。仓储允许向量已完成的普通块/子块保存关键词；重试时，空预览关键词不与派生缓存比较，显式指定的关键词仍严格比较。

**验证**：新增普通块、父子块和带已生成关键词的重试回归共 5 项，修复前失败、修复后通过。真实普通索引的 3 个活动分段均有关键词，3 个向量与 PostgreSQL 对齐；全文和 hybrid 均能命中水果文档，hybrid 的 match_type 为 hybrid。真实修订后新增关键词可召回；整库重建成功切换新索引；父子分段只写子块向量，多子块命中合并为同一个完整父块上下文。独立审查还验证了改变显式关键词时仍会拒绝冲突。

**代码与回归**：`rag_modules/indexing/engine.py`、`rag_modules/repositories/segment_repository.py`、对应引擎/仓储回归、真实能力用例。

## 问题 5：抽取模型返回数字关系端点，图中只有节点没有边

**现象**：真实 Neo4j 已保存“星河科技”和“远山研究院”，但关系数量为 0；查询“星河科技有哪些合作机构？”只召回原合作文档，无法通过邻接关系召回研究文档。

**原因**：捕获原始模型响应发现 `relations` 使用 `source: 0, target: 1`，`attributes` 为数组。原提示仅给出字段名，没有明确字段类型。解析器按既有规则拒绝数字端点，因而丢失关系。

**解决方案**：提示明确 attributes 必须是键值对象、端点必须为实体名称字符串，补充输入/输出示例并要求不复制示例实体。保留原文实体校验、端点存在校验及数字端点拒绝规则，不将模型的不合法结果宽松写入图。

**验证**：同一真实模型对同一合作句输出合法字符串端点；更新 API/Worker 后，原先失败的图邻接用例通过。查询星河科技能够召回只写“远山研究院研发智能检索系统”的第二份文档，无关书店文档被排除。禁用第二份文档后不再召回，重新启用后恢复。真实浏览器也显示两个图命中来源，Neo4j 实际存在 `星河科技 -[合作]-> 远山研究院`。

这是本次样本与配置的验收，不是抽取准确率评测。小模型在较复杂诊断句上曾遗漏地点实体，原始输出保存在证据中；本轮没有宣称任意文档的实体和关系都能完整识别。

**代码与回归**：`rag_modules/graph/extraction.py`、`tests/integration/test_real_capabilities.py` 的真实图检索用例。

## 实际能力验证明细

| 能力 | 实际验证与结果 |
|---|---|
| 语义召回 | “适合补充维他命的健康食物有哪些？”首位为水果文档；“发生灾害后怎样找回业务数据？”首位为备份文档。初次探测分数分别约 0.495、0.453 |
| 高质量全文/混合 | 实际 PostgreSQL 关键词已生成；全文命中，混合结果同时使用关键词和向量 |
| 人工修订 | 修订正文重新生成向量和关键词，`REALREVISION2026` 可检索 |
| 整库重建 | 新 index ID 成功激活，向量数量正确，按原始文件重建，修订原文件保持不变 |
| 父子分段 | 仅子块有向量；父块 embedding 为 not_required；多子块结果合并并返回完整父上下文 |
| 真实 OCR | PNG、JPEG、扫描 PDF、混合 PDF 均经 API 预览和 Worker 索引；中文、`OCR TEST 123`（允许空格差异）、`ALPHA2026` 正文完整，置信度 >0.8，文字框存在；混合 PDF 文本层也保留 |
| OCR 高质量检索 | 4 份 OCR 文档均能全文与向量召回，下载 SHA-256 与原始样本相同 |
| 图抽取/写入/检索 | 真实 Qwen 抽取、真实 Neo4j 写入、跨来源邻接召回及文档可见性过滤通过 |
| 输入错误 | 超限查询 422，SSE 输入错误；超限索引首次失败、不重试、不激活 |
| 真实浏览器 | 从创建、4 文件上传、OCR 预览到高质量索引完成；分别切换 vector/graph/full_text 并检索；中文原图下载文件名正确；页面脚本错误 0 |

最终验证命令和结果：

| 检查 | 结果 | 本地产物 |
|---|---|---|
| 后端全量 | **722 通过、11 跳过**；2 条既有依赖弃用警告 | `backend-tests-final.log` |
| 5 组实际能力 | **5 通过，62.65 秒** | `real-capabilities-final.log`、各 `test_real_*.json` |
| 既有真实 HTTP 文档生命周期/导入导出 | **3 通过**，图启用的最终容器上复验 | `api-flow-final.log` |
| MinIO/Milvus 真实集成 | **4 通过** | `storage-integration-final.log` |
| 前端 Playwright | **17 通过**，这些既有测试模拟 API | `frontend-tests.log` |
| 前端构建 | 通过 | `frontend-build.log` |
| 真实浏览器综合流程 | 通过，脚本错误 0 | `browser-results.json`、`browser-real.log`、4 张截图 |
| API/Worker 源码一致性 | 两个容器各 **116 个 Python 源文件**与工作区 SHA-256 一致 | `runtime-verification.json` |
| Compose 与补丁检查 | 合并配置、`git diff --check` 均通过 | 命令退出码 0 |

默认跳过的 11 项是需要显式启动外部服务的 3 项存储测试、3 项 HTTP 测试和新增 5 项实际能力测试，均已独立显式执行。存储单独运行的 4 项中另含 1 项本地清理异常校验，因此不要将所有数字直接相加当作唯一用例数。

本地证据目录：`data/real-environment-2026-10-27/`，由 `.gitignore` 排除。该目录含模型文件、原始 HTTP 响应、数据库/Milvus/Neo4j 摘要、复现前后日志和截图。可重复执行的实际能力测试与 OCR 样本已放入 `tests/integration/test_real_capabilities.py` 和 `tests/fixtures/documents/ocr-*`，不依赖忽略目录中的生成样本。

## 已保留的可操作验收结果

控制台保留知识库 **真实模型综合验收 2026-10-27**，ID 为 `3d35459ca2e6447c95ef9e77b6c12d3d`。该库含水果营养、合作机构、研究内容和 OCR 图片 4 份文档，活动索引中有 4 个分段、4 个真实 1024 维向量，以及实际 Neo4j 实体和合作关系。已将检索模式保存为 hybrid；可以在“知识库设置”切换 vector、full_text 或 graph。

可直接尝试：

- vector：`适合补充维他命的健康食物有哪些？`
- graph：`星河科技有哪些合作机构？`
- full_text：`ALPHA2026`

调试阶段的随机测试库已限定本轮创建时间与名称前缀清理：文档/知识库软删除，测试专属 Milvus collection 和 Neo4j 数据清理。既有知识库、模型源目录及其他 Docker 服务未删除。综合验收库及其向量/图保留，服务继续运行。

## 本机复启与复验

本机已导入 Qwen 并设置 `.env` 的 `LOCAL_EMBEDDING_MODEL_PATH`，无需重复下载。Ollama 当前由本轮启动的原生进程运行；需要重启时在独立终端执行 `ollama serve`。本轮使用 Ollama 而非 Docker 跑抽取模型，以使用 Apple Metal 并避免占用 Docker 的有限内存。

在项目根目录启动本地模型覆盖配置：

```bash
docker compose -f docker-compose.yml -f docker-compose.local-models.yml --profile app up -d --build api worker
```

`embedding` 会作为依赖启动并通过健康检查。已有最终应用镜像时可将 `--build` 改为 `--no-build`。该覆盖文件默认启用本地真实图抽取；只测高质量向量/OCR 时可设置 `LOCAL_GRAPH_ENABLED=false`。普通启动仅使用原 `docker-compose.yml` 时，不会自动加入这套本地模型目录和地址配置。

```bash
RUN_REAL_ENV=1 RUN_REAL_GRAPH=1 KEEP_REAL_ENV_DATA=1 \
  .venv/bin/python -m pytest tests/integration/test_real_capabilities.py -v -s

RUN_API_E2E=1 .venv/bin/python -m pytest tests/integration/test_console_flow.py -v -s
RUN_INTEGRATION=1 .venv/bin/python -m pytest tests/integration/test_minio_documents.py tests/integration/test_milvus_index.py -q
.venv/bin/python -m pytest -q
```

`KEEP_REAL_ENV_DATA=1` 保留验收知识库；省略时测试会清理自身创建的文档、知识库和 Milvus 集合。重复执行会创建新知识库，避免覆盖已有数据。`REAL_ENV_ARTIFACTS` 可指定新的证据目录；`API_E2E_URL` 可指定另一 API 地址。

本机模型注册文件保存在 `data/real-environment-2026-10-27/Modelfile`，包含 Qwen ChatML 对话模板；原生导入方式见 [Ollama 官方说明](https://github.com/ollama/ollama/blob/main/docs/import.mdx)。

## GPU OCR 的实际硬件边界

容器实测 `paddle.is_compiled_with_cuda() == false`，`paddle.device.get_device() == "cpu"`，因此本轮**完成真实 CPU OCR，没有宣称 GPU OCR 通过**。Apple Metal 用于原生 Qwen 推理，不能据此推断 Docker 内 Paddle 获得 CUDA。

[Docker 官方 GPU 说明](https://docs.docker.com/desktop/features/gpu/)指出，Desktop 的 `--gpus` Linux 容器透传针对 Windows WSL2；当前 Mac Docker 环境不能凭安装 Docker 获得 NVIDIA CUDA。若要验收 Paddle CUDA GPU OCR，需要具备相应 GPU 及 Paddle GPU 镜像的 Linux/NVIDIA 环境。
