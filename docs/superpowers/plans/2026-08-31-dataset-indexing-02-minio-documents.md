# MinIO Document Storage and API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将批量上传的知识库原始文件安全写入 MinIO，并在 PostgreSQL `documents` 中保存可供 API 和 Worker 使用的对象元数据。

**Architecture:** API 流式校验并计算 SHA-256，通过异步对象存储协议把阻塞 MinIO SDK 调用移出 event loop。每个文件独立提交，批量请求可以部分成功；PostgreSQL 保存对象引用而非本地路径。

**Tech Stack:** FastAPI UploadFile、MinIO Python SDK、anyio、SQLAlchemy async、PostgreSQL、pytest。

**Spec:** `docs/superpowers/specs/2026-08-31-dify-style-dataset-indexing-design.md`

## Global Constraints

- Bucket 固定取后端配置，默认 `graph-rag-uploads`。
- object key 由 `dataset_id`、`document_id` 和白名单扩展名生成，用户不能指定。
- API 不将文件整体读入 Python bytes；按块计算大小和 SHA-256 后 rewind。
- MinIO 同步 SDK 调用必须通过线程池封装。
- `documents.data_source_info` 不保存本地绝对路径或访问密钥。
- 支持的扩展名精确为 `.txt`、`.md`、`.pdf`、`.docx`、`.xls`、`.xlsx`、`.csv`。
- 上传一个文件失败不能回滚同请求中已成功的其他文件。

---

## File Structure

- Create: `rag_modules/object_storage/base.py` — `ObjectStorage` protocol 和对象元数据。
- Create: `rag_modules/object_storage/minio_store.py` — MinIO 实现。
- Create: `rag_modules/object_storage/factory.py` — 依赖创建和缓存。
- Create: `rag_modules/documents/types.py` — `PreparedUpload`、上传结果和错误码。
- Create: `rag_modules/documents/validation.py` — 扩展名、MIME、magic、大小、SHA-256 校验。
- Create: `rag_modules/repositories/document_repository.py` — 文档查询、position、重复检测和创建。
- Create: `rag_modules/services/document_service.py` — MinIO/PostgreSQL 编排。
- Create: `rag_modules/api/dto/document.py` — 文档上传和列表响应。
- Replace: `rag_modules/api/file_api.py` — dataset-scoped 文档路由，不再写 `data/uploads`。
- Modify: `main.py` — 保持新文档路由注册。
- Test: `tests/unit/object_storage/test_minio_store.py`。
- Test: `tests/unit/documents/test_upload_validation.py`。
- Test: `tests/unit/services/test_document_service.py`。
- Test: `tests/api/test_document_api.py`。
- Test: `tests/integration/test_minio_documents.py`。

### Task 1: Async Object Storage Boundary

**Interfaces:**
- Produces: `ObjectStorage.ensure_bucket() -> Awaitable[None]`。
- Produces: `put_stream(object_key, stream, length, content_type) -> StoredObject`。
- Produces: `get_stream(object_key) -> AsyncContextManager[BinaryIO]` and `remove_object(object_key)`。

- [ ] **Step 1: Write failing MinIO adapter tests**

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

- [ ] **Step 2: Run and observe missing module**

Run: `python -m pytest tests/unit/object_storage/test_minio_store.py -v`

Expected: FAIL because `rag_modules.object_storage` does not exist.

- [ ] **Step 3: Implement protocol and MinIO adapter**

Use these stable interfaces:

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

Wrap `bucket_exists`, `make_bucket`, `put_object`, `get_object`, `response.close`, `response.release_conn`, and `remove_object` with `anyio.to_thread.run_sync`. Treat MinIO `NoSuchKey` during removal as success; propagate authentication and connectivity errors as typed `ObjectStorageUnavailable`.

- [ ] **Step 4: Run adapter tests**

Run: `python -m pytest tests/unit/object_storage/test_minio_store.py -v`

Expected: PASS and the fake records the configured bucket, key, length and content type.

- [ ] **Step 5: Commit storage boundary**

```bash
git add rag_modules/object_storage tests/unit/object_storage/test_minio_store.py
git commit -m "feat: add async minio object storage"
```

### Task 2: Streaming File Validation

**Interfaces:**
- Produces: `prepare_upload(file: UploadFile, limits: UploadSettings) -> PreparedUpload`.
- `PreparedUpload` fields: `filename`, `extension`, `content_type`, `size`, `sha256`, `stream`.
- Raises: `UploadValidationError(code, message)` with stable codes.

- [ ] **Step 1: Write failing validation tests**

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

- [ ] **Step 2: Run and verify failure**

Run: `python -m pytest tests/unit/documents/test_upload_validation.py -v`

Expected: FAIL because validation types/functions do not exist.

- [ ] **Step 3: Implement bounded streaming validation**

Read `UploadFile` in 1 MiB chunks, update SHA-256 and total size, abort as soon as the configured maximum is exceeded, then `await file.seek(0)`. Validate extension first; validate signature/container next:

```python
MAGIC_PREFIXES = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04",),
    ".xlsx": (b"PK\x03\x04",),
    ".xls": (bytes.fromhex("D0CF11E0A1B11AE1"),),
}
```

TXT, Markdown and CSV have no fixed magic bytes, so reject NUL-heavy/binary-looking samples and accept known text MIME types. Normalize the extension to lowercase, but preserve the original display filename. For DOCX/XLSX, read the ZIP central directory without extracting files, reject encrypted entries, absolute/parent paths, excessive entry counts, configured total uncompressed size overflow and excessive compression ratio before a parser opens the container.

