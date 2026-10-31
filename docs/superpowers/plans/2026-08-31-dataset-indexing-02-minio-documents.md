# MinIO 文档存储与 API 实施计划

> **面向智能体工作者：** 必需子技能：使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`，按任务逐项执行本计划。步骤使用复选框（`- [ ]`）语法进行跟踪。

**目标：** 将批量上传的知识库原始文件安全写入 MinIO，并在 PostgreSQL `documents` 中保存可供 API 和 Worker 使用的对象元数据。

**架构：** API 流式校验并计算 SHA-256，通过异步对象存储协议把阻塞 MinIO SDK 调用移出 event loop。每个文件独立提交，批量请求可以部分成功；PostgreSQL 保存对象引用而非本地路径。

**技术栈：** FastAPI UploadFile、MinIO Python SDK、anyio、SQLAlchemy async、PostgreSQL、pytest。

**规格：** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## 全局约束

- Bucket 固定取后端配置，默认 `graph-rag-uploads`。
- object key 由 `dataset_id`、`document_id` 和白名单扩展名生成，用户不能指定。
- API 不将文件整体读入 Python bytes；按块计算大小和 SHA-256 后 rewind。
- MinIO 同步 SDK 调用必须通过线程池封装。
- `documents.data_source_info` 不保存本地绝对路径或访问密钥。
- 支持的扩展名精确为 `.txt`、`.md`、`.pdf`、`.docx`、`.xls`、`.xlsx`、`.csv`。
- 上传一个文件失败不能回滚同请求中已成功的其他文件。

---

## 文件结构

- 创建: `rag_modules/object_storage/base.py` — `ObjectStorage` protocol 和对象元数据。
- 创建: `rag_modules/object_storage/minio_store.py` — MinIO 实现。
- 创建: `rag_modules/object_storage/factory.py` — 依赖创建和缓存。
- 创建: `rag_modules/documents/types.py` — `PreparedUpload`、上传结果和错误码。
- 创建: `rag_modules/documents/validation.py` — 扩展名、MIME、magic、大小、SHA-256 校验。
- 创建: `rag_modules/repositories/document_repository.py` — 文档查询、position、重复检测和创建。
- 创建: `rag_modules/services/document_service.py` — MinIO/PostgreSQL 编排。
- 创建: `rag_modules/api/dto/document.py` — 文档上传和列表响应。
- Replace: `rag_modules/api/file_api.py` — dataset-scoped 文档路由，不再写 `data/uploads`。
- 修改: `main.py` — 保持新文档路由注册。
- 测试: `tests/unit/object_storage/test_minio_store.py`。
- 测试: `tests/unit/documents/test_upload_validation.py`。
- 测试: `tests/unit/services/test_document_service.py`。
- 测试: `tests/api/test_document_api.py`。
- 测试: `tests/integration/test_minio_documents.py`。

### 任务 1：异步对象存储边界

**接口：**
- 产出: `ObjectStorage.ensure_bucket() -> Awaitable[None]`。
- 产出: `put_stream(object_key, stream, length, content_type) -> StoredObject`。
- 产出: `get_stream(object_key) -> AsyncContextManager[BinaryIO]` and `remove_object(object_key)`。

- [ ] **步骤 1：编写失败的 MinIO 适配器测试**

```python
# tests/unit/object_storage/test_minio_store.py
@pytest.mark.asyncio
async def test_put_stream_uses_configured_bucket_and_exact_length(monkeypatch):
    client = FakeMinioClient()
    store = MinioObjectStorage(client=client, bucket="graph-rag-uploads")
    stream = io.BytesIO(b"hello")

    stored = await store.put_stream("datasets/d1/documents/x/source.txt", stream, 5, "text/plain")

    assert client.put_calls == [
        ("graph-rag-uploads", "datasets/d1/documents/x/source.txt", 5, "text/plain")
    ]
    assert stored.object_key.endswith("source.txt")


@pytest.mark.asyncio
async def test_remove_object_is_idempotent_for_missing_key():
    store = MinioObjectStorage(client=MissingObjectClient(), bucket="graph-rag-uploads")
    await store.remove_object("missing")
