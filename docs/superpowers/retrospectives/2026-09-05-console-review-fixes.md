# 2026-09-05 知识库控制台审查修复记录

后续界面调整单独记录在[控制台界面微调复盘](2026-09-05-console-visual-polish.md)，与本轮功能/数据库修复分开追踪。

## 背景与范围

用户在代码审查后批准修复，并要求记录修改以便复盘，随后要求关键逻辑添加注释。本次保留已有 FastAPI 请求契约，不新增权限系统、异步索引调度或迁移脚本。联调阶段经单独许可应用了仓库已有迁移，见下文。

审查基线：Git 工作区无改动；前端构建成功，但创建表单仍使用旧请求字段，上传仍使用旧地址。后端测试环境缺少 pytest、httpx 等依赖。

## 问题与处理

| 编号 | 原因与影响 | 处理方案 | 状态 |
| --- | --- | --- | --- |
| R1 | 创建表单提交旧字段，后端禁止额外字段，导致 422 | 仅提交 name、description、permission；移除未生效的索引/基础设施配置 | 已修复 |
| R2 | 上传地址和 multipart 字段错误，未关联 dataset ID | 先创建知识库，再在文档视图上传；使用 files；展示成功和拒绝结果 | 已修复 |
| R3 | 仅按文档数量推导完成状态，indexing/failed 筛选恒假 | 列表、详情、统计共用真实状态汇总；只统计有效已完成分段 | 已修复 |
| R4 | 前端未接入分页，默认只能访问第一页 10 条 | 接入 page/page_size/total；筛选重置页码；稳定排序 | 已修复 |
| R5 | 统计取前 10,000 条并构造 DTO，存在截断和额外开销 | 独立数据库聚合，不载入实体，不设统计条数上限 | 已修复 |
| R6 | 搜索无防抖/取消/过期保护，重复查询全局统计 | 300ms 防抖、请求取消与过期保护；统计独立刷新 | 已修复 |
| R7 | 独立审查发现请求失败后旧行被当作新页显示，文档错误恢复不完整 | 分页失败不显示旧行；区分列表加载错误和上传错误，保留部分提交提示 | 已修复 |

## 过程记录

- 安装项目已声明的后端及测试依赖，以运行回归测试。全量测试收集进一步发现旧的 `test_tabular.py` 使用未声明的 `xlwt`，已补入 `pyproject.toml` 的 test extra；未添加运行时后端依赖。
- 前端新增 Playwright 开发依赖，用浏览器级测试验证真实 UI 的请求和状态行为。
- 先添加后端回归测试 `tests/unit/repositories/test_knowledge_base_summary.py`，初次运行 5 failed，分别复现状态、任务/版本过滤、无效分段计数、10,001 条统计截断和排序不稳定；修复后 5 passed。
- 前端先添加 4 个浏览器测试，初次全部失败，复现创建契约、分页、过期响应覆盖和结构化错误展示问题；修复后扩充错误恢复和布局测试。
- 独立审查发现 R7，补 2 个测试并确认旧实现失败，再修复；另补充后端状态优先级、历史任务失败、空统计和真实 API 串联测试。
- 修复后再次独立复查，确认两项反馈均解决，未发现新增 P1/P2；审查者另行重跑新增后端文件，9 passed。
- 初始前端统计请求次数断言误把开发模式 StrictMode 的 effect 重放视作搜索重复请求；改为比较筛选前后请求数，未禁用 StrictMode 或放宽业务断言。

## 文件清单

| 文件 | 修改内容 |
| --- | --- |
| `frontend/src/App.jsx` | 简化创建表单；进入知识库文档视图；分页、防抖与过期保护；独立统计；删除错误提示/确认；移除不存在的 Milvus health 请求和伪默认配置 |
| `frontend/src/api.js` | 统一 HTTP 状态检查；解析 FastAPI detail 数组；处理 204；保留 AbortSignal |
| `frontend/src/components/Documents.jsx` | dataset-scoped 多文件上传、部分拒绝/503 提示、文档分页及错误恢复 |
| `frontend/src/components/Pagination.jsx` | 共享稳定尺寸的翻页控件、页数计算、边界禁用 |
| `frontend/src/styles.css` | 原生 dialog、上传控件、分页、横向滚动表格及移动端约束；图标对齐 |
| `frontend/package.json`、`package-lock.json` | 增加 Playwright 测试命令/依赖及 lucide-react 图标；未更换 React/Vite 框架 |
| `frontend/playwright.config.js`、`frontend/tests/console.spec.js` | 浏览器行为回归及 1440/390px 截图/溢出检查 |
| `rag_modules/repositories/knowledge_base_repository.py` | 共享 SQL 汇总、真实状态过滤、有效分段计数、数据库聚合统计、分页 ID 排序兜底 |
| `rag_modules/services/knowledge_base_service.py` | 接收仓储的明确状态，不再用文档数量猜测；统计不经过 DTO 列表 |
| `tests/unit/repositories/test_knowledge_base_summary.py` | 新增 9 项真实数据库/服务/API 回归测试 |
| `tests/api/test_dataset_api.py`、`tests/test_api_routes.py` | 更新仓储 stub 返回契约，显式提供状态；更新汇总 SQL 契约与统计字段断言 |
| `pyproject.toml` | 补充旧表格测试依赖 xlwt |
| `.gitignore` | 忽略 Playwright 截图、trace、报告等生成文件 |
| `README.md` | 记录迁移前置条件和复盘文档入口 |

