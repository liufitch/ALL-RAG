# 2026-10-27 项目功能测试与修复记录

本轮对当前工作区执行后端、浏览器、真实存储集成和真实 HTTP 流程测试，发现并修复 3 类问题。每类问题均记录现象、原因、解决方案和验证结果，并补充自动化回归。

开始时工作区已有索引、OCR、修订等未提交修复；这些改动均保留，其历史排查见 [本地 Docker/OCR 流程记录](2026-10-27-local-flow-test.md)。本文件单独记录本轮新增发现，避免把已有修复当作本轮新增工作。改动未提交 Git。

## 环境与范围

- 日期与时区：2026-10-27，Asia/Shanghai。
- macOS Apple Silicon；宿主机 Python 3.11 虚拟环境；Docker 应用采用 `linux/amd64`。
- 真实服务：PostgreSQL、MinIO、RabbitMQ/Celery Worker、Milvus、API。
- 初次复现使用现有容器 API `http://127.0.0.1:8000`。源码修复先在临时宿主机 API `http://127.0.0.1:8002` 验证，再更新容器 API/Worker，对 8000 重新执行真实 HTTP 回归。
- 前端测试分开统计：17 项既有 Playwright 用例模拟 API；另以真实浏览器访问 FastAPI 托管的构建页面，检查资源加载和中文文件下载。
- 真实流程创建独立随机命名的测试知识库，完成后通过 API 软删除测试文档与知识库，不修改既有知识库。

## 问题 1：中文、Emoji 文件名下载返回 500，特殊符号文件名被改变

**现象**：上传 `中文功能测试.txt` 返回 201，但下载原文件返回 500。日志显示 `UnicodeEncodeError: 'latin-1' codec can't encode characters`。原实现还把文件名中的双引号替换为单引号，下载名称与原名称不一致。

**原因**：`download_document` 将 Unicode 文件名直接拼入 `Content-Disposition` 响应头，Starlette 编码响应头时失败；原有双引号替换也无法完整表达特殊文件名。

**解决方案**：保留去除 CR/LF 的处理；对 Unicode 和特殊字符使用 UTF-8 百分号编码的 `filename*` 参数，普通英文文件名继续使用带引号的 `filename`。

**验证**：

- 修复前：新增 4 个下载用例中，中文、Emoji、双引号 3 项失败，普通英文通过。
- 修复后：4 项全部通过，下载内容字节不变，文件名可完整还原。
- 真实 HTTP：中文与 Emoji 文件名的上传、去重和下载通过。
- 真实 Chromium 下载：浏览器建议保存文件名为 `中文功能测试.txt`，无页面脚本异常。

**代码与回归**：`rag_modules/api/file_api.py`、`tests/api/test_document_governance_api.py`、`tests/integration/test_console_flow.py`。

## 问题 2：导入非法 ZIP 元数据时返回 500，缺少写入前校验

**现象**：ZIP 文件合法，但 `manifest.json` 为数组或 `files` 内含 `null` 时，真实导入接口返回 500。仓储替身回归还复现了非法权限、索引方式、文档字段没有被拒绝，或在类型异常前已写入原文件。

**原因**：服务只解析 JSON，没有验证 JSON 顶层类型、成员结构和字段类型，随后直接调用 `.get()`、构建字典或使用字段；错误没有转换成已有的 `ArchiveValidationError`。格式版本也没有被明确验证。

**解决方案**：在写入对象存储前，用 Pydantic 校验 manifest、知识库与文档元数据，包括格式版本、文件哈希/长度、权限、索引方式、路径和配置结构。非法输入沿用接口的 422 响应，不进入对象写入或数据库记录创建。

审查中发现严格权限校验会拒绝历史 `private/team/public` 值，而导出功能仍会原样导出这些值。已将其分别归一化为 `only_me/all_team_members/all_members`，保留原权限含义，未知值仍拒绝。

**验证**：

- 15 类非法元数据用例修复前全部失败，修复后全部通过；验证拒绝后没有写入对象或创建数据库记录。
- 6 种当前/历史权限分别执行真正的服务层导出→导入，权限含义与原始文件字节保持一致。
- 原有路径穿越、哈希不匹配、正常导入导出回归继续通过。
- 更新后的容器 API 对两类曾返回 500 的非法 manifest 均返回 422。
- 真实 PostgreSQL/MinIO/Worker 的导出→导入并重建通过；导入后索引配置、检索配置和描述保持一致，正文可检索。
- 导出保存的是原始上传文件；人工修改分段不会替换原文件。重新导入后按原始文件重建，该行为已显式验证。

**代码与回归**：`rag_modules/services/knowledge_base_archive_service.py`、`tests/unit/services/test_knowledge_base_archive_import.py`、`tests/unit/services/test_knowledge_base_archive_service.py`、`tests/integration/test_console_flow.py`。

## 问题 3：按 README 构建前端后，FastAPI 首页仍返回 404

**现象**：`npm run build` 成功，`frontend/dist/index.html` 和资源存在，但启动 FastAPI 后访问 `/` 仍返回 404，与 README 的生产启动方式不符。