```

- [ ] **步骤 2：运行并观察模块缺失**

运行: `python -m pytest tests/unit/object_storage/test_minio_store.py -v`

预期：失败，因为 `rag_modules.object_storage` 不存在。

- [ ] **步骤 3：实现协议与 MinIO 适配器**

使用以下稳定接口：

```python
@dataclass(frozen=True)
class StoredObject:
    bucket: str
    object_key: str
    etag: str | None


class ObjectStorage(Protocol):
    async def ensure_bucket(self) -> None: ...
    async def put_stream(
        self, object_key: str, stream: BinaryIO, length: int, content_type: str
    ) -> StoredObject: ...
    @asynccontextmanager
    async def get_stream(self, object_key: str) -> AsyncIterator[BinaryIO]: ...
    async def remove_object(self, object_key: str) -> None: ...
```

使用 `anyio.to_thread.run_sync` 封装 `bucket_exists`、`make_bucket`、`put_object`、`get_object`、`response.close`、`response.release_conn` 和 `remove_object`。删除时遇到 MinIO `NoSuchKey` 应视为成功；认证和连接错误应作为类型化的 `ObjectStorageUnavailable` 向上传播。

- [ ] **步骤 4：运行适配器测试**

运行: `python -m pytest tests/unit/object_storage/test_minio_store.py -v`

预期：通过，且 fake 会记录配置的 bucket、key、length 和 content type。

- [ ] **步骤 5：提交存储边界**

```bash
git add rag_modules/object_storage tests/unit/object_storage/test_minio_store.py
git commit -m "feat: add async minio object storage"
```

### 任务 2：流式文件校验

**接口：**
- 产出: `prepare_upload(file: UploadFile, limits: UploadSettings) -> PreparedUpload`.
- `PreparedUpload` 字段：`filename`、`extension`、`content_type`、`size`、`sha256`、`stream`。
- 抛出: `UploadValidationError(code, message)` with stable codes.

- [ ] **步骤 1：编写失败的校验测试**

```python
# tests/unit/documents/test_upload_validation.py
@pytest.mark.asyncio
async def test_prepare_upload_hashes_and_rewinds_supported_file():
    file = make_upload("guide.md", b"# Guide\n\nBody", "text/markdown")

    prepared = await prepare_upload(file, UploadSettings(max_file_size_mb=1))

    assert prepared.extension == ".md"
    assert prepared.size == 13
    assert prepared.sha256 == hashlib.sha256(b"# Guide\n\nBody").hexdigest()
    assert prepared.stream.read() == b"# Guide\n\nBody"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["legacy.doc", "slides.pptx", "payload.exe"])
async def test_prepare_upload_rejects_unsupported_extensions(name):
    with pytest.raises(UploadValidationError) as error:
        await prepare_upload(make_upload(name, b"data"), UploadSettings())
    assert error.value.code == "UNSUPPORTED_FILE_TYPE"


@pytest.mark.asyncio
async def test_prepare_upload_rejects_zip_container_expansion_limit():
    file = make_upload("bomb.docx", zip_with_declared_uncompressed_size(300 * 1024 * 1024))
    with pytest.raises(UploadValidationError) as error:
        await prepare_upload(file, UploadSettings(max_decompressed_size_mb=200))
    assert error.value.code == "ARCHIVE_EXPANSION_LIMIT_EXCEEDED"