## 接口与内部契约

- 创建请求仅接受 `name`、`description`、`permission`，权限选项为 `only_me`、`all_team_members`。
- 上传使用 `POST /api/knowledge_base/{dataset_id}/documents`，multipart 多次使用 `files` 字段；不再使用旧的 `/api/file_manage/upload`。
- 统计响应保留原字段，增加 `failed` 数量；状态分类计数之和等于 `total`。
- Repository 的列表行和详情由 `(record, document_count, chunk_count)` 改为附带第四个 `status` 值。所有已知消费方和测试替身一起更新，没有为旧内部结构保留“按数量猜状态”的后门。
- 创建成功只代表知识库元数据已保存；上传成功只代表原始文件和文档记录保存，不会自动提交尚未实现的索引任务。

## 状态规则

1. 有可处理文档正在下载/解析/分段/生成向量/索引，或有 pending/queued/running/retry_wait 任务：`indexing`。
2. 否则，有有效已完成分段：`ready`。失败的重建不会让旧的可用索引变成不可用。
3. 否则，有可处理文档 error/failed，或最近任务 failed/partial_success：`failed`。
4. 其余为 `draft`。仅上传且 `waiting`、尚未创建任务的文档不是索引完成。

有效分段必须未删除、status=completed、embedding_status 为 completed/not_required；文档未删除、启用且未归档。带版本 ID 的分段要求所属索引为同一知识库的 active 且未删除；旧的无版本 ID 分段要求文档 completed。重建中的文档仍保留旧 active 分段计数。

文档总数仍表示未删除文档，包括等待、禁用和归档文档，不与“可检索分段”混用。较早的失败任务不能覆盖较新的成功任务；分页同时间戳时按 ID 升序稳定排序。

关键注释已放在 SQL 汇总、旧版本兼容、状态优先级、统计聚合、前端防抖/过期保护、创建字段边界、结构化 API 错误和部分上传提交处；不逐行重复代码含义。

## 本机数据库与服务

- 本机原有 `5173` 前端和 `8000` 后端属于本项目，未停止或替换这些进程。
- 新状态查询初次联调返回 500。只读检查确认原库有三张业务表，但没有 `dataset_indexes`、`indexing_jobs`。
- 经工具明确请求许可后，执行 `.venv/bin/alembic upgrade head`，成功从基线升级到已有 revision `20260831_01`。新增索引任务/版本表、分段引用字段、约束和索引；本次没有修改迁移脚本，也没有删除现有业务表或数据。
- 迁移后真实后端列表返回 HTTP 200，统计返回全零且 HTTP 200；通过前端代理查询也成功。当前库没有知识库记录，未为验证向其写入测试知识库/文件。
- 为本次验收额外启动前端 `http://127.0.0.1:5174/`，代理到已有 `8000` 后端。
- 部署其他环境前必须应用该迁移。不要为了回退前端而直接执行 migration downgrade：它会删除新表/列中的索引任务数据，应先评估数据与依赖。

## 验证记录

| 验证 | 命令或方式 | 结果 |
| --- | --- | --- |
| 后端非基础设施集成测试 | `.venv/bin/python -m pytest -q -m 'not integration'` | 570 passed，3 deselected |
| 浏览器行为回归 | `cd frontend && npm test -- --reporter=line` | 11 passed |
| 前端生产构建 | `cd frontend && npm run build` | 成功 |
| 补丁格式 | `git diff --check` | 无错误 |
| 桌面/移动端 | Playwright 1440x900、390x844，列表/创建弹窗截图及根节点溢出断言 | 通过；已人工查看截图 |
| 真实 API 只读烟测 | 8000 列表/统计、5174 代理统计 | HTTP 200 |

后端新增测试使用 SQLite 内存库运行真实 SQLAlchemy 查询；串联 API 测试只替换对象存储，创建、上传校验、持久化、详情和统计仍走真实应用代码。浏览器测试 mock HTTP 边界，以确定性复现乱序、422、503 和部分成功；不能代替真实 MinIO/Milvus 集成测试。

截图位于 `frontend/test-results/console-layout-remains-usable-at-{1440,390}px/`，每次测试会重新生成，不提交 Git。现有第三方警告为 Starlette/httpx 与 jieba/pkg_resources 弃用提示；未在本次扩大依赖升级范围处理。

## 后续风险

- 未引入认证和资源授权；当前身份仍为原型占位值。
- 不实现尚未接入 API 的索引任务编排；上传成功不代表自动开始索引。
- 未经真实 PostgreSQL 执行计划和数据量验证，不声称数据库性能提升倍数。
- 未运行依赖真实 MinIO/Milvus 的 3 项集成测试；实际文件上传服务的连通性不属于已验证结论。
- 大数据量后续应针对 dataset_id、软删除过滤、任务时间排序检查执行计划，再决定索引方案；本次不盲目新增索引迁移。

## 复盘要点

1. 前端构建成功不代表 API 契约兼容，应同时验证请求字段、路径及可见结果。
2. 状态和数量必须有统一的数据来源，不能用“有上传”替代“可检索”。
3. 全局统计不能复用有分页上限的列表服务，避免隐藏截断和实体物化成本。
4. 异步 UI 必须同时考虑成功乱序与失败后旧数据，取消请求并不能代替状态展示约束。
5. 代码和数据库 schema 是共同的部署单元；单元测试建表成功不代表本机/部署库已应用迁移。