**原因**：`main.py` 只注册 API 路由，没有注册静态文件托管。

**解决方案**：将应用初始化集中到 `create_app()`。启动时按 `main.py` 的位置查找 `frontend/dist/index.html`，存在时在 API 路由之后挂载静态目录；不存在构建产物时仍支持单独启动 API。README 补充构建后启动/重启要求及当前 Docker 镜像仅包含后端的边界。

**验证**：

- 新增 2 项回归先失败、修复后通过：有构建时首页和脚本可访问；无构建时 API/OpenAPI 正常启动。
- 缺失资源和未知 API 地址继续返回 404，不将首页冒充 API 响应。
- 宿主机真实 Chromium 从 `http://127.0.0.1:8002` 加载生产构建，首页与脚本返回 200，显示真实知识库列表，页面脚本错误为 0。

**代码与回归**：`main.py`、`tests/api/test_frontend_hosting.py`、`README.md`。

## 最终验证结果

| 检查 | 结果 | 证据（相对于运行产物目录） |
|---|---|---|
| 后端完整回归 | **708 通过、6 跳过**；2 条依赖弃用警告 | `backend-tests.log` |
| 前端 Playwright | **17 通过**，API 为模拟响应 | `frontend-tests.log` |
| 前端生产构建 | 通过 | `frontend-build.log` |
| 真实 MinIO/Milvus 集成 | **4 通过** | `integration-tests.log` |
| 更新后容器上的真实 HTTP 流程 | **3 通过**，2.64 秒 | `api-flow-docker.log` |
| 临时宿主机 API 上的真实 HTTP 流程 | **3 通过**；包含实际 Worker 消费 | `api-flow.log` |
| 生产页面真实浏览器访问及中文下载 | 通过，页面脚本错误 0 | `production-browser.json`、`production-console.png` |
| 容器 API/Worker 更新 | 已使用原 OCR 依赖镜像复制当前源码并重新创建；3 个本轮修改的运行时文件 SHA-256 与工作区一致 | `docker-runtime-build.log`、`docker-recreate.log`、`runtime-verification.json` |
| 补丁空白检查 | `git diff --check` 通过 | 命令退出码 0 |

后端默认跳过的 6 项是 3 项 MinIO/Milvus 测试及 3 项真实 HTTP 测试，均已通过独立显式命令执行。依赖警告来自 Starlette/httpx 和 anyio 的弃用接口，本轮没有通过屏蔽警告修改测试结果。

运行产物目录：`data/functional-test-2026-10-27/`，由 `.gitignore` 排除。修复前证据包括 `reproduction.json`、`archive-reproduction.json`、`download-before.log`、`archive-before.log`、`archive-legacy-before.log`、`frontend-hosting-before.log`。

真实 HTTP 长流程覆盖：创建知识库、中文/Emoji 文件上传与去重、下载字节及文件名、预览、确认经济索引、Worker 完成、保存设置、按保存的参数检索、SSE、禁用/启用、归档/恢复、重命名、分段修订、过期修订 409、原文件不变、导出 ZIP、导入后重建、配置保留、删除后不可检索与下载 404。

## 复验命令

在项目根目录执行：

```bash
.venv/bin/python -m pytest -q
RUN_INTEGRATION=1 .venv/bin/python -m pytest tests/integration/test_minio_documents.py tests/integration/test_milvus_index.py -q
RUN_API_E2E=1 .venv/bin/python -m pytest tests/integration/test_console_flow.py -v -s
```

真实 HTTP 用例默认访问 `http://127.0.0.1:8000`，可通过 `API_E2E_URL` 指定另一地址；需要 API、数据库、对象存储、消息队列和 Worker 已就绪。用例仅软删除其自身创建的文档和知识库。

在 `frontend/` 执行：

```bash
npm test -- --reporter=line
npm run build
```

## 尚未覆盖的实际环境能力

下列内容是本轮通用测试完成时的历史状态。后续已利用本地 Docker 和真实模型补验高质量索引、向量/混合检索、Neo4j 抽取与图检索、CPU OCR，详见 [实际环境能力验收与修复记录](2026-10-27-real-environment-test.md)。GPU OCR 仍受当前硬件与镜像限制。

- **高质量索引及真实向量语义召回**：应用容器访问配置的 Embedding `/models` 仍报 `ConnectError`；需要可用的实际 Embedding 服务。本轮真实流程使用经济索引，Milvus 的集合写入/更新/计数/删除独立集成已通过，不能据此宣称完整语义检索通过。
- **Neo4j 真实抽取和图检索**：当前 `GRAPH__ENABLED=false`。图模块单元回归包含在后端 708 项中，未在本轮启用真实模型抽取链路。
- **真实 OCR 重跑及 GPU OCR**：本轮没有重新下载或重复运行 OCR 模型。已有真实 CPU OCR 流程证据保留在前述 Docker/OCR 测试记录中；本轮验证了包含 OCR 模块的后端回归。