```

- [ ] **步骤 2：运行并验证失败结果**

运行: `python -m pytest tests/unit/documents/test_upload_validation.py -v`

预期：失败，因为校验类型/函数尚不存在。

- [ ] **步骤 3：实现有界流式校验**

以 1 MiB 分块读取 `UploadFile`，更新 SHA-256 和总大小，超过配置上限时立即中止，然后执行 `await file.seek(0)`。先校验扩展名，再校验签名/容器：

```python
MAGIC_PREFIXES = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
    ".xls": (bytes.fromhex("D0CF11E0A1B11AE1"),),
}
```

TXT、Markdown 和 CSV 没有固定 magic bytes，因此应拒绝 NUL 字节过多或类似二进制的样本，并接受已知文本 MIME 类型。将扩展名规范为小写，但保留原始显示文件名。对于 DOCX/XLSX，在不解压文件的情况下读取 ZIP 中央目录；在解析器打开容器前，拒绝加密条目、绝对路径/父级路径、条目数过多、超过配置的解压总大小以及压缩比过高的文件。

- [ ] **步骤 4：运行校验测试**

运行: `python -m pytest tests/unit/documents/test_upload_validation.py -v`

预期：所有允许类型、空文件、大小超限、magic 错误及不支持类型的测试均通过。

- [ ] **步骤 5：提交校验功能**

```bash
git add rag_modules/documents tests/unit/documents/test_upload_validation.py
git commit -m "feat: validate dataset document uploads"
```

### 任务 3：文档 Repository 与上传服务

**接口：**
- 产出: `DocumentRepository.next_position(dataset_id) -> int`。
- 产出: `find_duplicate(dataset_id, sha256, filename) -> DocumentRecord | None`。
- 产出: `DocumentService.upload_one(dataset_id, file, actor_id) -> DocumentUploadItem`。
- 依赖: `ObjectStorage` and `KnowledgeBaseRepository.get_active`。

- [ ] **步骤 1：编写失败的编排测试**

```python
# tests/unit/services/test_document_service.py
@pytest.mark.asyncio
async def test_upload_one_stores_object_before_committing_document():
    storage = RecordingStorage()
    repository = RecordingDocumentRepository()
    service = DocumentService(repository, ExistingDatasetRepository(), storage, UploadSettings())

    result = await service.upload_one("dataset-1", make_upload("guide.txt", b"hello"), "user-1")

    assert result.status == "waiting"
    assert storage.keys == [f"datasets/dataset-1/documents/{result.id}/source.txt"]
    assert repository.created.data_source_info["storage"] == "minio"
    assert repository.created.data_source_info["sha256"] == hashlib.sha256(b"hello").hexdigest()


@pytest.mark.asyncio
async def test_database_failure_compensates_uploaded_object():
    storage = RecordingStorage()
    service = DocumentService(FailingDocumentRepository(), ExistingDatasetRepository(), storage, UploadSettings())

    with pytest.raises(DatabaseError):
        await service.upload_one("dataset-1", make_upload("guide.txt", b"hello"), "user-1")

    assert storage.removed == storage.keys
```

- [ ] **步骤 2：运行并验证服务缺失**

运行: `python -m pytest tests/unit/services/test_document_service.py -v`

预期：因缺少 `DocumentService`/repository 而失败。

- [ ] **步骤 3：实现 repository 与补偿顺序**

在上传对象前生成 `document_id = uuid4().hex` 和对象 key：

```python
object_key = f"datasets/{dataset_id}/documents/{document_id}/source{prepared.extension}"
```

创建 `DocumentRecord`，设置 `data_source_type="upload_file"`、`created_from="api"`、`indexing_status="waiting"`、`position=next_position` 以及完整的获批 `data_source_info`。若完全相同的文件名和 SHA 已存在，则返回现有文档并设置 `duplicate=True`，不要再次上传对象。若对象上传成功但 DB 提交失败，则删除该确切对象并重新抛出异常。

- [ ] **步骤 4：运行服务测试**

运行: `python -m pytest tests/unit/services/test_document_service.py -v`

预期：包括重复文件和补偿场景在内的测试均通过。

- [ ] **步骤 5：提交文档服务**

```bash
git add rag_modules/repositories/document_repository.py rag_modules/services/document_service.py tests/unit/services/test_document_service.py
git commit -m "feat: persist minio-backed documents"
```

### 任务 4：批量上传与文档列表 API

**接口：**
- 产出: `POST /api/knowledge_base/{dataset_id}/documents` with `files: list[UploadFile]`.
- 产出: `GET /api/knowledge_base/{dataset_id}/documents` with pagination, status and name filters.
- 批量响应：`{documents: [...], rejected: [...]}`。

- [ ] **步骤 1：编写失败的 API 测试**

```python
# tests/api/test_document_api.py
def test_batch_upload_returns_successes_and_rejections(client):
    response = client.post(
        "/api/knowledge_base/dataset-1/documents",
        files=[
            ("files", ("guide.txt", b"hello", "text/plain")),
            ("files", ("legacy.doc", b"bad", "application/msword")),
        ],
    )

    assert response.status_code == 201
    assert [item["name"] for item in response.json()["documents"]] == ["guide.txt"]
    assert response.json()["rejected"][0]["code"] == "UNSUPPORTED_FILE_TYPE"


