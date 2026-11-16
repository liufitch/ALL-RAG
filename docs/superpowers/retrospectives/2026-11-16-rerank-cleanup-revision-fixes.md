# 2026-11-16 重排、文档清理与人工修订保留问题记录

日期：2026-11-16，Asia/Shanghai。代码修复提交：[eb0e616](https://github.com/liufitch/ALL-RAG/commit/eb0e6162e26ff1cd99fe58992b850ea4a3bac33f)。

## 范围与交付状态

本轮处理三类问题：前端重排开关没有连接实际 reranker；文档删除缺少向量、图谱清理及失败补偿；人工分段修订在全库重建和归档迁移后丢失。本文同时记录实现和独立审查中发现的并发、异常与兼容问题。

修复代码、迁移、配置说明和回归测试已提交并推送到远端 `main`。本轮未把新迁移应用到正在使用的业务库，也未重新部署运行中的 API/Worker。真实 PostgreSQL 迁移验证使用隔离 schema；Neo4j/Milvus 验证只创建、清理测试专属资源。

较早的追加文档索引、知识库删除访问控制及长文档续跑问题见 [2026-11-10 索引生命周期记录](2026-11-10-indexing-lifecycle-fixes.md)。其中关于底层清理由既有任务负责的描述，由本次跨存储清理及补偿实现进一步补齐。

## 1. 重排开关存在，但检索服务没有 reranker

**现象与影响：** 前端可以勾选“启用重排”，请求和知识库设置也会保存 `rerank_enabled`，但实际创建的 `RetrievalService.reranker` 为 `None`，开启选项后结果不会经过重排。

**根因：** `get_retrieval_service` 仅注入 Embedding、向量存储和图谱存储，没有创建重排客户端。原有测试验证了开关参数传递，未验证真实依赖创建后的结果排序。

**处理：**

- 新增异步 `RerankClient` 和 `RerankSettings`，对接 Jina/Cohere 兼容的 `/rerank` 协议，并注入普通检索与 SSE 共用的检索依赖。
- 按提供方响应的 `index` 对齐输入候选分数，避免将已排序响应直接当作输入顺序；完成重排后再截取 Top-K。
- 仅在启用重排且存在候选时请求模型；请求失败或结果无效时保留原召回顺序，记录 `rerank_failed`，不在异常中暴露响应正文或凭据。
- 请求结束后关闭由依赖创建的客户端。注入的 HTTP 客户端仍由调用方管理。

**验证：** 使用真实 API 依赖和检索逻辑，替换外部 HTTP 边界，确认开启后顺序改变、关闭时不请求模型、知识库默认值和显式关闭均生效。另覆盖乱序响应、重复/越界索引、缺失/非有限分数、HTTP 错误及客户端关闭。

**部署条件：** 需要单独配置 `RERANK__BASE_URL`、`RERANK__MODEL`、可选的 `RERANK__API_KEY` 和 `RERANK__REQUEST_TIMEOUT`。Embedding 服务不一定提供重排接口。本轮使用受控 HTTP 响应验证协议及调用链，未部署或验收真实重排模型的排序质量。

主要实现：`rag_modules/api/retrieval_api.py`、`rag_modules/reranking.py`、`rag_modules/retrieval.py`、`rag_modules/config/settings.py`。

## 2. 文档删除后残留向量、图谱及清理失败数据

**现象与影响：** 原任务仅删除 MinIO 原文件并软删除 PostgreSQL 分段，没有清理 Milvus 和 Neo4j；MinIO 删除失败直接返回，注释所称的定时补偿并不存在。长期运行可能积累残留对象、向量和图谱事实。

**处理：**

- 清理前校验知识库 ID、文档 ID、文档已软删除且清理尚未完成，防止误投消息删除有效文档原文件。
- 分别处理 MinIO、Milvus、Neo4j 和 PostgreSQL 分段；单个外部存储失败不会阻断其他存储的清理。
- Milvus 按文档删除其所在知识库各索引版本的向量，包括 building、retired 和软删除版本；重复删除、集合已不存在视为安全结果。
- Neo4j 在事务内删除该文档的来源关系、来源分段及不再被其他来源引用的实体，保留其他文档的共享实体和事实。
- 新增 `cleanup_completed_at`、`cleanup_last_attempt_at`。只有各项清理成功才标记完成；Beat 每 60 秒扫描并分批补投，失败项轮转，避免占据批次使后续文档饥饿。
- 旧版已软删除文档的新完成字段为空，升级后也会进入补偿扫描。Broker 投递失败不丢失数据库中的清理意图。

### 审查中追加修复的边界

| 问题 | 原因与影响 | 处理 |
| --- | --- | --- |
| 关闭图谱后遗留数据被误判为清理完成 | 普通图谱工厂在 `GRAPH__ENABLED=false` 时返回 `None`，但历史数据可能存在 | 增加独立的 `for_cleanup=True` 连接路径，不依赖新索引/检索的功能开关；连接失败保持待补偿 |
| 清理后正在运行的索引又写回数据 | 文档删除未进入索引取消检查；已发起的远程 upsert 不能立即中断 | 领取任务时校验并锁定文档，通过标量查询观察最新删除状态；清理等待 running 状态结束 |
| 租约恢复后旧 Worker 延迟写回 | 恢复任务改变数据库状态和所有者，但不能停止旧进程已发起的远程操作 | Worker 等待外部操作结束后，在新 session 中重新打开已删除文档的清理状态，由 Beat 再清理 |
| 补偿状态写入失败后消息被确认 | Celery 默认会确认普通失败；仅设置 worker-lost 重投无法覆盖数据库临时异常 | 使用专用 `DocumentCleanupCompensationPending` 异常，任务边界持续退避重试；文档已删除、无法创建 runner 的重投也执行补偿 |

**验证：** 回归覆盖各存储单独失败、重复投递、跨知识库/有效文档保护、Broker 故障、批次轮转、运行中索引、租约恢复后延迟写入，以及补偿写入失败一次后的重投。真实 Neo4j 验证共享实体与其他文档事实保留；真实 Milvus 验证其他文档向量保留、重复删除和集合已删除。

**运行条件与范围：** 保持 Beat 和消费 `maintenance` 队列的 Worker 运行。清理历史图谱仍需有效的 Neo4j 地址、数据库和凭据，即使图谱功能已关闭。任务以文档软删除记录为补偿来源，不构成扫描所有存储的通用孤儿数据回收器；没有对应文档记录或已失去索引集合引用的历史孤儿资源不在本轮保证范围内。

主要实现：`rag_modules/tasks/document_cleanup_tasks.py`、`rag_modules/tasks/runtime.py`、`rag_modules/tasks/indexing_tasks.py`、`rag_modules/repositories/indexing_repository.py`、`rag_modules/graph/neo4j_store.py`、`rag_modules/vector_stores/milvus.py`。

## 3. 人工分段修订在重建或迁移后丢失

**现象与影响：** 分段编辑已保存 revision，Worker 却只在整个任务带有 `revision_id` 时读取快照；全库重建未按文档绑定修订。v1 归档只包含配置、元数据及原文件，迁移后重新解析原文件会丢失人工修订。

**处理：**

1. 在 `IndexingJobDocumentRecord` 上新增独立 `revision_id`，任务创建时为每篇文档绑定最新的已保存修订。全库重建、配置索引和兼容索引入口共用绑定逻辑；没有修订时才读取原文件。
2. 重试保留原绑定，避免在排队或续跑中切换到较新的修订。保存成功但索引失败或取消的修订仍属于已保存内容，不因发布失败而丢弃。
3. 高质量索引使用快照自身的分段模式，保留人工边界和父子结构；自动分段规则变更应用于未修订文档。经济模式将快照各段索引为独立普通分段，保留全部内容及相关字段，不修改修订历史中的原始结构。
4. 归档升级到 v2，增加带大小及 SHA-256 校验的 `revisions.json`，保存完整修订历史、版本、作者、时间、分段内容、问答、关键词、来源元数据及父子引用。
5. 导入为文档、修订及快照分段生成新标识，并同步映射父子引用；修订保存为 `saved`，不携带旧任务状态或索引引用，随后重建直接使用快照。
6. 继续支持没有修订快照的 v1 归档；保留旧单文档任务的显式任务级绑定兼容路径。

### 审查中追加修复的边界

| 问题 | 原因与影响 | 处理 |
| --- | --- | --- |
| 绑定快照无效时任务一直无法进入终态 | 在构建 runner、领取任务前抛错，失败没有持久化 | 读取时记录校验结果，在领取任务后的 command 构建中抛出非重试错误 `REVISION_SNAPSHOT_INVALID`，不回退原文件 |
| 改变自动分段规则后修订无法索引 | 快照类型与任务分段模式不一致，导致 `STAGED_SEGMENT_STATE_INVALID` | 按快照结构选择文档执行模式，并对经济模式做保留内容的普通分段投影 |
| 归档校验通过但暂存阶段拒绝结构 | 初版归档校验允许非 child 的父引用、负位置和超长标识 | 在线编辑、归档导入、Worker 共用结构校验；写对象前拒绝非法引用、重复版本、错误归属和无效快照 |
| 新修订外键影响导入写入顺序 | 修订依赖文档，文档任务依赖修订，立即外键不能依赖未保存父记录 | 按父记录顺序 flush；使用启用立即外键的测试验证导入和配置任务绑定 |

**验证：** 实际执行“导出 → 导入 → 创建全库重建 → Worker/索引引擎执行 → 检索”，确认命中人工修改内容，原文件独立保留。测试覆盖历史版本、新旧标识隔离、父子引用映射、问答/关键词/来源位置、任务重试固定输入、规则切换、经济模式投影、损坏归档在对象写入前拒绝、缺失绑定快照进入失败终态。

**兼容边界：** v1 归档没有携带的修订无法从原文件推导恢复，需要从仍保留修订记录的原库重新导出。迁移只回填旧任务中已有的显式修订引用，不根据当前最新修订重写所有历史任务的输入；新的多文档任务才按文档创建固定绑定。

主要实现：`rag_modules/repositories/revision_snapshot.py`、`rag_modules/repositories/knowledge_base_repository.py`、`rag_modules/repositories/process_rule_repository.py`、`rag_modules/services/document_revision_service.py`、`rag_modules/services/knowledge_base_archive_service.py`、`rag_modules/documents/revisions.py`、`rag_modules/tasks/indexing_tasks.py`。

## 4. 验证期间遇到的测试与环境问题

- SQLite 的 `metadata.create_all` 无法直接复现本项目 PostgreSQL 部分唯一索引的行为，多索引版本夹具触发 `UNIQUE constraint failed: dataset_indexes.dataset_id`。按已有测试约定在隔离夹具中移除该索引；需要验证约束的夹具则显式创建正确的 SQLite 条件索引，没有修改生产索引语义。
- 任务领取测试第一次从 SQLite 重新加载时间字段时触发 aware/naive datetime 比较错误，掩盖“已删除文档仍可领取”的目标断言。调整夹具保持所需记录和时区一致后，先确认目标断言失败，再验证修复。
- 添加文档删除检查后，一个旧取消测试没有创建实际文档，新的关联查询找不到记录。补齐真实文档夹具，保留跨 session 的取消验证。
- 真实 Neo4j 验证首次缺少有效凭据，失败于连接认证阶段；使用已有本地配置中的有效凭据后重跑。凭据值不写入记录、命令输出或 Git。
- Milvus 测试读取删除后的明细时，空 filter 未提供 limit，被服务端拒绝；补上测试查询的 limit 后验证其他文档向量仍存在。
- 导出仓储新增修订查询后，原测试替身缺少方法；补充空历史返回值，并将新版导出预期更新为 v2。原真实环境用例中“重建/导入后应丢失修订”的旧断言也改为应保留修订。

以上属于夹具或验证条件问题，与已复现的产品缺陷分开记录；没有通过跳过目标分支或放宽业务断言获得通过结果。

## 5. 验证结果与复现入口

以下为代码提交 `eb0e616` 对应的实际验证结果，不表示已完成运行中服务的部署验收。

| 验证 | 命令或入口 | 结果 |
| --- | --- | --- |
| 提交前完整后端回归 | `.venv/bin/python -m pytest tests -q --tb=short` | 856 passed，17 skipped，2 条依赖弃用警告 |
| 真实 Neo4j/Milvus 与清理迁移 | `tests/integration/test_document_cleanup.py`，显式启用外部服务测试并配置凭据 | 3 passed |
| 真实 PostgreSQL 修订迁移 | `RUN_INTEGRATION=1 .venv/bin/python -m pytest tests/integration/test_revision_migration.py -q --tb=short` | 1 passed；隔离 schema 升级、回填与降级 |
| 修订往返与完整重建 | `tests/unit/services/test_revision_preservation.py` | 覆盖真实服务、数据库、Worker、索引引擎和检索；对象存储为内存替身 |
| 独立代码审查 | 分轮审查并修复上述发现 | 最终复审无剩余可操作问题 |
| 配置与补丁检查 | `git diff --check`、Python compileall、`docker compose config --quiet`、`alembic heads` | 通过；最终迁移 head 为 `20261116_02` |

默认完整回归中跳过的外部服务用例需要显式启用。真实模型/完整容器的修订迁移端到端用例已更新预期，但本轮没有部署新版容器后重新执行，不将单元层完整链路表述为真实模型或运行中容器验收。

关键新增测试：`tests/api/test_retrieval_reranker.py`、`tests/unit/retrieval/test_reranking.py`、`tests/unit/tasks/test_document_cleanup_tasks.py`、`tests/unit/graph/test_cleanup_factory.py`、`tests/unit/services/test_revision_preservation.py`、`tests/integration/test_document_cleanup.py`、`tests/integration/test_revision_migration.py`。

## 6. 部署要求与排查入口

1. 先运行 `alembic upgrade head`：`20261116_01` 新增清理完成/尝试字段与索引；`20261116_02` 新增文档任务的修订外键及旧显式引用回填。
2. 部署并重启同一版本的 API 和 Worker，保持 Beat 与 `maintenance` 队列消费。仅更新代码或前端不代表补偿已经执行。
3. 配置实际可用的重排服务；Docker 调用宿主机服务时使用 `host.docker.internal`。模型名称必须与提供方部署名称一致。
4. 保留历史 Neo4j 连接配置。对已软删除文档，`cleanup_completed_at` 为空表示尚未完成；结合 `cleanup_last_attempt_at`、`document_cleanup_pending`、`document_cleanup_dispatch_pending` 日志定位存储或投递故障。
5. 排查修订重建时先检查文档任务绑定的 `revision_id` 和快照归属，再看 `REVISION_SNAPSHOT_INVALID`；不要直接修改正在运行任务的输入来绕过问题。

GitHub 在推送时提示仓库从 `liufitch/Graph-RAG` 迁至 `liufitch/ALL-RAG`。原远端地址的重定向推送成功，已复核远端 `main` 与提交一致；本轮没有修改本地 origin 配置。