- [ ] **Step 4: Run validation tests**

Run: `python -m pytest tests/unit/documents/test_upload_validation.py -v`

Expected: PASS for all allowed types, empty files, size overflow, bad magic and unsupported types.

- [ ] **Step 5: Commit validation**

```bash
git add rag_modules/documents tests/unit/documents/test_upload_validation.py
git commit -m "feat: validate dataset document uploads"
```

### Task 3: Document Repository and Upload Service

**Interfaces:**
- Produces: `DocumentRepository.next_position(dataset_id) -> int`。
- Produces: `find_duplicate(dataset_id, sha256, filename) -> DocumentRecord | None`。
- Produces: `DocumentService.upload_one(dataset_id, file, actor_id) -> DocumentUploadItem`。
- Consumes: `ObjectStorage` and `KnowledgeBaseRepository.get_active`。

- [ ] **Step 1: Write failing orchestration tests**

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

- [ ] **Step 2: Run and verify service is missing**

Run: `python -m pytest tests/unit/services/test_document_service.py -v`

Expected: FAIL on missing `DocumentService`/repository.

- [ ] **Step 3: Implement repository and compensation order**

Generate `document_id = uuid4().hex` before object upload and key:

```python
object_key = f"datasets/{dataset_id}/documents/{document_id}/source{prepared.extension}"
```

Create a `DocumentRecord` with `data_source_type="upload_file"`, `created_from="api"`, `indexing_status="waiting"`, `position=next_position`, and full approved `data_source_info`. If exact filename+SHA already exists, return the existing document with `duplicate=True` and do not upload another object. If object upload succeeds but DB commit fails, remove that exact object and re-raise.

- [ ] **Step 4: Run service tests**

Run: `python -m pytest tests/unit/services/test_document_service.py -v`

Expected: PASS including duplicate and compensation cases.

- [ ] **Step 5: Commit document service**

```bash
git add rag_modules/repositories/document_repository.py rag_modules/services/document_service.py tests/unit/services/test_document_service.py
git commit -m "feat: persist minio-backed documents"
```

### Task 4: Batch Upload and Document List API

**Interfaces:**
- Produces: `POST /api/knowledge_base/{dataset_id}/documents` with `files: list[UploadFile]`.
- Produces: `GET /api/knowledge_base/{dataset_id}/documents` with pagination, status and name filters.
- Batch response: `{documents: [...], rejected: [...]}`.

- [ ] **Step 1: Write failing API tests**

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

- [ ] **Step 2: Run and observe 404**

Run: `python -m pytest tests/api/test_document_api.py -v`

Expected: FAIL with route 404.

- [ ] **Step 3: Replace the local upload route**

Change `file_api.py` router prefix to `/api/knowledge_base/{dataset_id}/documents`. Remove `UPLOAD_DIR`, `Path.write_bytes`, and `/api/file_manage/upload`. Inject `DocumentService`, process files sequentially initially to keep memory bounded, and map each `UploadValidationError` to a rejected item without aborting the request. Infrastructure/storage unavailability returns 503 for the whole request because no file can be safely accepted.

Add list query validation:

```python
page: int = Query(1, ge=1)
page_size: int = Query(20, ge=1, le=100)
status: str | None = Query(None)
q: str | None = Query(None, max_length=255)
```

- [ ] **Step 4: Run API and regression tests**

Run: `python -m pytest tests/api/test_document_api.py tests/api/test_dataset_api.py tests/test_api_routes.py -v`

Expected: PASS; OpenAPI contains the dataset-scoped route and no active `/api/file_manage/upload`.

- [ ] **Step 5: Commit document API**

```bash
git add rag_modules/api/file_api.py rag_modules/api/dto/document.py main.py tests/api/test_document_api.py
git commit -m "feat: add dataset document upload api"
```

### Task 5: Real MinIO Integration

**Interfaces:**
- Verifies phase interfaces against the Compose MinIO service.

- [ ] **Step 1: Add an integration test with a unique prefix**

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

- [ ] **Step 2: Run without MinIO and confirm explicit skip/failure policy**

Run: `python -m pytest tests/integration/test_minio_documents.py -v`

Expected: SKIP only when `RUN_INTEGRATION` is not set; it must not silently pass on a connection error when enabled.

- [ ] **Step 3: Add integration fixtures**

In `tests/conftest.py`, gate real infrastructure with `RUN_INTEGRATION=1`, create `MinioObjectStorage` from test environment variables, and delete only the unique test prefix in fixture cleanup.

- [ ] **Step 4: Run against Compose MinIO**

Run:

```bash
docker compose up -d minio
RUN_INTEGRATION=1 python -m pytest tests/integration/test_minio_documents.py -v
```

Expected: PASS and the test object no longer exists.

- [ ] **Step 5: Commit integration coverage**

```bash
git add tests/conftest.py tests/integration/test_minio_documents.py
git commit -m "test: cover minio document round trip"
```

## Phase Verification

Run:

```bash
python -m pytest tests/unit/object_storage tests/unit/documents tests/unit/services/test_document_service.py tests/api/test_document_api.py -v
RUN_INTEGRATION=1 python -m pytest tests/integration/test_minio_documents.py -v
git diff --check
```

Expected: uploads are MinIO-backed, metadata is in PostgreSQL, no local `data/uploads` write path remains active, and batch partial success is deterministic.
