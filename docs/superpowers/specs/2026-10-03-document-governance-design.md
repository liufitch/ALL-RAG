# 文档治理与分段编辑设计

**日期：** 2026-10-03  
**状态：** 已确认，进入实现

## 目标

为知识库文档提供下载、删除、重命名、启用/禁用、归档/恢复、重新索引和分段编辑能力。分段编辑保存后自动重新索引，同时保证新索引完成前旧索引仍可用。

## 现状

- 前端 `frontend/src/components/Documents.jsx` 当前只支持上传、刷新和索引配置，列表只有文件名与索引状态。
- `DocumentRecord` 已有 `enabled`、`archived`、`deleted_at`、`disabled_*`、`archived_*` 和 `updated_*` 字段。
- 对象存储通过 `ObjectStorage` 抽象访问 MinIO，文档对象键保存在 `data_source_info.object_key`。
- 索引流程已经支持 `IndexingJobRecord`、`IndexingJobDocumentRecord`、新 `DatasetIndexRecord` 构建和完成后激活；当前 Worker 默认从原始对象重新解析。
- `DocumentSegmentRecord` 保存内容、关键词、问答字段、父子关系、源定位和索引版本，但没有用户编辑修订快照。

## 设计

### API

新增文档治理接口，全部限定在 `dataset_id` 下，并验证文档属于该知识库且未软删除：

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/{document_id}/download` | 从对象存储流式下载原文件；文件名和 MIME 类型来自保存的源信息 |
| PATCH | `/{document_id}` | 重命名显示名并同步 `data_source_info.original_filename`，不改变对象键 |
| DELETE | `/{document_id}` | 软删除文档，提交后投递异步清理任务 |
| POST | `/{document_id}/enable` | 启用文档；不重新索引 |
| POST | `/{document_id}/disable` | 禁用文档；立即从检索范围排除 |
| POST | `/{document_id}/archive` | 归档文档；立即从检索范围排除 |
| POST | `/{document_id}/restore` | 恢复归档文档；沿用已有完成索引 |
| POST | `/{document_id}/reindex` | 创建单文档重新索引任务并投递任务消息 |
| GET | `/{document_id}/segments` | 返回当前 active 索引中的分段 |
| PATCH | `/{document_id}/segments` | 校验并保存完整分段修订快照，然后自动创建重新索引任务 |

列表 DTO 增加 `enabled`、`archived`、`updated_at`、`error`、`size`、`content_type`、`segment_count` 等字段。分段 DTO 包含 `id`、`position`、`content`、`question`、`answer`、`keywords`、`parent_id`、`index_type` 和 `source_metadata`。

### 修订与自动重新索引

新增 `document_revisions` 表保存不可变修订：

- `id`、`document_id`、`dataset_id`、`version`；
- `segments` JSON 快照，保留内容、顺序、父子关系、源定位、关键词、问题和答案；
- `created_by`、`created_at`、`indexing_job_id`、`status`、`error`。

分段编辑接口在同一事务内校验请求、创建 revision、创建 `document_reindex` 任务和任务文档记录，并在提交后投递消息。Worker 根据任务的 `revision_id` 读取快照：

1. 跳过 MinIO 下载、解析和自动分段；
2. 将快照转换为索引引擎使用的 `PreviewSegment`；
3. 复用现有 staging、关键词生成、Embedding、向量写入和索引版本激活流程；
4. 新版本完成前旧 active 索引保持可检索；
5. 成功后切换新版本，失败时保留旧版本并将 revision 标记失败。

普通“重新索引”仍从 MinIO 原始文件重新解析，适用于文件内容未变但索引参数或索引状态需要恢复的情况。

### 状态与检索可见性

检索和文档统计统一排除：

```text
deleted_at IS NOT NULL
OR enabled = false
OR archived = true
OR indexing_status != completed
```

删除、禁用、归档使用幂等状态变更。恢复和启用不自动重建索引；如果没有可用完成索引，前端提示用户重新索引。

### 异步清理

删除接口只提交数据库软删除状态，随后投递清理消息。清理 Worker 删除 MinIO 原文件、该文档旧索引版本的分段和向量；清理失败保留可重试状态，不回滚用户已确认的删除。

### 前端

`Documents.jsx` 增加操作列和状态筛选：下载、重命名、启用/禁用、归档/恢复、重新索引、删除。文档详情弹窗加载分段列表，支持编辑内容、问题、答案、关键词和顺序；保存后显示“已保存，正在重新索引”，轮询 job 状态并在失败时显示重试入口。下载通过 API 响应创建浏览器下载，不暴露对象存储 URL。

## 实现难点与解决方案

### 1. 编辑后的分段不能依赖原文件重新解析

原有 Worker 从 MinIO 读取文件并自动分段，无法表达人工修改。解决方案是保存不可变 revision JSON，并让任务快照携带 `revision_id`；Worker 直接把 revision 转成 `PreviewSegment`，沿用后续索引阶段。

### 2. 旧索引不能在新索引完成前失效

编辑或重新索引失败时不能让文档消失。任务使用新的 `DatasetIndexRecord`/分段 staging，只有终态成功才切换 active index；旧分段和向量在切换后异步删除。

### 3. 分段父子关系和确定性 ID

编辑请求可能改变顺序、父子引用或空内容。API 层限制字段长度和关系完整性；Worker 将局部 ID 转换为索引引擎所需的 `PreviewSegment`，由现有 `SegmentRepository` 负责规范化、哈希和确定性 ID，避免重复重试产生重复记录。

### 4. 删除、禁用、归档的检索一致性

仅修改文档状态不足以保证所有查询路径安全。文档列表、统计、检索和分段读取都必须加入同一套可见性谓词；数据库状态提交先于异步清理，避免清理任务失败时文档仍被召回。

### 5. 下载的异步流和对象存储错误

MinIO 对象不能直接暴露给浏览器。API 需要把 `get_stream` 包装为 FastAPI `StreamingResponse`，从 `data_source_info` 恢复安全文件名和 MIME 类型，并把对象存储不可用映射为稳定的 503 错误。

### 6. 重复操作和并发编辑

同一文档可能同时收到重索引、删除或两次保存。服务层使用数据库行锁/活动任务检查：已有 pending、queued 或 running 任务时复用或拒绝重复任务；编辑按文档当前 revision 版本校验，过期版本返回 409，防止后保存覆盖先保存。

## 错误处理

- 文档不属于 dataset、已删除或目标不存在：404。
- 名称、分段关系、字段长度或 revision 版本不合法：422。
- 并发 revision 或已有互斥任务：409。
- 数据库、对象存储、消息队列临时不可用：503，并返回稳定消息。
- 索引失败不回滚 revision；返回 job 状态和安全错误信息，旧 active 索引继续使用。

## 测试策略

遵循 TDD：每个服务/API/Worker 行为先写失败测试，再实现最小代码。覆盖服务层状态转换与 revision 校验、API 隔离和响应头、Worker revision 输入和索引切换、前端操作和轮询。完成前运行后端全量 pytest、前端测试和迁移检查。