def test_document_list_is_scoped_to_dataset(client):
    response = client.get("/api/knowledge_base/dataset-1/documents?page=1&page_size=20")
    assert response.status_code == 200
    assert all(item["dataset_id"] == "dataset-1" for item in response.json()["items"])
```

- [ ] **步骤 2：运行并观察 404**

运行: `python -m pytest tests/api/test_document_api.py -v`

预期：因路由返回 404 而失败。

- [ ] **步骤 3：替换本地上传路由**

将 `file_api.py` 路由器前缀改为 `/api/knowledge_base/{dataset_id}/documents`。移除 `UPLOAD_DIR`、`Path.write_bytes` 和 `/api/file_manage/upload`。注入 `DocumentService`，初期按顺序处理文件以控制内存，并将每个 `UploadValidationError` 转换为 rejected 条目，而不终止整个请求。基础设施/存储不可用时，整个请求返回 503，因为无法安全接收任何文件。

添加列表查询校验：

```python
page: int = Query(1, ge=1)
page_size: int = Query(20, ge=1, le=100)
status: str | None = Query(None)
q: str | None = Query(None, max_length=255)
```

- [ ] **步骤 4：运行 API 和回归测试**

运行: `python -m pytest tests/api/test_document_api.py tests/api/test_dataset_api.py tests/test_api_routes.py -v`

预期：通过；OpenAPI 包含 dataset 范围内的路由，且不再包含活动的 `/api/file_manage/upload`。

- [ ] **步骤 5：提交文档 API**

```bash
git add rag_modules/api/file_api.py rag_modules/api/dto/document.py main.py tests/api/test_document_api.py
git commit -m "feat: add dataset document upload api"
```

### 任务 5：真实 MinIO 集成

**接口：**
- 使用 Compose MinIO 服务验证本阶段接口。

- [ ] **步骤 1：添加带唯一前缀的集成测试**

```python
# tests/integration/test_minio_documents.py
@pytest.mark.integration
@pytest.mark.asyncio
async def test_round_trip_and_remove_real_minio(minio_store):
    key = f"integration/{uuid4().hex}/source.txt"
    await minio_store.ensure_bucket()
    await minio_store.put_stream(key, io.BytesIO(b"round-trip"), 10, "text/plain")

    async with minio_store.get_stream(key) as stream:
        assert stream.read() == b"round-trip"

    await minio_store.remove_object(key)
    await minio_store.remove_object(key)
```

- [ ] **步骤 2：在 MinIO 未运行时执行，并确认明确的跳过/失败策略**

运行: `python -m pytest tests/integration/test_minio_documents.py -v`

预期：仅在未设置 `RUN_INTEGRATION` 时跳过；启用后遇到连接错误时不得静默通过。

- [ ] **步骤 3：添加集成 fixture**

在 `tests/conftest.py` 中使用 `RUN_INTEGRATION=1` 控制真实基础设施测试，根据测试环境变量创建 `MinioObjectStorage`，并在 fixture 清理时只删除唯一测试前缀。

- [ ] **步骤 4：针对 Compose MinIO 运行测试**

运行:

```bash
docker compose up -d minio
RUN_INTEGRATION=1 python -m pytest tests/integration/test_minio_documents.py -v
```

预期：通过，且测试对象已不存在。

- [ ] **步骤 5：提交集成测试覆盖**

```bash
git add tests/conftest.py tests/integration/test_minio_documents.py
git commit -m "test: cover minio document round trip"
```

## 阶段验证

运行:

```bash
python -m pytest tests/unit/object_storage tests/unit/documents tests/unit/services/test_document_service.py tests/api/test_document_api.py -v
RUN_INTEGRATION=1 python -m pytest tests/integration/test_minio_documents.py -v
git diff --check
```

预期：上传由 MinIO 提供支持，元数据保存在 PostgreSQL 中，不再使用本地 `data/uploads` 写入路径，且批量部分成功结果具有确定性。
